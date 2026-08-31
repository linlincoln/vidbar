"""vidtx 公共模块：分片、manifest、命名约定。

协议概要：
  客户端把大文件切成若干 chunk，连同 manifest.json 一起用 cimbar 视频流发出。
  文件名经 zstd 头随流传给服务端，服务端按 manifest 汇聚、校验、落盘。

命名约定（全局唯一，避免多批次传输互相覆盖）：
  manifest 文件: vt_<sid>_manifest.json
  分片文件:     vt_<sid>_<序号:03d>
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

MANIFEST_TYPE = "vidtx-manifest"
MANIFEST_VERSION = 1

# 默认分片大小：16MB。
# 过大 -> 单轮周期长、接收端中途加入恢复慢、内存占用高；
# 过小 -> 每片固定开销（锁帧、喷泉码状态）占比上升。
DEFAULT_CHUNK_MB = 16
MIN_CHUNK_MB = 1

CHUNK_RE = re.compile(r"^vt_(?P<sid>[0-9a-f]{8})_(?P<idx>\d{3,})$")
MANIFEST_RE = re.compile(r"^vt_(?P<sid>[0-9a-f]{8})_manifest\.json$")

# 每帧有效载荷（cimbar mode B: 1024x1024, 155/30 RS, 7500B/帧）
BYTES_PER_FRAME = 7500


def new_sid() -> str:
    """生成一次传输会话的 8 位十六进制 id。"""
    return secrets.token_hex(4)


def chunk_name(sid: str, index: int) -> str:
    return f"vt_{sid}_{index:03d}"


def manifest_name(sid: str) -> str:
    return f"vt_{sid}_manifest.json"


def sanitize_filename(name: str, fallback: str = "received.bin") -> str:
    """清洗客户端传来的文件名，防止路径穿越与非法字符。"""
    name = os.path.basename(name.replace("\\", "/"))
    # 去掉 Windows 保留字符与控制字符
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name or name in {".", ".."}:
        name = fallback
    return name[:200]


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


@dataclass
class ChunkInfo:
    index: int
    name: str
    size: int
    sha256: str


@dataclass
class Manifest:
    type: str
    version: int
    sid: str
    filename: str          # 原始文件名（客户端侧）
    size: int              # 原始文件总字节数
    sha256: str            # 原始文件整体 sha256
    chunks: list

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


def split_file(src: str | Path, out_dir: str | Path, sid: str,
               chunk_mb: int = DEFAULT_CHUNK_MB) -> tuple[Manifest, list[Path]]:
    """把 src 切成分片文件，写入 out_dir，返回 (manifest, 分片路径列表)。

    同时计算整文件与每片的 sha256。文件只顺序读一遍。
    """
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chunk_size = max(MIN_CHUNK_MB, chunk_mb) * 1024 * 1024

    total_hash = hashlib.sha256()
    chunks: list[ChunkInfo] = []
    paths: list[Path] = []

    with open(src, "rb") as f:
        index = 0
        while True:
            piece_hash = hashlib.sha256()
            name = chunk_name(sid, index)
            path = out_dir / name
            written = 0
            with open(path, "wb") as out:
                while written < chunk_size:
                    b = f.read(min(1024 * 1024, chunk_size - written))
                    if not b:
                        break
                    out.write(b)
                    piece_hash.update(b)
                    total_hash.update(b)
                    written += len(b)
            if written == 0:
                path.unlink()  # 空文件（源文件为空）
                break
            chunks.append(ChunkInfo(index, name, written, piece_hash.hexdigest()))
            paths.append(path)
            if written < chunk_size:
                break
            index += 1

    if not chunks:  # 空文件：造一个空分片，保证至少一片
        piece_hash = hashlib.sha256()
        name = chunk_name(sid, 0)
        path = out_dir / name
        path.touch()
        chunks.append(ChunkInfo(0, name, 0, piece_hash.hexdigest()))
        paths.append(path)

    manifest = Manifest(
        type=MANIFEST_TYPE,
        version=MANIFEST_VERSION,
        sid=sid,
        filename=sanitize_filename(src.name),
        size=src.stat().st_size,
        sha256=total_hash.hexdigest(),
        chunks=[asdict(c) for c in chunks],
    )
    return manifest, paths


def parse_manifest(path: str | Path) -> Manifest | None:
    """读取并校验 manifest，非法返回 None。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("type") != MANIFEST_TYPE:
        return None
    if data.get("version", 0) != MANIFEST_VERSION:
        return None
    try:
        m = Manifest(
            type=data["type"], version=data["version"], sid=data["sid"],
            filename=data["filename"], size=int(data["size"]),
            sha256=data["sha256"], chunks=data["chunks"],
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not re.fullmatch(r"[0-9a-f]{8}", m.sid):
        return None
    if not isinstance(m.chunks, list) or not m.chunks:
        return None
    for c in m.chunks:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str):
            return None
    return m


def is_manifest_name(name: str) -> bool:
    return bool(MANIFEST_RE.fullmatch(name))


def sid_of(name: str) -> str | None:
    m = MANIFEST_RE.fullmatch(name) or CHUNK_RE.fullmatch(name)
    return m.group("sid") if m else None


def unique_dest(path: Path) -> Path:
    """若目标已存在，返回带 (1)/(2)... 后缀的新路径。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"too many duplicates for {path}")


def assemble(manifest: Manifest, chunk_paths: dict, dest: str | Path) -> bool:
    """按 manifest 顺序拼接分片到 dest，并校验整体 sha256。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    total_hash = hashlib.sha256()
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with open(tmp, "wb") as out:
        for c in manifest.chunks:
            src = chunk_paths.get(c["name"])
            if src is None:
                tmp.unlink(missing_ok=True)
                return False
            with open(src, "rb") as f:
                while True:
                    b = f.read(1024 * 1024)
                    if not b:
                        break
                    out.write(b)
                    total_hash.update(b)
    if total_hash.hexdigest() != manifest.sha256 or tmp.stat().st_size != manifest.size:
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, dest)
    return True


def verify_chunk(path: str | Path, sha256_hex: str) -> bool:
    return sha256_file(path) == sha256_hex


def clean_staging(staging_dir: str | Path, sid: str, keep: Iterable[str] = ()) -> None:
    """删除属于 sid 的所有 staging 文件（manifest 与分片）。"""
    keep = set(keep)
    staging_dir = Path(staging_dir)
    if not staging_dir.is_dir():
        return
    for p in staging_dir.iterdir():
        if sid_of(p.name) == sid and p.name not in keep:
            try:
                p.unlink()
            except OSError:
                pass


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}GB"


# 传输时间估算 ---------------------------------------------------------------

def estimate_chunk_seconds(size: int, fps: int, safety: float,
                           min_seconds: float = 6.0, overhead: float = 2.5) -> float:
    """估算单个分片需要播放的秒数。

    size 为原始字节数（压缩后通常更小，此估算偏保守）。
    safety 为冗余系数（覆盖丢帧/误码/锁帧延迟）。
    """
    frames = size / BYTES_PER_FRAME * safety
    return max(min_seconds, frames / fps + overhead)


def estimate_total_seconds(size: int, fps: int, safety: float, rounds: int) -> float:
    return estimate_chunk_seconds(size, fps, safety) * rounds + rounds * 3.0
