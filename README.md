# Video Variant Studio

一个独立的本地视频差异化处理工具。它用于把同一条素材生成不同视觉版本，方便做素材 A/B 测试、内容排期和创意验证。它不承诺任何平台审核结果，也不以规避检测为目标。

## 功能

- 可视化网页界面
- 批量上传 `mp4 / mov / avi / webm / m4v`
- 多个视频会分别独立处理，不会先合并
- 可设置生成版本数量，例如上传 5 个视频且填 `3`，会为每个视频生成 3 个独立版本
- 多文件任务支持自定义线程数并发处理，默认同时跑 `3` 个视频，其余自动排队
- 开关式处理模块，不需要手动调复杂参数
- 自动随机生成安全范围内的视觉参数
- 输出 1080x1920 竖屏 MP4
- 处理视频和 Short Drama 成品直接保存到指定输出目录，不在任务卡重复提供下载
- Short Drama 支持根目录批量模式：每个一级子文件夹自动作为一组，输出到目标根目录下的同名文件夹，并复制源目录中的 TXT 文件
- Short Drama 成品使用 `剧ID_001.mp4` 连续编号；同目录再次处理会从已有最大序号继续，避免覆盖
- 根目录批量完成后生成 `发布批次.txt` 和 `发布批次.json`，每批优先从 5 个不同剧集各选 1 条且不重复复制视频
- 处理视频和 Short Drama 可把中文对白翻译成英文，以黄色粗体和黑色描边直接烧录到画面，不生成平台字幕轨道
- 正在排队或处理中的任务支持手动停止，会尝试终止对应 FFmpeg 子进程
- 独立合并多个视频，按选择顺序输出一个 MP4
- 独立切分大视频，可输入类似 `50-56` 的随机秒数范围，按顺序切完整个视频
- 支持输入 YouTube / Facebook / Instagram / TikTok 等公开视频分享链接下载到本地
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

页面里可以直接设置“线程数”，默认 `3`。普通电脑建议保持 `3`，高配机器可调到 `6-8`。

如果要限制客户机器的最高线程数，可在启动前设置环境变量；默认最高允许 `8`：

```bash
VIDEO_VARIANT_MAX_WORKERS=6 ./run_mac.command
```

## 一键启动 Windows

双击：

```txt
run_windows.bat
```

## 升级与百度网盘授权

源码版和打包版都把配置、授权及运行缓存保存在项目外的用户数据目录，覆盖源码或重新下载项目不会丢失百度授权。旧版项目目录中的 `data/baidu_pan.json` 会在首次启动时自动迁移。

打包可执行版的授权保存在用户目录，不随程序升级包一起替换：

- Windows：`C:\Users\当前用户\VideoVariantStudio\baidu_pan.json`
- macOS：`~/Movies/VideoVariantStudio/baidu_pan.json`

也可以通过环境变量 `VIDEO_VARIANT_DATA_DIR` 指定其他持久目录。

## 合并和切分

页面上新增了两个独立工具区：

- `合并视频`：选择两个或多个视频，系统会按选择顺序标准化并合并成一个 MP4。
- `切分视频`：选择一个大视频，输入随机秒数范围，例如 `50-56`，系统会从头到尾按 50 到 56 秒之间的随机长度依次切分，并提供每个片段下载和 ZIP 整包下载。

对应接口：

```bash
POST /api/merge
POST /api/split
POST /api/download-url
GET /api/download/{task_id}
GET /api/download/{task_id}/variants/{index}
GET /api/download/{task_id}/package
```

注意：为了完整切完视频，最后一个片段可能短于输入范围。

## 链接下载

项目集成了 `yt-dlp`，可以把主流平台公开视频分享链接下载到本地 `data/uploads/`。安装依赖后可直接命令行调用：

```bash
python scripts/download_url.py "https://www.youtube.com/watch?v=xxxx"
```

需要登录态的平台，例如部分 Facebook / Instagram 链接，可以让 `yt-dlp` 读取浏览器 cookies：

```bash
python scripts/download_url.py "https://www.instagram.com/reel/xxxx/" --cookies-browser chrome
```

也可以通过后端接口调用：

```bash
curl -X POST http://127.0.0.1:8120/api/download-url \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.youtube.com/watch?v=xxxx"}'
```

返回里会包含 `filepath`、`filename`、`download_url`、`title`、`duration` 等字段，后续处理可以直接使用 `filepath`。

## 个人百度网盘自动上传

