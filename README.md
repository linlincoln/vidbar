# vidtx — 用视频流单向传输文件

通过 USB HDMI 采集卡把一台电脑屏幕上的编码画面"拍"下来，在另一台电脑还原出文件。
基于 [libcimbar](https://github.com/sz3/libcimbar)（彩色二维条码）实现，针对采集卡链路做了工程化封装：
分片、清单、校验、断点恢复、GUI/CLI 一应俱全。

```
┌───────────────── 发送端（被采集的机器）─────────────────┐      ┌────────── 接收端（插采集卡的机器）──────────┐
│                                                          │      │                                             │
│  client.py (GUI)                                         │      │  server.py (CLI)                            │
│   ├─ 选文件 → 分片 + manifest(sha256)                     │ HDMI │   ├─ 打开采集卡 (MJPG 1080p)                │
│   └─ vidbar_send (原生) ──→ 播放窗口（彩色噪点）─────────┼──────┼→ ├─ vidbar_recv (原生) 逐帧解码              │
│            位置任意，拖到被采集的显示器上即可               │ USB  │   ├─ 喷泉码汇聚 → 分片还原                  │
│                                                          │      │   └─ 全部到齐 → sha256 校验 → 落盘输出      │
└──────────────────────────────────────────────────────────┘      └─────────────────────────────────────────────┘
```

## 特性

- **单向、免同步**：喷泉码（Wirehair）+ 前向纠错，接收端可在视频开始后任意时刻加入，
  错过前半段也能在下一轮循环补齐。
- **高吞吐**：1080p@60fps 下有效速率约 **90–280 KB/s**（不可压缩数据 137 KB/s 实测）；
  可压缩数据经 zstd 预压缩，实测文本达 **7.8 MB/s** 等效速率。
- **大文件**：默认 4MB 分片，可调至 128MB；逐片校验，缺哪片补哪片。
- **任意窗口位置**：播放窗口是独立 GLFW 窗口，拖到哪里都行，不依赖屏幕分辨率。
- **完整性保证**：分片级 Reed-Solomon + 文件级 sha256，校验不过不入输出目录。
- **跨平台**：Windows（主要目标）/ Linux 均可构建运行。

## 目录结构

```
vidtx/
├── client.py            # 发送端 GUI（选文件、调参数、启停播放窗口）
├── server.py            # 接收端 CLI（监测采集卡、解码、落盘）
├── common.py            # 分片 / manifest / sha256 / 速率估算
├── build_native.py      # 一键构建原生组件（克隆 libcimbar + CMake）
├── test_loopback.py     # 回环集成测试（无需采集卡，用 MJPEG 视频模拟信道）
├── native/
│   ├── vidbar_send.cpp  # 发送端：编码 + GLFW 播放窗口 / 无头视频输出
│   └── vidbar_recv.cpp  # 接收端：采集卡逐帧解码
└── bin/                 # 构建产物（vidbar_send / vidbar_recv）
```

## 构建原生组件

原生组件是 C++（OpenCV/GLFW），**必须编译**，且 libcimbar 官方未发布 Windows 预编译版。
没有本地编译环境时，用下面的云端构建；有环境时用本地构建。

### 方式一：云端构建（推荐，本地零安装）

只需一个 GitHub 账号和浏览器，在 GitHub 的服务器上编译，下载即用：

1. 在 github.com 新建一个 **Public** 仓库（Public 免费；Private 会消耗配额且
   Windows runner 按 2 倍计时）；
2. 仓库页面上传文件：**进入 vidtx 文件夹后全选其中的内容**（务必包含 `.github` 隐藏
   文件夹）拖到上传区——注意是拖 vidtx 里的内容，不是拖 vidtx 文件夹本身，否则工作流
   文件不会生效；
3. 上传完成后 Actions 页会自动开始 `build-windows`（也可在 Actions 页手动 Run
   workflow）；
4. 首次构建约 1~2 小时（编译 OpenCV、ANGLE 等），之后有缓存约 10 分钟；
5. 构建完成后点进该次运行 → Artifacts → 下载 `vidtx-win64-bin.zip` → 解压到本地
   vidtx 目录的 `bin/`（即 `vidtx/bin/vidbar_send.exe` 等）；
6. 之后两台机器只需 Python 3.10+（Windows 自带或官网安装），`python server.py` /
   `python client.py` 即可使用。

### 方式二：本地构建

#### Windows 前置要求

- Visual Studio 2022（含"使用 C++ 的桌面开发"工作负载）
- git、Python 3.10+、CMake（VS 自带亦可）
- 首次构建会经 vcpkg 编译 opencv4 等依赖，耗时较长（一次性）

#### 构建命令

```bash
python build_native.py                  # 完整流程：克隆 libcimbar → 注入 vidbar 源码 → CMake 构建
python build_native.py --skip-clone     # 复用已有源码树，开发迭代用
```

产物为 `bin/vidbar_send.exe`、`bin/vidbar_recv.exe`。

## 使用

### 1. 接收端（插采集卡的机器）

```bash
python server.py                        # 默认设备 0，输出到 ./received
python server.py -i 1 -o D:\incoming    # 指定设备与输出目录
python server.py --list-devices         # 先看看有哪些采集设备
```

参数速查：

| 参数 | 默认 | 说明 |
|---|---|---|
| `-o/--output` | `./received` | 文件输出目录 |
| `-i/--device` | `0` | 采集设备索引 |
| `--api` | Windows `dshow` | 后端，可选 `dshow/msmf/v4l2/any` |
| `--fourcc` | `MJPG` | 像素格式；传空字符串跳过设置 |
| `-F/--fps` | Win 60 / 其他 30 | 请求采集帧率 |
| `-w/--width` `--height` | 1920 1080 | 请求分辨率 |
| `--staging` | `<输出>/.vidtx-staging` | 未完成分片的暂存目录 |

启动后进入"等待信号"状态，检测到 vidtx 画面即自动锁定并开始解码，无需人工干预。
支持的文件全部到齐后输出并校验 sha256，控制台会打印最终结果。

### 2. 发送端（被采集的机器）

```bash
python client.py
```

操作流程：

1. **选择文件**
2. 按需调整传输设置：
   - **播放帧率**：15 / 30 / 60，显示器与采集卡允许时选 60 最快；
   - **轮数**：1 轮最快（要求接收端从头开始）；2 轮推荐（允许中途加入）；3 轮最稳；
     无限循环则一直播到手动停止；
   - **冗余**：1.3x 快速 / 1.6x 标准 / 2.2x 保守，信道越差调越高；
   - **分片大小**：大文件建议 8~32MB；分片越小，中途加入的接收端恢复越快。
3. 点击 **开始传输**，把弹出的彩色噪点播放窗口拖到被 HDMI 采集的显示器上，位置任意。
4. 控制面板实时显示轮数 / 分片进度 / 预计剩余时间。

### 3. 回环测试（无需硬件）

```bash
python test_loopback.py            # 生成 2MB 随机数据做最严苛的不可压缩压测
python test_loopback.py 8          # 8MB 随机数据
python test_loopback.py some/file  # 用真实文件（可压缩，通常快得多）
```

链路：原文件 → 分片 → `vidbar_send -o` 输出 MJPEG 视频（模拟采集卡抓到的压缩失真画面）
→ `server.py --source` 解码 → sha256 比对。

## 实测数据（回环，30fps，冗余 1.6x，2 轮）

| 场景 | 数据量 | 结果 | 有效速率 |
|---|---|---|---|
| 随机数据（不可压缩） | 2.0 MB | sha256 一致 | 137 KB/s |
| 高度可压缩文本 | 11.2 MB | sha256 一致 | 7.8 MB/s（zstd 压缩后仅 60 帧） |

不可压缩数据体现的是信道真实容量；日常文件（文档、代码、安装包）多数可压缩，
实际等待时间介于两者之间。

## 工作原理

1. **分片**（client 侧 `common.split_file`）：大文件切成若干分片，逐片算 sha256，
   生成 manifest（JSON，含文件名、大小、总哈希、分片列表）。
2. **编码**（`vidbar_send`，改自 libcimbar `cimbar_send`）：
   - zstd 压缩 → Reed-Solomon 前向纠错 → 喷泉码（Wirehair）切块；
   - 每块映射为一张 cimbar "彩色噪点"图（1088×1088）；
   - manifest 与每个分片各自分配独立的 7bit `encode_id`，避免多会话相互污染；
   - 播放窗口按设定帧率轮播，可多轮循环。
3. **采集**：USB HDMI 采集卡把屏幕画面当作摄像头（MJPG 1080p 60fps）。
4. **解码**（`vidbar_recv`，改自 libcimbar `cimbar_recv`）：
   - OpenCV 抓帧 → 定位/透视校正 → 解出喷泉码块 → 汇聚还原分片；
   - 支持中途加入：喷泉码只要收满任意 N 个块即可还原，不依赖起点；
   - 色彩校正 `-c` 可调（2=自动，0=关闭），用于适配不同采集链路。
5. **汇聚**（server 侧）：分片到齐 → 重组 → sha256 校验 → 原子落盘到输出目录；
   校验失败自动清理暂存，不会输出损坏文件。

## 常见问题

- **接收端一直"等待信号"**：确认播放窗口在被采集的那块显示器上、未被遮挡；
  `--list-devices` 核对设备索引；采集卡建议设为 MJPG 模式（YUY2 带宽不够 60fps）。
- **速率远低于预期**：把播放帧率调到 60（显示器和采集卡都支持的前提下）；
  冗余从 1.6x 降到 1.3x；确认采集分辨率 ≥1920×1080。
- **偶发分片校验失败**：提高冗余到 2.2x，或增加轮数；HDMI 线/USB 口接触不良也会导致。
- **想从视频中途开始接收**：喷泉码天然支持，把轮数设为 2 轮以上即可，无需额外操作。

## 依赖

- Python 3.10+（client/server 仅用标准库；测试脚本同）
- libcimbar（构建脚本自动克隆固定 commit `bfb0c8e`）及其依赖
  （OpenCV、GLFW、zstd、Wirehair 等，经 vcpkg/CMake 自动处理）
