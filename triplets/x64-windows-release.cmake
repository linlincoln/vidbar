# x64-windows-release：同官方 x64-windows（动态 CRT/动态库），
# 但只编译 Release —— 跳过每个依赖的 debug 版本。
# vcpkg 依赖里 ANGLE 的 debug 编译要 1 小时以上，而 vidtx 只发布 Release 二进制，
# debug 变体纯属浪费 CI 时间（曾导致 Windows 云编译"卡死"假象）。
set(VCPKG_TARGET_ARCHITECTURE x64)
set(VCPKG_CRT_LINKAGE dynamic)
set(VCPKG_LIBRARY_LINKAGE dynamic)
set(VCPKG_BUILD_TYPE release)
