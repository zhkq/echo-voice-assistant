# ECHO macOS 模块

本目录是给 **macOS** 用的独立模块。原仓库是 Windows 版，这里**不改动任何原文件**，
通过启动入口把 Windows 专属部分替换成 Mac 实现。

## 原理（一句话）

`run_mac.py` 在加载主程序前，把 `app.hotkey`、`app.runtime` 换成 Mac 版，
并给语音合成打补丁。原代码一行没动。

## 能用的功能

| 功能 | Mac | 说明 |
|---|---|---|
| 打开控制面板 | ✅ | 原生浮动框，或浏览器打开 `http://127.0.0.1:8970`（默认端口；改过见 ECHO 的 `data/echo-port.txt`） |
| 录音转文字 | ✅ | 默认 Whisper（可换 sherpa 等）；**Whisper 的中文可能输出繁体**（训练语料偏繁体），需要简体就换成 SenseVoice / Qwen3-ASR |
| 会议录音 + 纪要 | ✅ | 需要接 DSH 才能自动生成纪要 |
| 语音合成（朗读） | ✅ | 在线 edge-tts；离线兜底用 macOS `say` |
| 桌面通知 | ✅ | 通知中心（`osascript`），首次会申请通知权限 |
| 提示音 | ✅ | 用 sounddevice 播放 |
| 重启服务 | ✅ | 面板里的「重启」按钮 |
| 全局热键 | ⚠️ 可选 | 需装 `pynput` 并在系统设置里授权 |
| 右缘边条 | ✅ | AppKit + WKWebView，支持置顶、展开、收起、贴边隐藏和菜单栏入口 |
| 语音唤醒 | ⚠️ 可选 | 需装 sherpa 唤醒模型 |

## 安装（只做一次）

```bash
cd 本地语音项目
mac/setup_mac.sh
```

脚本会自动：安装 Python 3.11 和 PortAudio（用 Homebrew）→ 建虚拟环境 → 装依赖。