左侧打开 `百度网盘`，填写百度网盘开放平台应用的 `App Key`、`Secret Key` 和目标目录，保存后完成 OAuth 授权即可。

建议目标目录：

```txt
/apps/你的应用名称/VideoVariantStudio
```

使用流程：

1. 在百度网盘开放平台创建应用，取得 App Key 和 Secret Key。
2. 在本工具的“百度网盘”页面保存配置。
3. 点击“打开百度授权”，登录自己的个人百度网盘并同意授权。
4. 将授权页面显示的 code 粘贴到工具中，点击“提交授权码”。
5. 开启“处理完成后自动上传”。

处理视频、Short Drama 及 Reel 多版本任务完成后，会由独立上传线程把最终文件传到目标目录。上传失败不会删除或影响本地成品。大文件按 4 MiB 分片上传，访问令牌过期后会使用 refresh token 自动刷新。

配置和令牌保存在本机用户数据目录的 `baidu_pan.json`，文件权限会尽量设为仅当前用户可读写。Secret Key 和令牌不会返回给前端状态接口。个人百度网盘开放平台通常只能访问应用自己的 `/apps/应用名称/` 目录。

## FFmpeg

程序会按下面顺序查找 FFmpeg：

1. 环境变量 `VIDEO_VARIANT_FFMPEG` / `VIDEO_VARIANT_FFPROBE`
2. 当前系统对应的内置目录，例如 `runtime/ffmpeg/mac-arm64/ffmpeg`
3. 兼容旧结构：`runtime/ffmpeg/ffmpeg`、`runtime/ffmpeg/ffprobe`
4. 系统 PATH 里的 `ffmpeg`、`ffprobe`

仓库默认不内置 FFmpeg 二进制文件。务必按自己的系统下载后放进去：

```txt
runtime/ffmpeg/mac-arm64/
runtime/ffmpeg/mac-x64/
runtime/ffmpeg/windows-x64/
runtime/ffmpeg/linux-x64/
```

详细说明见：

```txt
runtime/ffmpeg/README.md
```

## 模块化视频增强流水线

`app.video_augmentor.VideoAugmentor` 可在现有任务流程或独立脚本中调用。它将空间、颜色、画质、
时域、音频和合成操作组织成一次 FFmpeg 滤镜图，减少重复编解码；每次输出还会生成
`*.augmentation.json` 参数清单，用相同 `seed` 可复现 A/B 测试版本。

```python
from app.video_augmentor import VideoAugmentor

parameters = {
    "profile": "balanced",                 # light / balanced / strong
    "seed": 20260729,                       # None 表示每次随机
    "spatial": {
        "crop_percent": [0.02, 0.05],
        "background": {
            "enabled": True,
            "mode": "gaussian",            # gaussian / box
            "blur_sigma": 24,
            "foreground_scale": 0.90,
        },
        "breathing_zoom": {"enabled": True, "amplitude": 0.018, "period_seconds": 6},
    },
    "temporal": {
        "speed": [1.01, 1.05],              # atempo 保持音高
        "target_fps": 60,
        "fps_mode": "interpolate",         # interpolate 光流插帧；resample 普通重采样
    },
    "audio": {
        "pitch_semitones": [-0.3, 0.3],
        "eq": {"enabled": True, "bands": 5, "highpass_hz": 70, "lowpass_hz": 15500},
        "stereo": {"enabled": True, "width": 1.08, "haas_delay_ms": [6, 14]},
        "reverb": {"enabled": True, "wet": 0.035},
        "layering": {
            "pink_noise": {"enabled": True, "volume_db": -42},
            "ambient_path": "/path/to/room-tone.wav",
            "ambient_volume_db": -40,
            "bgm_path": "/path/to/bgm.mp3",
            "bgm_volume_db": -24,
            "bgm_fade_in": 1.5,
            "bgm_fade_out": 2.0,
        },
    },
    "composition": {
        "watermark": {"path": "/path/to/logo.png", "opacity": 0.6, "position": "top_right"},
        "pip": {"path": "/path/to/pip.mp4", "start": 2, "end": 8, "position": "bottom_right"},
        "intro": {"path": "/path/to/intro.mp4", "transition": 0.5},
        "outro": {"path": "/path/to/outro.mp4", "transition": 0.5},
    },
}

augmentor = VideoAugmentor(parameters)
plan = augmentor.process("input.mp4", "output.mp4")

# 调试/任务队列集成时可只构建命令，不执行：
command = augmentor.process("input.mp4", "output.mp4", dry_run=True)
```

