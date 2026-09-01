#!/usr/bin/env python3
"""vidtx 原生组件构建脚本（Windows / Linux 通用）。

功能：
  1. 克隆 libcimbar（固定 commit，保证 vidbar_recv 补丁兼容）；
  2. 注入 vidtx 定制的 vidbar_recv 接收端源码并注册到根 CMakeLists；
  3. 配置并构建（Windows 下走 vcpkg 工具链，自动安装依赖）；
  4. 收集 cimbar_send / vidbar_recv / cimbar 到 ./bin。

Windows 前置要求：
  - Visual Studio 2022（含 "使用 C++ 的桌面开发" 工作负载）
  - git、Python 3.10+、CMake（VS 自带亦可）
  - 首次构建 vcpkg 会编译 opencv4 等依赖，耗时可能较长（一次性）

用法：
  python build_native.py                # 完整流程
  python build_native.py --skip-clone    # 复用已存在的源码树（开发迭代）
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# Windows CI 等环境下 stdout 默认可能是 cp1252，输出中文会 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

LIBCIMBAR_REPO = "https://github.com/sz3/libcimbar.git"
LIBCIMBAR_COMMIT = "bfb0c8e471820ae493cd3694ea6bed5d5ac06c37"

ROOT = Path(__file__).resolve().parent
THIRD_PARTY = ROOT / "third_party"
SRC_TREE = THIRD_PARTY / "libcimbar"
NATIVE_DIR = ROOT / "native"
BIN_DIR = ROOT / "bin"

EXE_SUFFIX = ".exe" if platform.system() == "Windows" else ""
IS_WINDOWS = platform.system() == "Windows"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def clone_libcimbar() -> None:
    if SRC_TREE.exists():
        print(f"[+] 复用已有源码树: {SRC_TREE}")
        return
    THIRD_PARTY.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", LIBCIMBAR_REPO, SRC_TREE])
    run(["git", "-C", SRC_TREE, "checkout", LIBCIMBAR_COMMIT])
    print(f"[+] libcimbar @ {LIBCIMBAR_COMMIT[:12]}")


def inject_vidbar_recv() -> None:
    """把 vidbar_recv / vidbar_send 源码放进 libcimbar 树并注册到根 CMakeLists。"""
    for exe in ("vidbar_recv", "vidbar_send"):
        exe_dir = SRC_TREE / "src" / "exe" / exe
        exe_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(NATIVE_DIR / f"{exe}.cpp", exe_dir / f"{exe}.cpp")
        shutil.copyfile(NATIVE_DIR / f"{exe}_CMakeLists.txt", exe_dir / "CMakeLists.txt")

    root_cmake = SRC_TREE / "CMakeLists.txt"
    text = root_cmake.read_text(encoding="utf-8")
    changed = False
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if "src/exe/cimbar_send" in line:
            indent = line[: len(line) - len(line.lstrip())]
            inserts = []
            if "src/exe/vidbar_recv" not in text:
                inserts.append(f"{indent}src/exe/vidbar_recv\n")
            if "src/exe/vidbar_send" not in text:
                inserts.append(f"{indent}src/exe/vidbar_send\n")
            for j, ins in enumerate(inserts):
                lines.insert(i + 1 + j, ins)
            changed = bool(inserts)
            break
    else:
        raise RuntimeError("根 CMakeLists.txt 结构与预期不符，请手动检查")
    if changed:
        root_cmake.write_text("".join(lines), encoding="utf-8")
    print("[+] 已注入 vidbar_recv / vidbar_send")


def find_vcpkg() -> Path | None:
    """定位或克隆 vcpkg（仅 Windows 需要）。"""
    env = os.environ.get("VCPKG_ROOT")
    if env and Path(env, "scripts", "buildsystems", "vcpkg.cmake").exists():
        return Path(env)

    default = THIRD_PARTY / "vcpkg"
    if Path(default, "scripts", "buildsystems", "vcpkg.cmake").exists():
        return default

    print("[+] 未检测到 vcpkg，开始克隆（一次性，约 5 分钟）...")
    default.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "https://github.com/microsoft/vcpkg.git", default])
    bootstrap = default / ("bootstrap-vcpkg.bat" if IS_WINDOWS else "bootstrap-vcpkg.sh")
    if IS_WINDOWS:
        run([str(bootstrap), "-disableMetrics"])
    else:
        run(["bash", str(bootstrap), "-disableMetrics"])
    return default


def build() -> None:
    build_dir = SRC_TREE / "build-vidtx"
    cmake_args = ["cmake", "-S", str(SRC_TREE), "-B", str(build_dir)]

    if IS_WINDOWS:
        vcpkg = find_vcpkg()
        if vcpkg is None:
            raise RuntimeError("vcpkg 不可用")
        toolchain = vcpkg / "scripts" / "buildsystems" / "vcpkg.cmake"
        cmake_args += [
            f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
            "-DVCPKG_TARGET_TRIPLET=x64-windows",
        ]
        run(cmake_args)
        run(["cmake", "--build", str(build_dir), "--config", "Release",
             "--target", "vidbar_send", "vidbar_recv", "-j"])
    else:
        cmake_args += ["-DCMAKE_BUILD_TYPE=RelWithDebInfo"]
        run(cmake_args)
        run(["cmake", "--build", str(build_dir), "--target",
             "vidbar_send", "vidbar_recv", "-j", str(os.cpu_count() or 2)])


def collect() -> None:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    build_dir = SRC_TREE / "build-vidtx"

    found: dict[str, Path] = {}
    for name in ("vidbar_send", "vidbar_recv"):
        cands = sorted(build_dir.rglob(name + EXE_SUFFIX)) if build_dir.exists() else []
        # 过滤掉 POST_BUILD 拷贝出的 .dbg 等副产品
        cands = [c for c in cands if c.is_file() and c.suffix != ".dbg"]
        if cands:
            found[name] = cands[0]

    missing = [n for n in ("vidbar_send", "vidbar_recv") if n not in found]
    if missing:
        raise RuntimeError(f"未找到构建产物: {missing}（在 {build_dir} 中查找）")

    for name, path in found.items():
        dst = BIN_DIR / (name + EXE_SUFFIX)
        shutil.copy2(path, dst)
        print(f"[+] {dst}")

    # Windows: 收集运行时依赖 DLL
    # 两个来源取并集：
    #   1) 构建树中被 POST_BUILD 拷到各 exe 输出目录的 DLL（直接链接依赖）；
    #   2) vcpkg_installed/x64-windows/bin（兜底：ANGLE 等动态加载库不在链接表里）
    if IS_WINDOWS:
        seen: set[str] = set()
        sources = list(build_dir.rglob("*.dll"))
        vcpkg_bin = build_dir / "vcpkg_installed" / "x64-windows" / "bin"
        if vcpkg_bin.is_dir():
            sources += list(vcpkg_bin.glob("*.dll"))
        for dll in sources:
            if dll.is_file() and dll.name not in seen:
                seen.add(dll.name)
                shutil.copy2(dll, BIN_DIR / dll.name)
        print(f"[+] 已收集 {len(seen)} 个 DLL 到 bin/")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-clone", action="store_true", help="跳过克隆（复用现有源码树）")
    args = ap.parse_args()

    if not args.skip_clone:
        clone_libcimbar()
    inject_vidbar_recv()
    build()
    collect()
    print("\n完成。二进制位于:", BIN_DIR)


if __name__ == "__main__":
    main()
