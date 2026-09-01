#!/usr/bin/env python3
"""vidtx 发送端（GUI）。

用法：python client.py

流程：
  1. 选择文件，程序将其切分为分片并生成 manifest（含 sha256）；
  2. 调用 bin/vidbar_send 在独立播放窗口中循环渲染 cimbar 帧；
  3. 把播放窗口拖到被 HDMI 采集的显示器上即可，位置不限。

依赖：Python 3.10+（仅标准库）。原生二进制需先运行 build_native.py 生成。
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import common

ROOT = Path(__file__).resolve().parent
IS_WIN = sys.platform == "win32"
EXE = ".exe" if IS_WIN else ""

SENDER = ROOT / "bin" / ("vidbar_send" + EXE)

FPS_CHOICES = {"15": 15, "30": 30, "60": 60}
ROUND_CHOICES = {"1 轮（最快，要求服务端从头接收）": 1,
                 "2 轮（推荐，允许服务端中途开始）": 2,
                 "3 轮（最稳）": 3,
                 "无限循环（手动停止）": 0}
REDUNDANCY_CHOICES = {"快速 1.3x": 1.3, "标准 1.6x": 1.6, "保守 2.2x": 2.2}
# 窗口模式：双窗口时一个任务在同一块屏上左右并排开两个播放进程，
# 分片交错分配（窗口A: 偶数片, 窗口B: 奇数片），一张采集卡同帧解两路，
# 理论速度翻倍（接收端自动识别，无需参数）。
WINDOW_MODE_CHOICES = {"单窗口": 1, "双窗口加速（x2，同屏双码流）": 2}

ROUND_FILE_RE = re.compile(r"round (\d+) file (\d+) \((.*?)\): (\d+) frames")
MONITOR_RE = re.compile(r"monitor (\d+): (\d+)x(\d+) \+(-?\d+)\+(-?\d+)(?:\s+(.*))?")


def list_monitors() -> list[tuple[int, int, int, str]]:
    """向 vidbar_send --list-monitors 查询可用屏幕，返回 [(序号, 宽, 高, 名称), ...]。"""
    try:
        p = subprocess.run([str(SENDER), "--list-monitors"], capture_output=True,
                           text=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
        mons = [(int(m[1]), int(m[2]), int(m[3]), (m[5] or "").strip())
                for m in (MONITOR_RE.match(l.strip()) for l in p.stdout.splitlines()) if m]
        if mons:
            return mons
    except Exception:
        pass
    return [(1, 0, 0, "")]  # 探测失败：假装只有一块屏，不传 --monitor（兼容旧二进制）


def monitor_labels(mons: list[tuple[int, int, int, str]]) -> dict[str, int]:
    """'屏幕2 · DELL U2415 (1920x1080)' -> 2"""
    labels = {}
    for idx, w, h, name in mons:
        parts = [f"屏幕{idx}"]
        if name:
            parts.append(name)
        if w:
            parts.append(f"({w}x{h})")
        labels[" · ".join(parts)] = idx
    return labels


class TransferJob(threading.Thread):
    """后台线程：分片 -> 启动 1~2 个 vidbar_send -> 转发 stderr 进度。"""

    def __init__(self, app: "ClientApp"):
        super().__init__(daemon=True)
        self.app = app
        self.stop_flag = threading.Event()
        self.procs: list[subprocess.Popen] = []

    def _spawn(self, files: list[str], base: int, monitor: int,
               win: str | None = None, pos: str | None = None) -> subprocess.Popen:
        app = self.app
        cmd = [str(SENDER), "-f", str(app.fps), "-r", str(app.rounds),
               "-R", str(app.redundancy), "-b", str(base)]
        if monitor > 0:
            cmd += ["--monitor", str(monitor)]
        if win:
            cmd += ["--win", win]
        if pos:
            cmd += ["--pos", pos]
        cmd += files
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
        self.procs.append(proc)
        return proc

    def _reader(self, proc: subprocess.Popen, tag: str) -> None:
        """读一个 vidbar_send 的 stderr，转发进度。"""
        app = self.app
        for line in proc.stderr:  # 阻塞读，进程结束即 EOF
            line = line.strip()
            if not line:
                continue
            m = ROUND_FILE_RE.search(line)
            if m:
                rnd, idx, name, frames = int(m[1]), int(m[2]), m[3], int(m[4])
                app.on_chunk_done(rnd, idx, name, frames)
            app.emit(f"{tag} {line}" if tag else line)
        code = proc.wait()
        app.emit(f"vidbar_send{tag} 退出，code={code}")

    def run(self) -> None:
        app = self.app
        src = app.file_path
        app.emit(f"选择文件: {src} ({common.human_size(Path(src).stat().st_size)})")

        sid = common.new_sid()
        staging = Path(tempfile.gettempdir()) / "vidtx" / sid
        app.emit(f"会话 id: {sid}")

        # ---- 分片（含哈希计算） ----
        try:
            app.emit("正在分片并计算 sha256 ...")
            manifest, chunk_paths = common.split_file(src, staging, sid, app.chunk_mb)
        except OSError as e:
            app.emit(f"分片失败: {e}")
            app.finish()
            return
        if self.stop_flag.is_set():
            app.emit("已取消")
            app.finish()
            return

        manifest_path = staging / common.manifest_name(sid)
        manifest_path.write_text(manifest.to_json(), encoding="utf-8")

        self.n_chunks = len(chunk_paths)  # 供进度显示
        rounds = app.rounds
        redun = app.redundancy
        fps = app.fps
        nwin = app.n_windows if len(chunk_paths) >= 2 else 1
        if app.n_windows == 2 and nwin == 1:
            app.emit("只有 1 个分片，双窗口退化为单窗口")
        est = common.estimate_total_seconds(Path(src).stat().st_size, fps, redun,
                                            max(rounds, 1)) / nwin
        app.emit(f"共 {len(chunk_paths)} 个分片 + manifest，"
                 f"帧率 {fps}，冗余 {redun}x，轮数 {rounds or '∞'}，"
                 f"{'双窗口并行' if nwin == 2 else '单窗口'}")
        app.emit(f"预计传输用时约 {int(est // 60)} 分 {int(est % 60)} 秒（不含压缩时间）")

        base = int(sid[:2], 16) & 0x7F
        try:
            if nwin == 2:
                # 双窗口：同一块屏左右并排，分片交错分配（A: 偶数片, B: 奇数片）。
                # manifest 走窗口A；encode_id 偏移 64 避让两条流。
                app.emit("双码流模式：接收端自动识别（server.py 直接启动即可，无需参数）")
                files_a = [str(manifest_path)] + [str(p) for p in chunk_paths[0::2]]
                files_b = [str(p) for p in chunk_paths[1::2]]
                mon = app.monitor_a
                w = max(512, min(app.mon_w // 2 - 16, app.mon_h - 80))
                proc_a = self._spawn(files_a, base, mon,
                                     win=f"{w}x{w}", pos="0,0")
                proc_b = self._spawn(files_b, (base + 64) & 0x7F, mon,
                                     win=f"{w}x{w}", pos=f"{app.mon_w - w - 8},0")
                app.emit(f"窗口A pid={proc_a.pid}（左半屏 {w}x{w}），"
                         f"窗口B pid={proc_b.pid}（右半屏 {w}x{w}）")
                t_readers = [threading.Thread(target=self._reader, args=(p, t), daemon=True)
                             for p, t in ((proc_a, "[A]"), (proc_b, "[B]"))]
                for t in t_readers:
                    t.start()
                for t in t_readers:
                    t.join()
            else:
                proc = self._spawn([str(manifest_path)] + [str(p) for p in chunk_paths],
                                  base, app.monitor_a)
                app.emit(f"vidbar_send pid={proc.pid}（屏幕{app.monitor_a or 1}）")
                self._reader(proc, "")
        except OSError as e:
            app.emit(f"启动 vidbar_send 失败: {e}")
            app.finish()
            return

        app.finish()

    def stop(self) -> None:
        self.stop_flag.set()
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except OSError:
                    pass


class ClientApp:
    def __init__(self) -> None:
        self.win = tk.Tk()
        self.win.title("vidtx 发送端")
        self.win.geometry("760x520")
        self.win.minsize(680, 460)

        self.file_path: str | None = None
        self.job: TransferJob | None = None
        self.t0 = 0.0
        self.start_time = 0.0
        self.queue: queue.Queue[tuple] = queue.Queue()

        self._build_ui()
        self._poll_queue()
        self.win.protocol("WM_DELETE_WINDOW", self.on_close)
        if not SENDER.exists():
            self.emit(f"未找到 {SENDER}，请先运行: python build_native.py")

    # ---------- UI ----------

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 6}

        top = ttk.Frame(self.win)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="选择文件…", command=self.pick_file).pack(side="left")
        self.file_label = ttk.Label(top, text="（未选择）", width=72, anchor="w")
        self.file_label.pack(side="left", padx=8)

        settings = ttk.LabelFrame(self.win, text="传输设置")
        settings.pack(fill="x", **pad)

        self.fps_var = tk.StringVar(value="30")
        self.rounds_var = tk.StringVar(value=list(ROUND_CHOICES)[1])
        self.redun_var = tk.StringVar(value=list(REDUNDANCY_CHOICES)[1])
        self.chunk_var = tk.StringVar(value=str(common.DEFAULT_CHUNK_MB))

        # 探测屏幕（来自 vidbar_send --list-monitors）
        self.monitors = list_monitors()
        self.mon_labels = monitor_labels(self.monitors)
        default_mon = list(self.mon_labels)[0]
        if len(self.mon_labels) > 1:
            default_mon = list(self.mon_labels)[1]  # 多屏默认第二块（通常是采集屏）
        self.screen_var = tk.StringVar(value=default_mon)
        self.mode_var = tk.StringVar(value=list(WINDOW_MODE_CHOICES)[0])

        row = ttk.Frame(settings)
        row.pack(fill="x", padx=8, pady=4)
        for label, var, values in (
                ("播放帧率", self.fps_var, list(FPS_CHOICES)),
                ("轮数", self.rounds_var, list(ROUND_CHOICES)),
                ("冗余", self.redun_var, list(REDUNDANCY_CHOICES))):
            ttk.Label(row, text=label).pack(side="left")
            cb = ttk.Combobox(row, textvariable=var, values=values, state="readonly", width=18)
            cb.pack(side="left", padx=(2, 12))

        row2 = ttk.Frame(settings)
        row2.pack(fill="x", padx=8, pady=4)
        ttk.Label(row2, text="分片大小 (MB)").pack(side="left")
        ttk.Spinbox(row2, from_=common.MIN_CHUNK_MB, to=128, textvariable=self.chunk_var,
                    width=6).pack(side="left", padx=(2, 12))
        ttk.Label(row2, text="屏幕").pack(side="left")
        ttk.Combobox(row2, textvariable=self.screen_var, values=list(self.mon_labels),
                     state="readonly", width=20).pack(side="left", padx=(2, 12))
        ttk.Label(row2, text="窗口模式").pack(side="left")
        ttk.Combobox(row2, textvariable=self.mode_var, values=list(WINDOW_MODE_CHOICES),
                     state="readonly", width=24).pack(side="left", padx=(2, 12))

        row3 = ttk.Frame(settings)
        row3.pack(fill="x", padx=8, pady=4)
        ttk.Label(row3, text="（大文件建议 8~32MB；双窗口=同一块屏上左右并排两个播放窗口，"
                             "分片交错各传一半，速度约 x2；接收端自动识别，无需参数）").pack(side="left")

        status = ttk.LabelFrame(self.win, text="状态")
        status.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="请选择文件后开始")
        ttk.Label(status, textvariable=self.status_var).pack(anchor="w", padx=8, pady=2)
        self.progress = ttk.Progressbar(status, maximum=1000)
        self.progress.pack(fill="x", padx=8, pady=(0, 8))

        btns = ttk.Frame(self.win)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="开始传输", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)

        self.notice = ("提示：播放窗口会自动放到所选屏幕（双窗口=该屏左右并排两个窗口，"
                       "接收端自动识别码流数量）。请确认该屏幕正被 HDMI 采集，且窗口不被遮挡。")
        ttk.Label(self.win, text=self.notice, foreground="#666").pack(fill="x", **pad)

        logbox = ttk.LabelFrame(self.win, text="日志")
        logbox.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logbox, height=8, state="disabled", font=("Consolas", 9))
        sb = ttk.Scrollbar(logbox, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb.pack(side="right", fill="y", pady=8)

    # ---------- 事件 ----------

    def pick_file(self) -> None:
        path = filedialog.askopenfilename(title="选择要传输的文件")
        if path:
            self.file_path = path
            size = common.human_size(Path(path).stat().st_size)
            self.file_label.config(text=f"{path}（{size}）")

    def start(self) -> None:
        if self.job and self.job.is_alive():
            return
        if not self.file_path:
            messagebox.showwarning("vidtx", "请先选择文件")
            return
        if not SENDER.exists():
            messagebox.showerror("vidtx", f"未找到 {SENDER}\n请先运行: python build_native.py")
            return
        try:
            self.chunk_mb = max(common.MIN_CHUNK_MB, int(self.chunk_var.get()))
        except ValueError:
            messagebox.showwarning("vidtx", "分片大小无效")
            return
        self.fps = FPS_CHOICES[self.fps_var.get()]
        self.rounds = ROUND_CHOICES[self.rounds_var.get()]
        self.redundancy = REDUNDANCY_CHOICES[self.redun_var.get()]
        self.n_windows = WINDOW_MODE_CHOICES[self.mode_var.get()]
        self.monitor_a = self.mon_labels.get(self.screen_var.get(), 0)
        # 屏幕几何（探测失败时 w=0，双窗口分支会用兜底尺寸）
        for idx, w, h, _name in self.monitors:
            if idx == self.monitor_a:
                self.mon_w, self.mon_h = (w, h) if w else (1920, 1080)
                break
        else:
            self.mon_w, self.mon_h = 1920, 1080
        self._done: dict[int, set[str]] = {}  # 轮次 -> 已完成分片名集合

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.start_time = time.time()
        self.job = TransferJob(self)
        self.job.start()

    def stop(self) -> None:
        if self.job:
            self.job.stop()

    def on_close(self) -> None:
        self.stop()
        if self.job and self.job.is_alive():
            self.job.join(timeout=5)
        self.win.destroy()

    # ---------- 跨线程回调 ----------

    def emit(self, msg: str) -> None:
        self.queue.put(("log", msg))

    def on_chunk_done(self, rnd, idx, name, frames) -> None:
        self.queue.put(("chunk", rnd, idx, name, frames))

    def finish(self) -> None:
        self.queue.put(("finish",))

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "chunk":
                    self._update_progress(*item[1:])
                elif kind == "finish":
                    self.status_var.set("完成/已停止。服务端校验通过后文件会出现在输出目录。")
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
        except queue.Empty:
            pass
        self.win.after(200, self._poll_queue)

    def _append_log(self, msg: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _update_progress(self, rnd, idx, name, frames) -> None:
        elapsed = time.time() - self.start_time
        # 双窗口时两个进程的 file idx 各自独立，按「本轮去重后的已完成分片数」计进度
        self._done.setdefault(rnd, set()).add(name)
        n_chunks = self.job.n_chunks
        rounds = self.rounds
        frac_in_round = len(self._done[rnd]) / max(n_chunks, 1)
        if rounds:
            overall = ((rnd - 1) + frac_in_round) / rounds
        else:
            overall = frac_in_round
        overall = min(1.0, overall)
        self.progress["value"] = int(overall * 1000)
        eta = elapsed / max(overall, 1e-6) - elapsed if overall > 0.02 else 0
        eta_s = f"，预计剩余 {int(eta // 60)} 分 {int(eta % 60)} 秒" if eta > 0 else ""
        self.status_var.set(
            f"第 {rnd}{'/∞' if not rounds else '/' + str(rounds)} 轮 · "
            f"分片 {len(self._done[rnd])}/{n_chunks}（最新: {name}，{frames} 帧）· "
            f"已用 {int(elapsed // 60)} 分 {int(elapsed % 60)} 秒{eta_s}")


def main() -> None:
    app = ClientApp()
    app.win.mainloop()


if __name__ == "__main__":
    main()