声音模块也可以脱离视频流水线单独使用：

```python
from app.audio_processor import AudioProcessor

audio = AudioProcessor({
    "seed": 20260729,
    "speed": [1.01, 1.05],
    "pitch_semitones": [-0.3, 0.3],
    "eq": {"bands": 3},
    "reverb": {"wet": 0.03},
})

audio.process("input.mp4", "enhanced_audio.m4a")
ffmpeg_command = audio.process("input.mp4", "enhanced_audio.m4a", dry_run=True)
print(" ".join(ffmpeg_command))
```

完整默认参数及逐项注释见 `app/video_augmentor.py` 中的 `DEFAULT_PARAMETERS`。光流插帧比普通
重采样慢很多，批量生成 60fps 版本时建议先用短片段评估耗时。

## 常规视频编辑与隐私元数据清理

`app.video_processor.VideoProcessor` 用于可审计的常规剪辑、局部字幕区域处理、品牌合成、
帧率转换和转码。默认清除输入文件携带的隐私元数据，并写入真实的项目名称与版本信息。

```python
from app.video_processor import VideoProcessor

processor = VideoProcessor({
    "metadata": {
        "strip_all": True,
        "project_name": "MyVideoProject",
        "project_version": "2.3.0",
        "title": "Authorized edited master",
        "comment": "Edited from licensed source material",
    },
    "region": {
        "enabled": True,
        "mode": "blur",             # blur 或 crop_bottom
        "x": 0.0,
        "y": 0.85,
        "width": 1.0,
        "height": 0.15,
        "blur_sigma": 18,
        "crop_bottom_ratio": 0.15,
    },
    "composition": {
        "style_overlay": {
            "enabled": True,
            "mode": "film",          # film / warm / cool / contrast / vignette
            "opacity": 0.18,
            "grain_strength": 2.0,
        },
        "watermark": {
            "path": "/path/to/brand.png",
            "opacity": 0.25,
            "width_ratio": 0.18,
            "position": "top_right",
            "margin": 24,
        },
        "pip": {
            "enabled": False,         # 默认关闭独立区域画中画
            "path": "/path/to/pip.mp4",
            "width_ratio": 0.30,
            "position": "bottom_right",
            "start": 2.0,
            "end": 8.0,
        },
        "border": {"enabled": True, "width": 4, "color": "white@0.9"},
        "intro": {"path": "/path/to/brand-intro.mp4", "duration": 0.6, "fade": 0.2},
        "outro": {"path": "/path/to/brand-outro.mp4", "duration": 0.6, "fade": 0.2},
    },
    "temporal": {
        "trim_head_seconds": 0.2,
        "trim_tail_seconds": 0.3,
        # 也可以用 start_time / end_time 指定绝对时间点。
        "target_fps": 30,
    },
    "quality": {
        "sharpen": 0.30,
        "contrast": 1.04,
        "brightness": 0.0,
        "saturation": 1.03,
        "color_smoothing": 0.8,
    },
})

plan = processor.process("input.mp4", "edited_output.mp4")
command = processor.process("input.mp4", "edited_output.mp4", dry_run=True)
```

剪辑区间会同时应用到视频和音频，并分别重置时间戳，因此首尾剪辑后仍保持音画同步。
全屏风格层由主画面自身生成调色版本后按 alpha 混合，不需要额外的不可见视频素材。

网页高级面板中的固定水印、画中画、环境音和 BGM 路径保存在用户数据目录的
`advanced_pipeline.json`，不写入源码项目。macOS 默认位置为：

```txt
~/Movies/VideoVariantStudio/advanced_pipeline.json
```

## 自动化批量处理

`scripts/batch_video_pipeline.py` 会扫描 `.mp4 / .mov / .mkv`，为每个视频生成独立的随机
裁剪、变速、首尾剪辑、色彩与帧率参数，再通过一个 FFmpeg filtergraph 完成视频、音频、
区域模糊、品牌合成和元数据处理。单个文件失败只会写入 `batch_report.json`，不会中止批次。

