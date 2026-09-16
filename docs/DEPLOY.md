# 部署指南（Windows）

从零把 ECHO 跑起来。全程只需要 Windows 10/11 + Python；**执行指令 / 生成纪要**这一步需要另外装
[DSH Desktop](https://github.com/NeoBigZhou)（可选，不装也能用转写、会议、面板）。

---

## 0. 前置

| 项 | 要求 | 说明 |
|---|---|---|
| 系统 | Windows 10/11 x64 | 麦克风、Edge WebView2 Runtime（边条要用，Win11 自带） |
| Python | **3.11**（3.12+ 部分依赖尚未适配） | 建议 [uv](https://docs.astral.sh/uv/)：`uv python install 3.11` |
| 显卡 | 可选 | NVIDIA 显卡 + CUDA 会显著加快转写；没有就自动走 CPU |
| 磁盘 | ≥ 10 GB 空闲 | venv（含 torch/cu128 约 5–7 GB）+ 模型（按需下载） |

> **路径尽量全英文**：`D:\ECHO` ✅ ／ `D:\学习\ECHO` ❌。
> 少数原生依赖（nagisa/dynet、部分 funasr 组件）读不了非 ASCII 路径。
> 若必须放在中文路径下：另建一个 ASCII 目录联接（junction）指向该 venv，并把解释器路径写进**两个**
> 环境变量——`ECHO_PYTHON`（所有脚本都优先使用它，见 `scripts\*.ps1`）和 `ECHO_PYTHONW`
> （DSH 的 echo-host 插件用它拉起服务；只设 `ECHO_PYTHON` 不够，因为插件走的是 `pythonw`）：
> ```powershell
> New-Item -ItemType Junction -Path C:\echo-venv -Target <你的ECHO目录>\venv
> setx ECHO_PYTHON  C:\echo-venv\Scripts\python.exe
> setx ECHO_PYTHONW C:\echo-venv\Scripts\pythonw.exe
> ```
> **两个变量都必须写「联接路径」（ASCII）**，不要写成 `<你的ECHO目录>\venv\Scripts\...` 的真实中文路径；
> 否则会议转写选 `qwen3asr` 时会在加载引擎处失败，报错只有一句
> `RuntimeError: Could not read model from ...\nagisa\data\nagisa_v001.model`
> （2026-09-15 迁移事故就是这个原因：仓库仍是中文路径，但解释器从 junction 换成了仓库内 venv）。
> 仓库搬家/改名后 junction 的 Target 会失效（联接指向旧路径），需要重新 `New-Item -ItemType Junction`。

## 1. 取代码 + 建 venv

```powershell
git clone https://github.com/NeoBigZhou/echo-voice-assistant.git
cd echo-voice-assistant

python -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# 有 NVIDIA 显卡（推荐）：
.\venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu128
# 纯 CPU：跳过上面这行，torch 会以 CPU 版安装（转写仍可用，慢一些）
```

> 装依赖**一律**用 `python -m pip`，不要直接跑 `venv\Scripts\pip.exe`（那个 exe 里写死了创建
> venv 时的解释器路径，挪过机器就会报 `Fatal Python error: init_fs_encoding`）。
> 同理，如果你是从别人的机器整包拷来的 venv，先跑 `scripts\new-machine-setup.ps1` 修 `pyvenv.cfg`
> 里的 `home` 指向。

## 2. 初始化 + 启动

```powershell
powershell -File scripts\setup.ps1               # 校验 venv / 补依赖 / 检查模型 / 建库
powershell -File scripts\start.ps1 -Background   # 后台启动（无窗口）
# 面板：http://127.0.0.1:<端口>（默认 8970；实际端口见 data\echo-port.txt）
```

- 开机自启：`powershell -File scripts\install-autostart.ps1`（卸载加 `-Remove`）
- 停：`powershell -File scripts\stop.ps1`　重启：`powershell -File scripts\restart-echo.ps1`
- 前台看日志：`powershell -File scripts\start.ps1`（Ctrl+C 结束）
- 日志文件：`data\logs\echo-server.log`、`data\logs\echo-server.log.err`

## 3. 模型

仓库不含模型。面板 → **设置 → 模型** 会列出每一项的**体积 / 落地路径 / 是否就绪 / 获取方式**：

| 模型 | 体积 | 怎么拿 |
|---|---|---|
| SenseVoice（默认转写引擎） | ~896 MB | 面板点「下载」（ModelScope 缓存）；不点也行，首次转写会自动下 |
| Whisper 各档（tiny…large-v3） | 75 MB – 2.9 GB | 面板点「下载」（HF 镜像 `hf-mirror.com`） |
| Qwen3-ASR 0.6B + 强制对齐 | ~3.6 GB | 面板点「下载」，或 `powershell -File scripts\install-qwen3asr.ps1`（同时装依赖） |
| sherpa-onnx 流式 zipformer | ~189 MB | 面板点「下载」 |
| 说话人分离（pyannote） | ~31 MB | **自行获取**：上游是 HF gated 模型（需同意条款），拿到后按面板给的路径放好 |
| 唤醒词 KWS | ~39 MB | **自行获取**：从 sherpa-onnx 的 KWS 模型放成面板给的四个文件名 |

> ⚠️ **Whisper 系列（`tiny`…`large-v3`）的中文输出可能带繁体字**：它的中文训练语料以繁体为主，
> 和"语言设成 zh"无关。ECHO 已用简体提示词诱导，并且**命令 / 会议 / 对外 API 三条路径参数一致**
> （2026-09-15 issue #2：会议路径此前漏了提示词）。仍遇到繁体就把引擎换成 `sensevoice`（默认）
> 或 `qwen3asr`。

内网环境连不上外网时：把另一台机器上已经就绪的 `models/` 目录（或 ModelScope 缓存
`~/.cache/modelscope/models`）按同样的相对路径拷过来即可。

## 4. 接上 DSH Desktop（可选，用来"执行指令/写纪要"）

1. 安装并启动 DSH Desktop，在其设置里**放开本机访问**（ECHO 默认连 `http://127.0.0.1:43120`）。
2. 面板 → 启动 → `DSH 执行引擎` 应为在线；不在线可点「启动」/「重试」。
3. 想让 DSH 启动时顺便守护 ECHO（并在 DSH 升级后自动重装插件）：

```powershell
powershell -File scripts\install-echo-host-plugin.ps1          # 部署 + 自检
powershell -File scripts\install-echo-host-plugin.ps1 -Uninstall
```

部署脚本会把 `plugin/echo-host/` 拷到 `<DSH 安装目录>\resources\app.asar.unpacked\echo-host\`，
写一份 `echo-root.txt`（插件据此找到本仓库），并在 DSH 的**活动 Profile** 补丁层
`%USERPROFILE%\.dsh\profiles\<active>\cordis.patch.yml` 里维护注册行。改完源码要重跑，并**重启 DSH** 才生效。

## 5. 右缘边条（可选）

```powershell
dotnet build sidebar\echo-sidebar.csproj -c Release
# 产物：sidebar\bin\Release\net7.0-windows\win-x64\echo-sidebar.exe
```

热键 `Ctrl+Shift+E` 切换面板；折叠态是一条 64px 功能条（录音 / 电平 / 说话 / 服务状态灯 / 隐藏箭头）。
边条宽度、热键、打开方式都在面板 → 设置 → 语音命令 里改。

## 6. 排错

| 症状 | 原因 / 处理 |
|---|---|
| `Fatal Python error: init_fs_encoding` | venv 是从别的机器拷来的，`pyvenv.cfg` 的 `home` 指向不存在的 Python。跑 `scripts\new-machine-setup.ps1` |
| 启动时提示"模型缺失" | 见第 3 节；面板 → 设置 → 模型 里看每个模型的落地路径 |
| 转写很慢 | 装了 CUDA 版 torch 吗？面板 → 启动 → `命令转写引擎` 的详情会显示 `cuda:0` 还是 CPU |
| 热键没反应 | 面板 → 启动 → `热键/媒体键` 状态；热键由 ECHO 服务注册，改完设置需重启 ECHO |
| `.ps1` 脚本报"字符串缺少终止符" | 脚本被存成了**无 BOM 的 UTF-8 且含非 ASCII**，PowerShell 5.1 按 ANSI 读就会坏。本仓库脚本一律纯 ASCII 或带 BOM，见 [powershell-编码与脚本经验.md](powershell-编码与脚本经验.md) |
| 面板显示"连接失败" | ECHO 没起来 / 端口被占。看 `data\logs\echo-server.log.err`，或 `scripts\restart-echo.ps1` |

## 7. 安全：本机 API 只允许本机访问

ECHO 的 API（默认 8970，权威值见 `data\echo-port.txt`）与模型路由（默认 8899，见
`dsh-failover/config.json` 的 `port`）默认**不要求 token**，因为它们只监听 `127.0.0.1`。
但"只监听回环"并不等于安全：**你打开的任意网页**，其 JS 都能访问 `http://127.0.0.1:<端口>`（默认 8970）。
在早期版本里这构成一条完整攻击链——页面可以

* 读走会议原始录音、逐字转写、LLM 纪要（`GET /api/meetings` + `/audio` / `/file`）；
* `POST /api/meeting/start` 让 **ECHO 服务进程**开始录音（不需要浏览器麦克风权限、也不会亮录音指示灯），
  随后把音频下载走——**可远程触发的窃听**；
* `POST /api/assistant/command` 让大模型执行任意指令、`POST /models/download` 拉几 GB 占满磁盘。

现在有**四层**防护（前两层由 `app/netguard.py` 提供，ECHO 与模型路由都装了）：

1. **CORS 只放行回环来源**（不再是 `allow_origins=["*"]`），跨站页面拿不到响应；
2. **Host / Origin 守卫中间件**：`Host` 或 `Origin` 不是 `127.0.0.1` / `localhost` / `::1` 一律 **403**。
   这一层还顺带封死 **DNS Rebinding**（攻击者域名先解析到真实 IP 过校验、再改指 127.0.0.1），
   并且挡住"简单请求"式的跨站写入（那种请求浏览器不预检，只收紧 CORS 是拦不住的）。
   `Origin: null`（`file://`、sandbox iframe）也拒绝，所以折叠条页面改为经
   `http://127.0.0.1:<端口>/web/rail.html`（默认 8970，权威值见 `data\echo-port.txt`）同源加载。
3. **路径穿越收口**：`GET /meetings/{id}/file?kind=` 的白名单只允许 `transcript` / `topics` / `summary`，
   目录名取 basename，且最终路径必须仍在 `data/meetings/` 内（realpath 判定）；
   `/meetings/{id}/audio` 同样有兜底。没有这一层，`kind=../../..` 或 `kind=C:/...` 就能读走磁盘上
   任意 `.md`（`os.path.join` 遇到绝对路径会整段替换掉前面的目录）。
4. **API 密钥不明文落库**（可选，开 `apiAuthEnabled` 才生效）：`api_keys` 只存 `sha256(token)`，
   校验用 `hmac.compare_digest`；`GET /api/keys` 不回 token/哈希，明文仅在 `POST /api/keys`
   返回一次（丢了删掉重建）。开启 `apiAuthEnabled` 前先创建密钥并存到客户端，否则面板自身
   不带 token 会被 401。

**副作用（预期）**：用局域网 IP 从手机或别的机器访问面板会 403。

**手机访问的正确姿势**：不要为此把服务绑到 `0.0.0.0`。保持 127.0.0.1 绑定，前面套一个带认证的反向代理
（Caddy / Nginx + Basic Auth + TLS），只对代理放行；同时打开 ECHO 的 `apiAuthEnabled` 并用
`POST /api/keys` 生成 Bearer Token。这样暴露面收敛到"代理 + 认证"这一层，而不是把裸 API 交给整个局域网。

> 提示：`GET /api/status` 设计上不需要 token（面板首页要显示状态），所以**它也不该被反向代理公开暴露**。

## 8. 数据出网说明：哪些是本地、哪些会连外网

**"本地部署"指的是 ECHO 这一侧的转写与存储在本机，不代表数据不出网。** 逐项如下：

| 功能 | 执行位置 | 数据去向 |
|---|---|---|
| 命令/会议转写、唤醒词、说话人分离 | 本机（SenseVoice / Whisper / Qwen3-ASR / sherpa-onnx / KWS / pyannote） | 不出网 |
| 录音、转写、纪要文件与数据库 | 本机 `data/` | 不出网 |
| **会议纪要 / 议题分段** | 本机发起，**推理在 DSH 所连的模型服务** | **转写全文发往该服务**（内网网关 = 贵单位内网；公网 API = 模型厂商） |
| **指令执行** | 同上 | **指令文本与上下文发往该服务** |
| **语音播报** | 默认 `ttsEngine=auto`：先试 **edge-tts（微软在线，需访问 speech.platform.bing.com）**，失败才降级 Windows SAPI | **播报文本发往微软**；设成 `sapi` 即全离线（音色略差） |
| 纪要归档（委派给你的技能） | 本机（DSH 会话 + 你的技能） | 由你的技能决定，例如写本地笔记库 = 不出网 |
| 模型下载 | 本机（ModelScope / hf-mirror 镜像） | 只下载权重，不上传数据 |

**要"全程不出网"的三步**：

1. 设置 → 语音命令 → 语音合成引擎 = `sapi`（或 `off` 关闭播报）；
2. DSH 的 provider 指向**本机或内网**的 OpenAI 兼容服务（没有任何本地大模型时这一步无法满足，
   也就意味着"纪要 / 执行"必然出网——请据此判断能不能把会议内容交给那个服务商）；
3. 关掉自动纪要（设置 → 会议 → 自动生成纪要），改为需要时手动生成。

> 设置页里对应项的说明也标注了在线/离线：`ttsEngine`（edge-tts = 在线 / sapi = 离线）、
> `meetingAutoSummarize`（转写全文会发给 DSH 配置的模型服务）。
## 附：端口一览

| 端口 | 用途 |
|---|---|
| 8970（默认，可改） | ECHO 服务 + 控制面板 + REST API。改过就以 `data\echo-port.txt` 为准（本机为 18060） |
| 8899（默认，可改） | 模型路由（可选，见 [dsh-failover/README.md](../dsh-failover/README.md)）；改 `dsh-failover/config.json` 的 `port`（本机为 18061） |
| 43120 | DSH Desktop 本地 API（ECHO 连它执行指令） |