> 前提：已安装 [Homebrew](https://brew.sh)。

## 启动 / 停止 / 重启

```bash
mac/start_mac.sh      # 启动
mac/stop_mac.sh       # 停止
mac/restart_mac.sh    # 重启
```

启动后打开：**http://127.0.0.1:8970**（默认端口；权威值见 ECHO 的 `data/echo-port.txt`）

首次使用建议：

1. 面板 → **设置 → 模型**：下载 Whisper（默认 `base`，约 145MB）。
2. 面板 → **启动**：看各组件状态。转写、会议、面板不依赖 DSH 就能用。
3. 想让它「听懂指令并执行 / 自动写纪要」，需要本机另外跑一个 DSH。

## 全局热键（可选）

默认关闭（未装 pynput 时热键组件显示“未安装”）。需要时：

```bash
venv/bin/pip install pynput
mac/restart_mac.sh
```

然后到 **系统设置 → 隐私与安全性 → 辅助功能 / 输入监控**，把运行它的程序
（终端 / Python）勾上授权。热键在 面板 → 设置 → 语音命令 里配置。
Mac 上暂不支持耳机媒体键触发，请用组合键。
授权没给够时热键组件会显示失败原因，不会假装在线。

## 面板打开方式

Mac 支持 `sidebar`（原生浮动框）和 `browser`（浏览器）。首次安装默认使用浮动框；
升级保留现有选择，旧用户可在「设置 → 语音命令 → 仪表盘打开方式」选择 `sidebar`。

浮动框需要 macOS 12 或更新系统，使用系统自带 AppKit / WebKit，不需要额外 Python GUI 依赖。
源码位于 `mac/sidebar/`；安装脚本会在检测到 Apple Command Line Tools 时自动构建，也可手动运行：

```bash
# 如未安装编译工具，先运行 xcode-select --install
bash mac/build_sidebar.sh
```

构建结果为 `mac/sidebar/build/ECHO Sidebar.app`（本机临时签名，未公证，不入 Git）。
浮动框模式下，`panelAutoStart` 控制随 ECHO 启动，`panelStartCollapsed` 控制初始收起状态。
未构建时，手动打开面板会退回浏览器。

- 默认启动只留下 4 点宽的右侧细边；鼠标移上细边直接展开完整面板，移出面板约 0.6 秒后自动缩回。
- 鼠标返回面板会取消收回，拖拽/按住鼠标或原生确认窗口打开时暂停收回。悬停展开不主动抢键盘焦点。
- 右下角箭头仍可收成窄条，窄条也会在鼠标离开后自动隐藏；页面内容在收回后保留。
- 菜单栏 **ECHO** 可展开/收起、移到当前鼠标所在屏幕、打开浏览器或退出浮动框。
- 窗口置顶并使用屏幕可用区域，避开菜单栏与 Dock；屏幕布局变化时重新贴边。
- 浮动框按 ECHO 端口保持单实例；服务重启不会重复开窗，也不会强制改变现有展开状态。
- 展开与收起保留各自页面；会议详情和外部链接在默认浏览器打开。
- `Ctrl+Shift+E` 仍需可选 `pynput` 和系统授权，菜单栏与箭头不依赖全局热键权限。

目前展开宽度固定为 450 点，不提供 Windows 版的拖拽调宽。多显示器和全屏空间行为
依赖 macOS 的窗口管理设置，发布前应在对应设备上验证。

## 可选组件（按需再装）

### 说话人分离安装入口

「设置 → 模型 → 说话人分离」仅提供 **复制下载命令**、命令预览和官方授权链接。
先在 Hugging Face 的 segmentation-3.0 与 speaker-diarization-community-1 页面同意条件，
使用 `venv/bin/hf auth login` 登录（只读 Token）。如还没有该命令，可先运行
`venv/bin/python -m pip install huggingface-hub`。

复制内容为可直接查看的 `hf download` 命令，包含两份模型权重及 PLDA 文件的目标路径，
由用户粘贴到终端自行执行；页面按钮只复制文字，不启动下载或安装依赖。
使用说话人分离仍需在项目环境安装 pyannote.audio 4.x 与 speechbrain；
模型文件完整不代表运行依赖已安装。完成后重启 ECHO 并在会议设置中开启说话人分离。

### SenseVoice / Qwen3-ASR（中文更准）

macOS 默认用 Whisper。想换成中文更准的 SenseVoice，需先装 `funasr`——注意
**funasr 1.4 起不再自动安装 torch**，要一起装，否则会报 `No module named 'funasr'`：

```bash
venv/bin/pip install funasr modelscope torch
```

装好后在「设置 → 会议 → 会议转写模型」选择 `sensevoice`（命令引擎 `sttModel` 也可选）。
首次使用会自动从 ModelScope 下载模型（约 900MB，落 `~/.cache/modelscope`）。
已在 Apple 芯片（arm64）macOS 上实测可用：加载约 7 秒、CPU 转写正常。
模型面板的「就绪」现在同时要求模型文件与 `funasr`/`torch` 可导入，缺一即显示未就绪。

```bash
venv/bin/pip install pyannote.audio  # 会议说话人分离（会拉 torch，体积很大）
```

## 日志与排错

- 运行日志：`data/logs/echo-mac.out`
- 重启日志：`data/logs/restart-mac.out` / `.err`
- 端口被占：`mac/stop_mac.sh` 会清理默认端口（8970；以 ECHO 的 `data/echo-port.txt` 为准）上的残留进程

## 与 Windows 版的关系

Windows 版原样保留，不受影响。两边共用同一份 `app/` 代码，只有
`mac/` 这一个目录是 Mac 专用。在 Mac 上永远通过 `mac/run_mac.py` 启动；
直接跑 `python -m app.main` 会因 Windows 依赖而失败（这是预期的）。
