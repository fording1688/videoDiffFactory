# Video Variant Studio

一个独立的本地视频差异化处理工具。它用于把同一条素材生成不同视觉版本，方便做素材 A/B 测试、内容排期和创意验证。它不承诺任何平台审核结果，也不以规避检测为目标。

## 功能

- 可视化网页界面
- 批量上传 `mp4 / mov / avi / webm / m4v`
- 多个视频会先按上传顺序合并成一个视频，再进入视觉差异化处理
- 可设置生成版本数量，例如填 `3` 会生成 3 个差异化版本，再把 3 个版本合并成一个最终视频
- 开关式处理模块，不需要手动调复杂参数
- 自动随机生成安全范围内的视觉参数
- 输出 1080x1920 竖屏 MP4
- 最终下载文件会包含所有生成版本的串联合并结果
- 本地运行，上传和输出都保存在本机 `data/` 目录

## 一键启动 macOS

双击：

```bash
run_mac.command
```

或终端运行：

```bash
cd "video-variant-studio"
./run_mac.command
```

启动后会自动打开：

```txt
http://127.0.0.1:8120
```

## 一键启动 Windows

双击：

```txt
run_windows.bat
```

## FFmpeg

程序会按下面顺序查找 FFmpeg：

1. 环境变量 `VIDEO_VARIANT_FFMPEG` / `VIDEO_VARIANT_FFPROBE`
2. `runtime/ffmpeg/ffmpeg`、`runtime/ffmpeg/ffprobe`
3. 系统 PATH 里的 `ffmpeg`、`ffprobe`

如果要做成尽量不依赖系统环境的压缩包，请把对应系统的 FFmpeg 二进制文件放到：

```txt
runtime/ffmpeg/
```

## 打包成可执行文件

macOS：

```bash
./build_mac.sh
```

输出：

```txt
dist/VideoVariantStudio
```

Windows 需要在 Windows 机器上打包：

```powershell
powershell -ExecutionPolicy Bypass -File build_windows.ps1
```

输出：

```txt
dist/VideoVariantStudio.exe
```

说明：PyInstaller 一般不能跨系统打包，所以 Mac 生成 Mac 可执行文件，Windows 生成 exe。

## 推荐发布压缩包结构

```txt
VideoVariantStudio/
├── VideoVariantStudio 或 VideoVariantStudio.exe
├── runtime/
│   └── ffmpeg/
│       ├── ffmpeg / ffmpeg.exe
│       └── ffprobe / ffprobe.exe
└── data/
    ├── uploads/
    └── outputs/
```

## 处理模块说明

- 模糊动态背景：适合横屏/非 9:16 视频补成竖屏
- 随机微缩放/偏移：轻微改变构图
- 色彩微调：轻微调整饱和度、对比度、色相
- 质感噪点：添加轻微纹理
- 微变速：0.97x 到 1.03x 附近随机
- 暗角层：增加视觉聚焦
