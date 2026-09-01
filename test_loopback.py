#!/usr/bin/env python3
"""vidtx 回环集成测试（无需采集卡硬件）。

链路模拟：
  原始文件 --common.split_file--> 分片+manifest
    --vidbar_send -o--> MJPEG 视频（模拟采集卡从屏幕抓到的画面，含压缩失真）
    --server.py --source--> 解码分片并汇聚校验
    --> 输出文件，与原始文件逐字节比对（sha256）

用法：
  python test_loopback.py            # 默认 2MB 随机数据
  python test_loopback.py 8          # 8MB 随机数据
  python test_loopback.py path/file  # 用真实文件测试（推荐：混合压缩率）

随机数据不可压缩，是最严苛的信道压力测试；真实文件通常更快。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import common

ROOT = Path(__file__).resolve().parent
IS_WIN = sys.platform == "win32"
EXE = ".exe" if IS_WIN else ""
SENDER = ROOT / "bin" / ("vidbar_send" + EXE)


def run_one_case(src: Path, workdir: Path, fps: int, rounds: int,
                 redundancy: float, chunk_mb: int, timeout: int = 1800) -> bool:
    """跑一次完整回环，返回是否逐字节一致。"""
    sid = common.new_sid()
    staging = workdir / "chunks"
    outdir = workdir / "received"

    t0 = time.time()
    print(f"[1/4] 分片: {src.name} ({common.human_size(src.stat().st_size)})")
    manifest, chunk_paths = common.split_file(src, staging, sid, chunk_mb=chunk_mb)
    manifest_path = staging / common.manifest_name(sid)
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    print(f"     sid={sid}，{len(chunk_paths)} 个分片")

    print(f"[2/4] 编码为 MJPEG 视频（{fps}fps，{rounds or '∞'} 轮，冗余 {redundancy}x）")
    video = workdir / "loop.avi"  # MJPG 必须配 AVI 容器（mp4 不支持该编码）
    files = [str(manifest_path)] + [str(p) for p in chunk_paths]
    base = int(sid[:2], 16) & 0x7F
    cmd = [str(SENDER), "-o", str(video), "-f", str(fps), "-r", str(rounds),
           "-R", str(redundancy), "-b", str(base)] + files
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not video.exists():
        print(f"[!] vidbar_send 失败: {p.stderr[-500:]}")
        return False
    print(f"     视频已生成: {video.name} "
          f"({common.human_size(video.stat().st_size)})，用时 {time.time() - t0:.1f}s")

    print("[3/4] 启动 server.py 从视频解码")
    t1 = time.time()
    srv = subprocess.run(
        [sys.executable, str(ROOT / "server.py"),
         "--source", str(video), "-o", str(outdir)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    print(srv.stdout)
    if srv.returncode != 0:
        print(f"[!] server.py 异常退出: {srv.stderr[-800:]}")

    print("[4/4] 校验结果")
    dest = outdir / common.sanitize_filename(src.name)
    if not dest.exists():
        print(f"[✗] 未收到文件: {dest}")
        return False
    got = common.sha256_file(dest)
    want = common.sha256_file(src)
    dt = time.time() - t1
    if got != want:
        print(f"[✗] sha256 不一致!\n     期望 {want}\n     实际 {got}")
        return False
    speed = src.stat().st_size / dt
    print(f"[✓] 回环测试通过: {src.name} {common.human_size(src.stat().st_size)} "
          f"sha256 一致 · 解码耗时 {dt:.1f}s · 平均 {common.human_size(int(speed))}/s")
    return True


def run_dual_case(src: Path, workdir: Path, fps: int, rounds: int,
                  redundancy: float, chunk_mb: int, timeout: int = 1800) -> bool:
    """同屏双码流回环：窗口A/B 各渲染一段视频，水平拼接成一个画面后以 --split 2 解码。"""
    sid = common.new_sid()
    staging = workdir / "chunks"
    outdir = workdir / "received"

    t0 = time.time()
    print(f"[1/5] 分片: {src.name} ({common.human_size(src.stat().st_size)})")
    manifest, chunk_paths = common.split_file(src, staging, sid, chunk_mb=chunk_mb)
    manifest_path = staging / common.manifest_name(sid)
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    print(f"     sid={sid}，{len(chunk_paths)} 个分片")

    files_a = [str(manifest_path)] + [str(p) for p in chunk_paths[0::2]]
    files_b = [str(p) for p in chunk_paths[1::2]]
    if not files_b:
        print("[!] 分片数不足以拆两路，请用更小的 --chunk-mb 重试")
        return False
    base = int(sid[:2], 16) & 0x7F

    print(f"[2/5] 分别渲染窗口A/B 视频（{fps}fps，{rounds or '∞'} 轮，冗余 {redundancy}x）")
    videos = []
    for tag, files, bid in (("A", files_a, base), ("B", files_b, (base + 64) & 0x7F)):
        video = workdir / f"loop_{tag}.avi"
        cmd = [str(SENDER), "-o", str(video), "-f", str(fps), "-r", str(rounds),
               "-R", str(redundancy), "-b", str(bid)] + files
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0 or not video.exists():
            print(f"[!] vidbar_send（窗口{tag}）失败: {p.stderr[-500:]}")
            return False
        videos.append(video)
        print(f"     窗口{tag}: {video.name} ({common.human_size(video.stat().st_size)})")

    print("[3/5] 水平拼接为同屏双码流画面")
    combined = workdir / "loop_dual.avi"
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-i", str(videos[0]), "-i", str(videos[1]),
           "-filter_complex", "[0:v][1:v]hstack=inputs=2",
           "-c:v", "mjpeg", "-q:v", "3", str(combined)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not combined.exists():
        print(f"[!] ffmpeg 拼接失败: {p.stderr[-500:]}")
        return False

    print("[4/5] 启动 server.py 从合成画面解码（自动识别双码流，不传 --split）")
    t1 = time.time()
    srv = subprocess.run(
        [sys.executable, str(ROOT / "server.py"),
         "--source", str(combined), "-o", str(outdir)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    print(srv.stdout)
    if srv.returncode != 0:
        print(f"[!] server.py 异常退出: {srv.stderr[-800:]}")

    print("[5/5] 校验结果")
    dest = outdir / common.sanitize_filename(src.name)
    if not dest.exists():
        print(f"[✗] 未收到文件: {dest}")
        return False
    got = common.sha256_file(dest)
    want = common.sha256_file(src)
    dt = time.time() - t1
    if got != want:
        print(f"[✗] sha256 不一致!\n     期望 {want}\n     实际 {got}")
        return False
    speed = src.stat().st_size / dt
    print(f"[✓] 双码流回环测试通过: {src.name} {common.human_size(src.stat().st_size)} "
          f"sha256 一致 · 解码耗时 {dt:.1f}s · 平均 {common.human_size(int(speed))}/s")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="vidtx 回环集成测试")
    ap.add_argument("target", nargs="?", default=None,
                    help="测试文件路径；省略则生成 2MB 随机数据。也可传纯数字表示随机数据 MB 数")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--redundancy", type=float, default=1.6)
    ap.add_argument("--chunk-mb", type=int, default=1)
    ap.add_argument("--dual", action="store_true",
                    help="同屏双码流模式：渲染左右两路拼成一个画面，以 --split 2 解码")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    if not SENDER.exists():
        print(f"未找到 {SENDER}，请先运行: python build_native.py", file=sys.stderr)
        sys.exit(1)

    workdir = Path(tempfile.mkdtemp(prefix="vidtx-loopback-"))
    try:
        if args.target and not args.target.isdigit():
            src = Path(args.target).resolve()
            if not src.is_file():
                print(f"文件不存在: {src}", file=sys.stderr)
                sys.exit(1)
        else:
            size_mb = int(args.target) if args.target else 2
            src = workdir / "random.bin"
            print(f"生成 {size_mb}MB 随机测试数据 ...")
            with open(src, "wb") as f:
                left = size_mb * 1024 * 1024
                while left > 0:
                    n = min(left, 1024 * 1024)
                    f.write(os.urandom(n))
                    left -= n

        if args.dual:
            ok = run_dual_case(src, workdir, args.fps, args.rounds,
                               args.redundancy, args.chunk_mb)
        else:
            ok = run_one_case(src, workdir, args.fps, args.rounds,
                              args.redundancy, args.chunk_mb)
        sys.exit(0 if ok else 1)
    finally:
        if args.keep:
            print(f"临时目录保留: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
