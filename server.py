#!/usr/bin/env python3
"""vidtx 接收端（CLI）。

用法：
  python server.py                        # 默认设备 0，输出到 ./received
  python server.py -o D:/recv -i 0        # 指定设备与输出目录
  python server.py --list-devices         # 探测可用采集设备
  python server.py -i video.mp4           # 无硬件回环测试：直接解码视频文件

行为：
  持续监测采集卡信号；解码出的 vidtx 分片暂存于 <输出>/.vidtx-staging/，
  收齐 manifest 所列分片并通过 sha256 校验后，拼接为原始文件落入输出目录。
  也兼容普通 cimbar 流（非 vidtx 分片协议的文件直接落盘）。

依赖：Python 3.10+（仅标准库）。原生二进制需先运行 build_native.py 生成。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import common

ROOT = Path(__file__).resolve().parent
IS_WIN = sys.platform == "win32"
EXE = ".exe" if IS_WIN else ""
RECEIVER = ROOT / "bin" / ("vidbar_recv" + EXE)

STAGING_DIRNAME = ".vidtx-staging"
RESTART_DELAY_AFTER_ASSEMBLY = 3.0   # 完成一个文件后延迟重启接收器（清理解码器状态）
MAX_RAPID_RESTARTS = 10              # 短时间内崩溃重启上限


class Session:
    """一次传输会话（一个 sid）的接收状态。"""

    def __init__(self, manifest: common.Manifest, staging_dir: Path):
        self.manifest = manifest
        self.staging = staging_dir
        self.chunks: dict[str, Path] = {}      # name -> 已校验的分片路径
        self.first_seen = time.time()
        self.reported = False
        self.needs_restart = False             # 有分片校验失败，需要重启接收器

    @property
    def sid(self) -> str:
        return self.manifest.sid

    def chunk_path(self, name: str) -> Path:
        return self.staging / name

    def adopt_existing(self) -> None:
        """把 staging 中已存在的本会话分片纳入状态。"""
        for c in self.manifest.chunks:
            p = self.chunk_path(c["name"])
            if p.exists() and c["name"] not in self.chunks:
                self._try_chunk(c, p)

    def on_chunk_file(self, path: Path) -> bool:
        name = path.name
        for c in self.manifest.chunks:
            if c["name"] == name:
                if name in self.chunks:
                    return self.is_complete()
                self._try_chunk(c, path)
                return self.is_complete()
        return self.is_complete()

    def _try_chunk(self, c: dict, path: Path) -> None:
        if common.verify_chunk(path, c["sha256"]):
            self.chunks[c["name"]] = path
            self._report_progress()
        else:
            print(f"[!] 分片校验失败，丢弃等待重传: {path.name}")
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            # 解码器已把该 (id,size) 流标记为完成，不会再接收同一分片；
            # 标记需要重启接收器，才能接收客户端下一轮重传。
            self.needs_restart = True

    def is_complete(self) -> bool:
        return len(self.chunks) == len(self.manifest.chunks)

    def _report_progress(self) -> None:
        total = len(self.manifest.chunks)
        print(f"[*] {self.manifest.filename}: 分片 {len(self.chunks)}/{total}"
              f"（{common.human_size(self.manifest.size)}）")

    def assemble(self, output_dir: Path) -> Path | None:
        dest = common.unique_dest(output_dir / common.sanitize_filename(self.manifest.filename))
        if not common.assemble(self.manifest, self.chunks, dest):
            print(f"[!] 拼接失败（整体 sha256 不符）: {self.manifest.filename}")
            return None
        return dest


class Server:
    def __init__(self, args):
        self.args = args
        self.output_dir = Path(args.output).resolve()
        self.staging_dir = Path(args.staging).resolve() if args.staging \
            else self.output_dir / STAGING_DIRNAME
        self.proc: subprocess.Popen | None = None
        self.sessions: dict[str, Session] = {}
        self.shutdown = threading.Event()
        self._restart_at: float | None = None
        self._restart_times: list[float] = []
        self._last_stat: dict = {}
        self._last_stat_print = 0.0
        self._t0 = time.time()

    # ---------- 主循环 ----------

    def run(self) -> None:
        if not RECEIVER.exists():
            print(f"未找到 {RECEIVER}，请先运行: python build_native.py", file=sys.stderr)
            sys.exit(1)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._recover_sessions()

        print(f"输出目录: {self.output_dir}")
        print(f"暂存目录: {self.staging_dir}")
        split_desc = {0: "码流: 自动识别（单/双窗口均免配置）"}.get(self.args.split)
        if split_desc is None:
            split_desc = f"码流: 强制 {self.args.split} 条"
        print(f"设备: {self.args.device} | 后端: {self.args.api} | "
              f"请求: {self.args.width}x{self.args.height} @ {self.args.fps or '默认'}fps "
              f"({self.args.fourcc}) | {split_desc}")
        print("等待信号...（Ctrl+C 退出）")

        try:
            while not self.shutdown.is_set():
                # 完成一个文件后延迟重启接收器（清理解码器 done 状态，防止后续传输撞 id）
                if self._restart_at is not None and time.time() >= self._restart_at:
                    self._restart_at = None
                    self._kill_receiver()

                if self.proc is None:
                    if self._restart_backoff():
                        continue
                    self._spawn_receiver()
                elif self.proc.poll() is not None:
                    if self.args.source is not None:
                        # 视频文件模式：读尽剩余事件后正常结束
                        self._pump_output()
                        print("\n[i] 视频播放完毕，解码结束。")
                        break
                    if self._restart_backoff():
                        continue
                    self._spawn_receiver()

                self._pump_output()
                time.sleep(0.05)
        except KeyboardInterrupt:
            pass  # Ctrl+C：跳到 finally 杀子进程（否则 vidbar_recv 残留并占用采集卡）
        finally:
            self._kill_receiver()
        print("\n已退出。")

    def _spawn_receiver(self) -> None:
        cmd = [str(RECEIVER),
               "-i", str(self.args.device),
               "-o", str(self.staging_dir),
               "--api", self.args.api,
               "--fourcc", self.args.fourcc,
               "-w", str(self.args.width),
               "--height", str(self.args.height),
               "-s", "60"]
        if self.args.fps:
            cmd += ["-F", str(self.args.fps)]
        cmd += ["--split", str(self.args.split)]
        if self.args.mode:
            cmd += ["-m", self.args.mode]
        if self.args.source:  # 回环测试：视频文件代替设备
            cmd[cmd.index(str(self.args.device))] = str(self.args.source)

        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL,
                                         text=True, encoding="utf-8", errors="replace",
                                         creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
        except OSError as e:
            print(f"[!] 启动 vidbar_recv 失败: {e}", file=sys.stderr)
            self.shutdown.set()
            return

        if self._restart_at is None:
            self._restart_times.append(time.time())
        print(f"[+] vidbar_recv 已启动 (pid={self.proc.pid})")

    def _restart_backoff(self) -> bool:
        """崩溃重启保护：10 秒内最多 MAX_RAPID_RESTARTS 次。"""
        now = time.time()
        self._restart_times = [t for t in self._restart_times if now - t < 10]
        if len(self._restart_times) >= MAX_RAPID_RESTARTS:
            print("[!] 接收器反复崩溃，退出。请检查 --device / --api 参数。", file=sys.stderr)
            self.shutdown.set()
            return True
        time.sleep(0.5)
        return False

    def _kill_receiver(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None

    def _pump_output(self) -> None:
        if not self.proc or not self.proc.stdout:
            return
        stream = self.proc.stdout
        while True:
            line = stream.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            self._dispatch(ev)

    # ---------- 事件处理 ----------

    def _dispatch(self, ev: dict) -> None:
        kind = ev.get("ev")
        if kind == "negotiate":
            print(f"[.] 采集协商: {ev.get('msg')}")
        elif kind == "open":
            fps = ev.get("fps", 0)
            print(f"[+] 信号源已打开: {ev.get('w')}x{ev.get('h')} @ {fps}fps "
                  f"({ev.get('fourcc')})")
            req = (self.args.fourcc or "").upper()
            got = str(ev.get("fourcc") or "").upper()
            if req and got and "?" not in got and req != got:
                print(f"[!] 提示: 请求 {req} 但采集卡实际工作在 {got}。"
                      f"若下方诊断 readfps ≥ 50 则无碍（USB3 带宽足够，"
                      f"YUY2 无压缩画质反而更好）；若 readfps 骤降则说明带宽不足")
            if fps and fps < 29:
                print("[!] 注意: 实际帧率低于 30，请检查采集卡是否工作在 MJPG 模式")
        elif kind == "signal":
            if ev.get("locked"):
                print("[+] 信号锁定：检测到 vidtx/cimbar 画面")
            else:
                print("[-] 信号丢失")
        elif kind == "saved":
            self._on_saved(Path(ev["path"]))
        elif kind == "stat":
            self._last_stat = ev
            self._print_stat(ev)
        elif kind == "error":
            print(f"[!] vidbar_recv: {ev.get('msg')}")

    def _on_saved(self, path: Path) -> None:
        name = path.name
        if common.is_manifest_name(name):
            m = common.parse_manifest(path)
            if m is None:
                print(f"[!] manifest 解析失败（数据损坏），等待重传: {path}")
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                self._schedule_restart()
                return
            sess = Session(m, self.staging_dir)
            self.sessions[m.sid] = sess
            print(f"[+] 收到 manifest：{m.filename}"
                  f"（{common.human_size(m.size)}，{len(m.chunks)} 个分片）")
            sess.adopt_existing()
            self._check_complete(sess)
            if sess.needs_restart:
                sess.needs_restart = False
                self._schedule_restart()
        elif common.sid_of(name):
            sid = common.sid_of(name)
            sess = self.sessions.get(sid)
            if sess is None:
                # manifest 还没到（服务端中途加入），先留在暂存目录等 manifest
                print(f"[*] 分片 {name} 已收到（等待 manifest）")
                return
            if sess.on_chunk_file(path):
                self._check_complete(sess)
            if sess.needs_restart:
                sess.needs_restart = False
                self._schedule_restart()
        else:
            self._handle_stray(path)

    def _handle_stray(self, path: Path) -> None:
        """非 vidtx 协议的普通 cimbar 文件，或文件名损坏的 vidtx 分片。"""
        name = path.name
        if name.startswith("vt_"):
            # vidtx 分片名必然匹配 vt_<hex sid>_NNN / vt_<hex sid>_manifest.json。
            # 带着损坏名字落盘说明解码数据出错（zstd 头被误码破坏）。
            print(f"[!] 损坏的 vidtx 分片（文件名异常），丢弃等待重传: {name!r}")
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            self._schedule_restart()
            return
        try:
            dest = common.unique_dest(self.output_dir / common.sanitize_filename(name))
            shutil.move(str(path), dest)
            print(f"[✓] 已保存: {dest}")
        except OSError as e:
            print(f"[!] 保存失败（忽略）: {name!r}: {e}")

    def _schedule_restart(self, delay: float = 1.0) -> None:
        """请求重启接收器：解码器对已完成的流不会重收，损坏数据需清除状态后重传。"""
        if self.args.source is not None:  # 视频文件模式自然播完退出，不重启
            return
        if self._restart_at is None:
            self._restart_at = time.time() + delay

    def _check_complete(self, sess: Session) -> None:
        if not sess.is_complete() or sess.reported:
            return
        sess.reported = True
        elapsed = max(time.time() - sess.first_seen, 0.001)
        speed = sess.manifest.size / elapsed
        dest = sess.assemble(self.output_dir)
        if dest is None:
            sess.reported = False  # 允许之后重试
            return
        print("=" * 62)
        print(f"[✓] 接收完成: {sess.manifest.filename} "
              f"({common.human_size(sess.manifest.size)})")
        print(f"    -> {dest}")
        print(f"    sha256 校验通过 · 用时 {int(elapsed // 60)} 分 {int(elapsed % 60)} 秒 "
              f"· 平均 {common.human_size(int(speed))}/s")
        print("=" * 62)
        common.clean_staging(self.staging_dir, sess.sid)
        if self.args.source is None:  # 视频文件模式让接收器自然播完退出
            self._restart_at = time.time() + RESTART_DELAY_AFTER_ASSEMBLY

    # ---------- 恢复与状态 ----------

    def _recover_sessions(self) -> None:
        """启动时扫描暂存目录，恢复未完成会话。"""
        for p in sorted(self.staging_dir.iterdir()):
            if common.is_manifest_name(p.name):
                m = common.parse_manifest(p)
                if m:
                    self.sessions[m.sid] = Session(m, self.staging_dir)
        for sess in self.sessions.values():
            sess.adopt_existing()
            self._check_complete(sess)

    def _print_stat(self, ev: dict) -> None:
        """换行打印解码诊断（\r 原地刷新在 PowerShell 里既不显眼也没法复制反馈）。"""
        now = time.time()
        if now - self._last_stat_print < 8:
            return
        self._last_stat_print = now

        prefer = ev.get("prefer")
        layout = {1: "条带", 2: "整帧"}.get(prefer, "探测中")
        prog = ev.get("progress") or []
        prog_s = ",".join(f"{p * 100:.0f}%" for p in prog) or "-"
        mins = int((now - self._t0) // 60)
        print(f"[诊断 {mins:02d}m] 帧数={ev.get('frames', 0)} 实际采集={ev.get('readfps', 0):.0f}fps "
              f"布局={layout} 条带(提角/解出)={ev.get('sext', 0)}/{ev.get('sdec', 0)} "
              f"整帧(提角/解出)={ev.get('fext', 0)}/{ev.get('fdec', 0)} "
              f"码流数={ev.get('streams', 0)} 进度={prog_s}")


def list_devices(args) -> None:
    print("探测采集设备（每个约 2 秒）...")
    found = False
    for idx in range(8):
        cmd = [str(RECEIVER), "-i", str(idx), "-o", tempfile.mkdtemp(prefix="vidtx-probe-"),
               "--api", args.api, "--fourcc", args.fourcc, "-s", "0"]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"[!] 启动失败: {e}")
            return
        t0 = time.time()
        desc = None
        while time.time() - t0 < 2.5:
            line = p.stdout.readline() if p.stdout else ""
            if not line:
                if p.poll() is not None:
                    break
                continue
            try:
                ev = json.loads(line.strip())
            except ValueError:
                continue
            if ev.get("ev") == "open":
                desc = (f"{ev.get('w')}x{ev.get('h')} @ {ev.get('fps')}fps "
                        f"({ev.get('fourcc')})")
                break
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
        if desc:
            found = True
            print(f"  设备 {idx}: {desc}")
    if not found:
        print("  未发现可用设备。尝试 --api msmf / --fourcc YUY2 / 检查连接。")


def main() -> None:
    ap = argparse.ArgumentParser(description="vidtx 接收端：监测采集卡并还原文件")
    ap.add_argument("-o", "--output", default="received", help="输出目录（默认 ./received）")
    ap.add_argument("-i", "--device", default="0", help="采集设备索引（默认 0）")
    ap.add_argument("--source", default=None, help="[测试] 从视频文件解码（模拟采集卡）")
    ap.add_argument("--api", default="dshow" if IS_WIN else "v4l2",
                    choices=["dshow", "msmf", "v4l2", "any"], help="采集后端")
    ap.add_argument("--fourcc", default="MJPG", help="像素格式（默认 MJPG；传空跳过设置）")
    ap.add_argument("-w", "--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("-F", "--fps", type=int, default=60 if IS_WIN else 30,
                    help="请求采集帧率（0=不设置，默认 Windows 60 / 其他 30）")
    ap.add_argument("-m", "--mode", default="B", help="cimbar 模式（默认 B）")
    ap.add_argument("--split", type=int, default=0, choices=[0, 1, 2, 3, 4],
                    help="同帧竖切几条码流（0=自动识别单/双码流，默认；1=强制整帧；"
                         "客户端双窗口同屏模式无需任何参数，自动按 2 条解）")
    ap.add_argument("--staging", default=None, help="暂存目录（默认 <输出>/.vidtx-staging）")
    ap.add_argument("--list-devices", action="store_true", help="列出可用采集设备后退出")
    args = ap.parse_args()

    if args.list_devices:
        list_devices(args)
        return

    try:
        Server(args).run()
    except KeyboardInterrupt:
        print("\n已中断。")


if __name__ == "__main__":
    main()
