# FFmpeg 安装说明

本项目默认不内置 FFmpeg 二进制文件，避免仓库和安装包过大，也避免不同系统架构不匹配。

如果启动时提示找不到 FFmpeg，请按你的系统下载，并把文件放到对应目录。

## 放置位置

### macOS Apple Silicon

适用于 M1 / M2 / M3 / M4：

```txt
runtime/ffmpeg/mac-arm64/ffmpeg
runtime/ffmpeg/mac-arm64/ffprobe
```

### macOS Intel

```txt
runtime/ffmpeg/mac-x64/ffmpeg
runtime/ffmpeg/mac-x64/ffprobe
```

### Windows x64

```txt
runtime/ffmpeg/windows-x64/ffmpeg.exe
runtime/ffmpeg/windows-x64/ffprobe.exe
```

### Linux x64

```txt
runtime/ffmpeg/linux-x64/ffmpeg
runtime/ffmpeg/linux-x64/ffprobe
```

## 下载地址

### macOS

下载地址：

```txt
https://evermeet.cx/ffmpeg/
```

下载 `ffmpeg` 和 `ffprobe`，放入对应的 `mac-arm64` 或 `mac-x64` 目录。

如果你使用 Homebrew，也可以复制本机文件：

Apple Silicon:

```bash
cp /opt/homebrew/bin/ffmpeg runtime/ffmpeg/mac-arm64/
cp /opt/homebrew/bin/ffprobe runtime/ffmpeg/mac-arm64/
chmod +x runtime/ffmpeg/mac-arm64/ffmpeg runtime/ffmpeg/mac-arm64/ffprobe
```

Intel:

```bash
cp /usr/local/bin/ffmpeg runtime/ffmpeg/mac-x64/
cp /usr/local/bin/ffprobe runtime/ffmpeg/mac-x64/
chmod +x runtime/ffmpeg/mac-x64/ffmpeg runtime/ffmpeg/mac-x64/ffprobe
```

### Windows x64

下载地址：

```txt
https://www.gyan.dev/ffmpeg/builds/
```

建议下载 `release essentials` 版本。

解压后复制：

```txt
bin/ffmpeg.exe
bin/ffprobe.exe
```

放到：

```txt
runtime/ffmpeg/windows-x64/
```

### Linux x64

下载地址：

```txt
https://johnvansickle.com/ffmpeg/
```

建议下载 `amd64 static` 版本。

解压后复制：

```txt
ffmpeg
ffprobe
```

放到：

```txt
runtime/ffmpeg/linux-x64/
```

并赋予执行权限：

```bash
chmod +x runtime/ffmpeg/linux-x64/ffmpeg runtime/ffmpeg/linux-x64/ffprobe
```

## 程序查找顺序

程序会按下面顺序查找：

1. 环境变量 `VIDEO_VARIANT_FFMPEG` / `VIDEO_VARIANT_FFPROBE`
2. 当前系统对应目录，例如 `runtime/ffmpeg/windows-x64/ffmpeg.exe`
3. 旧兼容目录 `runtime/ffmpeg/ffmpeg`
4. 系统 PATH 里的 `ffmpeg`