```bash
# 默认输出 30fps，并发处理 3 个视频
python scripts/batch_video_pipeline.py ./input ./output --workers 3

# 30/60fps 随机选择、光流插帧、底部 12% 模糊、可见细边框
python scripts/batch_video_pipeline.py ./input ./output \
  --workers 3 --fps random --fps-mode interpolate \
  --blur-bottom --border

# 加入品牌素材、授权环境音和 BGM
python scripts/batch_video_pipeline.py ./input ./output \
  --watermark ./assets/brand.png \
  --pip ./assets/pip.mp4 \
  --ambient ./assets/room-tone.wav \
  --bgm ./assets/licensed-bgm.mp3

# 复现实验批次，或只检查生成的 FFmpeg 命令
python scripts/batch_video_pipeline.py ./input ./output --seed 20260729 --dry-run
```

完整命令行参数：

```bash
python scripts/batch_video_pipeline.py --help
```

## 编码性能与硬件加速

`VideoProcessor` 与 `VideoAugmentor` 默认使用 `video_codec=auto`。启动处理时会执行一帧真实
测试编码，并按平台尝试 VideoToolbox、NVENC、QSV 或 AMF；测试失败自动降级到 `libx264`。
检测结果会缓存，单次运行不会反复探测。

```python
"output": {
    "video_codec": "auto",       # 也可指定 h264_nvenc / h264_qsv / h264_videotoolbox
    "codec_family": "h264",      # h264 或 hevc
    "video_bitrate": "4M",       # 硬件编码目标码率
    "preset": "ultrafast",       # CPU仅允许 ultrafast / superfast
    "crf": 25,                    # 自动限制在22-26；GPU侧作为质量值
    "pixel_format": "yuv420p",
}
```

所有输入与输出命令均显式使用 `-threads 0`、`-filter_threads 0` 和
`-filter_complex_threads 0`，让 FFmpeg 自动利用可用 CPU 核心。光流插帧、
动态缩放、去噪和复杂模糊仍由滤镜链计算；若追求速度，优先使用30fps与`resample`模式。

常用下载地址：

- macOS Apple Silicon：下载 [evermeet.cx FFmpeg macOS builds](https://evermeet.cx/ffmpeg/)，或使用 Homebrew 后复制 `/opt/homebrew/bin/ffmpeg` 和 `/opt/homebrew/bin/ffprobe`
- macOS Intel：下载 [evermeet.cx FFmpeg macOS builds](https://evermeet.cx/ffmpeg/)，或使用 Homebrew 后复制 `/usr/local/bin/ffmpeg` 和 `/usr/local/bin/ffprobe`
- Windows x64：下载 [gyan.dev FFmpeg release builds](https://www.gyan.dev/ffmpeg/builds/) 的 `release essentials` 压缩包，解压后复制 `bin/ffmpeg.exe` 和 `bin/ffprobe.exe`
- Linux x64：下载 [johnvansickle.com FFmpeg static builds](https://johnvansickle.com/ffmpeg/) 的 `amd64 static` 压缩包，解压后复制 `ffmpeg` 和 `ffprobe`

macOS / Linux 复制后需要给执行权限：

```bash
chmod +x runtime/ffmpeg/mac-arm64/ffmpeg runtime/ffmpeg/mac-arm64/ffprobe
chmod +x runtime/ffmpeg/mac-x64/ffmpeg runtime/ffmpeg/mac-x64/ffprobe
chmod +x runtime/ffmpeg/linux-x64/ffmpeg runtime/ffmpeg/linux-x64/ffprobe
```

## 导出体积控制

默认导出参数已调整为更小文件：

```txt
分辨率：480x854 竖屏
格式：MP4
帧率：30 fps
视频编码：H.264
视频码率：低码率 CRF 30
音频：AAC 192k / 44100 Hz / stereo
编码速度：ultrafast
```

如果要改回 720P 竖屏，可在启动前设置：

```bash
VIDEO_VARIANT_EXPORT_WIDTH=720 VIDEO_VARIANT_EXPORT_HEIGHT=1280 ./run_mac.command
```

也可以手动调整压缩强度，数值越大文件越小、画质越低：

```bash
VIDEO_VARIANT_EXPORT_CRF=32 ./run_mac.command
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
│       ├── README.md
│       └── 当前系统目录/
│           ├── ffmpeg / ffmpeg.exe
│           └── ffprobe / ffprobe.exe
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
- 中线划痕：在画面中轴叠加轻微胶片划痕/折痕线
- 动态扫光：每个版本随机使用可见但不抢眼的移动细线，主线约 3-6px，外侧只有很轻的柔光
- 胶片颗粒：每个版本随机使用约 3%-15% 强度、small / medium 颗粒大小、约 5%-20% 透明度，并保持动态随机变化
