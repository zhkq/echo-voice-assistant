# ECHO — Windows 语音助手 + 会议纪要

Windows 上的一体化个人语音助手：**全局热键/唤醒词 → 本地转写 → 交给大模型执行 → 语音播报结论**，
外加**会议录音 → 转写 → 说话人分离 → 自动生成纪要**，以及一个原生 SPA 控制面板。

LLM 执行层由 [DeepSeek Harness Desktop](https://github.com/NeoBigZhou)（DSH Desktop）本地 API 承担。
ECHO 自己**不做推理**，只负责录音、转写、编排、面板与播报——**所以"本地"指的是 ECHO 这一侧**：
转写与语音识别全在本机，但**纪要与指令执行的文本会离开这台机器**（发给 DSH 配置的模型服务），
**语音合成默认用的是微软在线服务**。逐项见下面「本地 / 联网」一节。

```
热键/唤醒词 → 录音 → 本地转写(SenseVoice / Whisper / Qwen3-ASR / sherpa)
  → DSH 会话执行(可调用技能) → 极简结论语音播报 + 历史入库
会议录音 → 分段存档 → 转写 → 说话人分离(可选) → DSH 生成纪要 → SQLite 管理
控制面板 → 组件状态与启停 / 设置 / 历史 / 会议管理 / 模型清单与下载
模型路由 → 多个上游组成「模型组」按通道号顺序派发（DSH 侧只认 ECHO AUTO）
```

## 本地 / 联网（重要：哪些数据会出网）

| 功能 | 在哪里执行 | 数据去向 |
|---|---|---|
| 转写（命令/会议）、唤醒词、说话人分离、声纹识别 | **本机**（SenseVoice / Whisper / Qwen3-ASR / sherpa-onnx / KWS / pyannote） | 不出网 |
| 录音与转写文件 | **本机** `data/meetings/`（SQLite + wav + md） | 不出网 |
| **会议纪要 / 议题分段** | 本机记录，**推理在 DSH 配置的模型服务** | **转写全文会发给该模型服务**：内网网关就是贵单位内网，公网 API 就是模型厂商（如 DeepSeek 官方） |
| **指令执行** | 同上（DSH 会话 + 技能） | **你的指令文本与相关上下文会发给该模型服务** |
| **语音播报（TTS）** | 默认 `ttsEngine=auto` → 先试 **edge-tts（微软在线，需访问 `speech.platform.bing.com`）**，失败才降级 Windows SAPI | **要念出来的文本会发给微软**；想全离线就在设置里把引擎固定为 `sapi`（音色差一些） |
| 纪要归档到你自己的技能 | 本机（DSH 会话 + 你的技能） | 取决于你的技能实现（例如写本地笔记库＝不出网） |
| 模型下载 | 本机（ModelScope / hf-mirror 镜像） | 只下载权重，不上传任何数据 |

一句话：**"ECHO 本地"≠"数据不出网"**。要求全程不出网时，请把 `ttsEngine` 设为 `sapi`，
并把 DSH 指向本机/内网的模型服务（或用你信得过、可接受数据外发的服务商）。

## 特性

- **转写与语音识别全本地**：命令与会议转写、唤醒词、说话人分离都在本机跑，音频文件不出网
  （需要出网的是"交给大模型"和"默认的在线语音合成"，见上表）。
- **指令只要一句结论**：提示词要求模型先给极简结论再给详情，语音只念结论，详情留在会话里。
- **会议纪要**：分段录音、按需/常驻双转写引擎、说话人分离（pyannote）、纪要归档可委派给你自己的技能。
- **常用联系人声纹（默认关闭）**：在会议里把说话人**改名成联系人**（如「张总」）即自动入库；下次会议听到同一个人的
  声音会自动把 TA 标成联系人名，同一个人被分离成两簇时自动合并。**声纹属生物特征数据，默认不开**：
  到 设置 → 会议 里显式开启（`voiceprintEnabled` / `voiceprintAutoEnroll`）；转写结束后
  `source=voiceprint` 的日志会给出一行汇总（判定次数 / 命中 / 最高相似度 / 未命中原因），
  阈值与歧义间隔就照它校准。样本只存本机 `data/echo.db`，可在会议页「说话人管理」里删除；
  关闭时不留存任何样本。
- **右缘边条**：**ECHO 启动后自动**在屏幕右缘显示一条 64px 折叠条（录音、电平、说话、状态灯），
  `Ctrl+Shift+E` 展开/收起；不想自动显示可在 设置 → 语音命令 关掉（`panelAutoStart`），
  也可设成"启动即展开面板"（`panelStartCollapsed=false`）。
- **模型路由（ECHO AUTO）**：把多个上游（内网网关 / 公网 API / 任意 OpenAI 兼容端点）组成
  「模型组」，按通道号顺序派发并自动故障转移 + 熔断，DSH 里只需选一个 `ECHO AUTO`
  （见下文「模型路由」）。
- **模型面板**：设置页列出每个功能需要的模型、体积、落地路径与就绪状态，能从 ModelScope / HF 镜像一键下载。
- **可被其他应用调用**：本地 REST API（转写、TTS、会议、设置），面板与手机 App 共用同一入口。

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11 + FastAPI + uvicorn（单进程；端口默认 8970，可改，权威值见 `data/echo-port.txt`） |
| 数据库 | SQLite（`data/echo.db`，WAL，版本化迁移） |
| 转写 | faster-whisper / funasr SenseVoice / Qwen3-ASR / sherpa-onnx（本地模型，自动选 GPU） |
| 唤醒 | sherpa-onnx KWS（离线关键词，可自定义） |
| 热键 | ctypes 全局组合键 + 媒体键低级钩子（无 C# 编译依赖） |
| 合成 | edge-tts（在线，自然）→ Windows SAPI（离线兜底） |
| 面板 | 原生 HTML/CSS/JS SPA（无构建链，响应式，可直接在手机浏览器打开） |
| 边条 | .NET 7 WinForms + WebView2（`sidebar/`） |
| 模型路由 | 本机 OpenAI 兼容代理（`dsh-failover/`，默认 `127.0.0.1:8899`，可被 `config.json` 的 `port` 覆盖）：多上游派发 + 探测 + 熔断 |
| 执行层 | DeepSeek Harness Desktop 2.x 本地 API（默认 `http://127.0.0.1:43120`） |

## 快速开始

```powershell
git clone https://github.com/NeoBigZhou/echo-voice-assistant.git
cd echo-voice-assistant

python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
# 有 NVIDIA 显卡时（可选，转写提速明显）：
#   .\venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu128

powershell -File scripts\setup.ps1              # 校验 venv/依赖/模型 + 建库（一次性）
powershell -File scripts\start.ps1 -Background  # 后台启动
powershell -File scripts\start-all.ps1          # 或：一键（含 DSH 检查）
# 打开面板：http://127.0.0.1:<端口>（默认 8970；实际端口见 data\echo-port.txt）
```

开机自启：`powershell -File scripts\install-autostart.ps1`（卸载加 `-Remove`）。

首次使用建议：

1. 面板 → **设置 → 模型**：点一下把 `SenseVoice`（默认转写引擎，约 896MB）下载好；
2. 面板 → **设置 → 语音命令**：确认热键（默认 `Ctrl+Alt+C` 说话、`Ctrl+Shift+E` 面板）；
3. 面板 → **启动**：查看各组件状态，缺什么点什么（DSH 执行引擎需要另行安装 DSH Desktop）；
4. 想在 DSH 里用**模型路由**：面板 → **模型路由** 页签配置通道，再到 DSH 把模型选成 `ECHO AUTO`。

更细的安装、显卡、非 ASCII 路径、开机自启、边条编译、插件部署见 **[docs/DEPLOY.md](docs/DEPLOY.md)**；
逐项核对清单见 **[docs/新机器部署指南.md](docs/新机器部署指南.md)**。

### macOS（精简支持）

macOS 使用独立入口，以原生 AppKit / WKWebView 浮动框替代 Windows 边条：

```bash
mac/setup_mac.sh
mac/start_mac.sh
```

支持右缘浮动框、面板、录音转写、会议、桌面通知与 macOS `say` 离线播报；全局热键需要可选依赖 `pynput`。
浮动框可运行 `bash mac/build_sidebar.sh` 构建（需要 Apple Command Line Tools）；
旧用户在设置里将 `panelOpenMode` 改为 `sidebar` 后，重启 ECHO 即可随服务启动。
限制和权限设置见 **[mac/README.md](mac/README.md)**。

## 目录结构

```
echo-voice-assistant/
├── app/            Python 后端（db/config/dsh/assistant/meeting/boot/audio/modelinfo/router…）
├── web/            控制面板 SPA（index.html / app.js / app.css）+ 右缘折叠条 rail.html
├── models/         本地模型（不入 git；用面板下载或自行拷贝）
├── data/           echo.db、录音、历史、日志（不入 git）
├── assets/         提示音等资源
├── plugin/         DSH Desktop 宿主插件源码（echo-host：拉起/守护 ECHO；右缘边条见 sidebar/）
├── sidebar/        右缘折叠条/边条宿主（.NET 7 WinForms + WebView2，echo-sidebar.exe）
├── dsh-failover/   模型路由（默认本机 8899，可被 config.json 的 port 覆盖）：多个上游组成「模型组」，DSH 侧只认 ECHO AUTO
├── scripts/        setup / start / stop / 自启 / 插件部署 / 一键安装向导
├── docs/           部署指南、新机器部署指南、纪要归档说明、PowerShell 编码经验
└── .dsh/skills/    DSH 技能（随仓库提供 meeting-record；其余按需自建）
```

## 配置

设置全部入库（`data/echo.db`），面板里改完即生效，分组为：智能体 / 通用 / 语音命令 / 唤醒词 / 会议 / 纪要归档 / 模型路由 / 面板 / DSH 服务。
常用项：

| 键 | 含义 |
|---|---|
| `sttModel` | 命令转写引擎：`sensevoice`（默认）/ `qwen3asr` / `sherpa` / `tiny`…`large`（whisper 档） |
| `meetingSttModel` | 会议转写引擎（可与命令不同） |
| `sttLanguage` | 转写语言（命令与会议共用）：`zh`（默认）/ `en` / `ja` / `ko` / `yue` / `auto`（自动识别）。Whisper 只认 ISO 码，填全名（如 `Chinese`）会自动纠正，非法值回退 `zh` |
| `wakeHotkey` / `fallbackHotkey` / `panelHotkey` | 说话 / 备用 / 面板热键 |
| `panelOpenMode` | `sidebar`（右缘边条）/ `app` / `browser` |
| `ttsEngine` | `auto` / `edge-tts` / `sapi` / `off` |
| `minimalReply*` | 「先结论、后详情」的提示词与字数上限 |
| `worklogEnabled` / `worklogVaultRoot` / `worklogMode` | 纪要归档：把归档委派给你自己的技能（见 [docs/worklog.md](docs/worklog.md)） |
| `voiceprintEnabled` / `voiceprintAutoEnroll` / `voiceprintThreshold` / `voiceprintMargin` | 常用联系人声纹（**默认关闭**，生物特征数据）：转写时自动认人 / 改名自动入库 / 匹配阈值 / 歧义间隔 |
| `apiAuthEnabled` | 开启后除 `/api/status` 外都需要 `Authorization: Bearer <token>` |

> ⚠️ **Whisper 系列的中文可能输出繁体字**（它的中文训练语料以繁体为主，与语言参数无关）：
> ECHO 已用简体提示词诱导，且命令 / 会议 / 对外 API 三条路径一致（2026-09-15 修）；
> 若仍出现繁体，把引擎换成 `sensevoice`（默认）或 `qwen3asr` 即可。

## 模型路由（ECHO AUTO）

ECHO 自带一个**本机模型路由**（`dsh-failover/proxy.py`，只监听回环，默认 `127.0.0.1:8899`、
可被 `dsh-failover/config.json` 的 `port` 覆盖）：把多个上游
（公司内网网关、DeepSeek 官方、任意 OpenAI 兼容端点…）组成一个「模型组」，**按通道号顺序派发**；
前面的通道连不上、首字节超时或返回 401/429/5xx 时自动改走下一个（连续失败会短暂熔断该通道，
冷却后再试）。

**为什么不由 DSH 自己做**：DSH 的 `agent-default-model` 只存单一 `{provider, model}`，
重试也只在同一 provider 内 —— 跨上游派发只能放在本机；路由对 DSH 是一个普通 provider，
所以也不会把 DSH 拖进整轮失败。

**怎么用**：ECHO 启动时 `boot` 自动拉起路由、并把它注册成 DSH 的本地 provider `echo-auto`
（写 `~/.dsh/settings.yaml`，幂等，可在设置里关），到 DSH 里把模型选成 **ECHO AUTO** 即可。
面板顶部 **模型路由** 页签就是它的控制台：**通道号 = 派发顺序**（按列表位置自动生成），
每条通道一个可改的**昵称**（= `config.json` 里成员的 `name`），可开关通道 / 立即探测 /
看「派发情况」统计。

| 项 | 说明 |
|---|---|
| 配置 | `dsh-failover/config.json`（`groups`、成员、超时、熔断参数）；从 `config.example.json` 复制修改；命令行 `powershell -File dsh-failover\start.ps1` / `stop.ps1` / `status.ps1` |
| 凭据 | `config.json` 只写凭据**名字**（如 `DEEPSEEK_API_KEY`），真实值从 `~/.dsh/.credentials.yaml` 读，密钥不落仓库 |
| 健康 | `GET /api/failover/health`（注册态 + 各通道 `通道号 昵称` + 派发/熔断统计） |
| 细节 | 探测与熔断口径、已知边界见 [dsh-failover/README.md](dsh-failover/README.md) |

## 模型从哪来

仓库**不含**模型权重（体积大、且部分上游有授权限制）。三条路：

| 方式 | 适用 |
|---|---|
| 面板 → 设置 → 模型 → **下载** | SenseVoice、Whisper 各档、Qwen3-ASR、sherpa 流式（走 ModelScope / hf-mirror 镜像） |
| 首次使用时自动下载 | SenseVoice 走 ModelScope 缓存；Whisper 走 HF 镜像缓存 |
| 从别处拷贝 | 说话人分离（pyannote，HF 上是 gated 模型）与 KWS 唤醒词模型：按模型面板里给的**落地路径**放对目录即可 |

面板会显示每一项的体积、目标路径与当前是否就绪，落地路径是**代码约定**（改名会加载不到）。

**Qwen3-ASR（可选，会议/命令转写更准）**：

```powershell
powershell -File scripts\install-qwen3asr.ps1   # 装依赖(qwen-asr) + 下载模型(~1.5GB)
```

装好后在 设置 → 会议 → 会议转写模型 选择 `qwen3asr`（命令引擎 `sttModel` 也可选）。
模型经 modelscope 缓存加载（`~/.cache/modelscope`，无中文路径兼容问题）。

## DSH Desktop 宿主插件（echo-host）

`plugin/echo-host/` 是挂到 DSH Desktop 上的 Cordis 插件，随 DSH Desktop 启动：

- 自动拉起 / 守护 ECHO Python 服务，崩溃自动重启；**DSH Desktop 退出时不停止 ECHO**
  （ECHO 常常是独立启动的，早先"随 DSH 一起关"导致一重启 DSH 服务就没了）。
  端口不写死：`ECHO_PORT` → `~/.dsh/settings.yaml` 的 `serverPort` 注释行 → 默认 8970；
  权威来源是 ECHO 启动时写出的 `data\echo-port.txt`（本机因端口保留段冲突用的是 18060）。
- 仪表盘热键 `Ctrl+Shift+E`：由 **ECHO 服务进程自己注册**（`app/hotkey.py` 的 `RegisterHotKey`，
  与 `Ctrl+Alt+C` 同一套机制）。按一下按 `panelOpenMode` 行事：默认 `sidebar` 切换
  **右缘边条**（`sidebar/echo-sidebar.exe`，.NET 7 + WebView2，单实例、经命名管道 toggle，
  由 ECHO 服务拉起）；`app`=Chromium 应用窗口；`browser`=默认浏览器。
- 插件侧的 Electron 边条窗口/热键在 **DSH Desktop 2.0.9 上不可用**（`app.asar.unpacked` 里动态
  import 到的 `electron` 只有 `net/systemPreferences`，没有 `app/BrowserWindow/screen/globalShortcut`，
  2026-09-12 实测，见插件文件日志）：插件仍会探测一次并记日志，探测失败即降级为 no-op，
  边条由 ECHO 自带的 .NET 边条进程负责。

面板热键/打开方式可在 面板 → 设置 → **语音命令** 里改：`panelHotkey`（默认 `Ctrl+Shift+E`，
支持 `Ctrl+Shift+Space`、`Ctrl+Alt+F1` 等）、`panelOpenMode`（`sidebar`=右缘边条（默认）/
`app`=Chromium 应用窗口 / `browser`=默认浏览器）。热键由 ECHO 注册，改完只重启 ECHO 即可
（不用动 DSH）。

**部署（源码改动后必须重跑）**：

```powershell
powershell -File scripts\install-echo-host-plugin.ps1            # 部署 + 自检
powershell -File scripts\install-echo-host-plugin.ps1 -Quiet     # 启动时自愈（无输出）
powershell -File scripts\install-echo-host-plugin.ps1 -Uninstall # 卸载
```

脚本把源码复制到 `<DSH 安装目录>\resources\app.asar.unpacked\echo-host\`，并在 **Profile 补丁层**
`%USERPROFILE%\.dsh\profiles\<活动 profile>\cordis.patch.yml` 里维护注册行（标记块
`# >>> echo-host plugin ... >>>`，`id: echo-host` + 指向部署副本的 `file:///` URL），最后用 DSH
自带 loader 代码**离线校验**（补丁能否解析、行是否进树、入口文件是否存在、模块能否 import），
校验不过即报错退出，避免"下次启动才炸"。

> ⚠ **要写到哪个 profile？** Desktop 只用它**当前活动**的那个 Profile。安装脚本自动读取
> `%APPDATA%\DSH Desktop\profile-selection\state.json` 的 `active` 字段，然后为活动 profile 与
> `desktop` 两个 profile 各写一份注册行，避免切 profile 丢插件。
> 手工排查时先确认这个字段，别再默认往 `desktop` 里写。

> ⚠ **DSH Desktop 2.0.9 不再读取 `app.asar.unpacked\cordis.patch.yml`**（2.0.5 会读）。2026-09-12
> 从 2.0.5 升到 2.0.9 后插件"静默消失"，就是补丁放在那个目录里、Loader 实时清单中一行都没有。
> 现在注册行放在 Profile 补丁层（Desktop 的组合顺序：bundle 层 → Profile 层 → 机器层
> `~\.dsh\cordis.patch.yml`）。
>
> ⚠ **Profile 补丁层只在 DSH Desktop 启动时组合**：部署后必须**重启 DSH Desktop** 才生效。
> `scripts\launch-desktop.ps1`（桌面快捷方式）与 `scripts\start.ps1`（开机自启）每次启动都会自动
> 补装（自愈），所以 DSH 升级后启动一次 ECHO 即可；也可手动重跑上面的部署命令。
>
> ⚠ 该补丁文件里**不要写非 ASCII 注释**：Windows PowerShell 5.1 会把无 BOM 的 UTF-8 当 ANSI 读，
> 读回时会把行读串、YAML 直接坏掉（本工具只写 ASCII 注释，并在写入前用 yaml 解析器校验）。

## 与 DSH Desktop 的关系

「执行指令 / 生成纪要」这一步需要 DSH Desktop（本地 API，默认 43120 端口，需在 DSH 里放开本机访问）。
ECHO 通过 JSON-RPC 风格接口与会话交互，把技能（skills）能力直接借过来——所以"整理成表格""查一下天气"
这类任务不需要在 ECHO 里再实现一遍。

插件 `plugin/echo-host/` 是可选的：它让 DSH 启动时顺带守护 ECHO，并在 DSH 升级后自动重装；
ECHO 也可以完全脱离 DSH 独立启动（转写、会议、面板都不依赖它），只把"执行"这一步留白。

## 常用 API（面板/手机 App/技能共用）

| 端点 | 说明 |
|---|---|
| `GET /api/status` | 组件状态 + DSH + 会议 + 忙闲 + 转写引擎加载状态 |
| `GET/PUT /api/settings` | 配置读写（入库，带分组/类型元数据） |
| `POST /api/assistant/command` | 发送文本命令 `{text, source}` |
| `POST /api/assistant/capture` | 触发一次录音命令流 |
| `POST /api/models/download` | 下载指定模型（`GET /api/models` 看清单与进度） |
| `POST /api/system/restart` | 重启 ECHO 服务 |
| `GET /api/boot/status` | 启动编排状态（组件 + 进度 + 汇总） |
| `POST /api/boot/component/{id}/start\|stop` | 组件启动/重试/停止（dsh/stt-cmd/stt-meeting/tts/wake/hotkey） |
| `POST /api/meeting/start\|stop` | 会议录音开关 |
| `GET /api/meetings` · `GET /api/meetings/{id}` | 会议列表 / 详情 |
| `POST /api/meetings/{id}/speaker/rename\|merge\|recognize` | 说话人改名（改名即声纹入库）/ 合并 / 声纹识别本场 |
| `POST /api/meetings/{id}/summary/regenerate` | 重新生成纪要（含语义分段） |
| `POST /api/control/dsh/start\|stop` | DSH 服务管理 |
| `POST /api/control/stt/unload` | 卸载转写模型（释放显存） |
| `POST /api/control/tts/test` | 语音合成测试播报 |
| `POST /api/control/wake/start\|stop` | 唤醒词监听开关 |
| `POST /api/control/hotkey/start\|stop` | 热键监听开关 |
| `GET /api/failover/health` | 模型路由健康（注册态 + 各通道 `通道号 昵称` + 派发/熔断统计） |
| `GET /api/logs` · `GET /api/events` | 日志 / 事件流 |

## 转写服务（可被其他应用调用）

ECHO 提供本地转写 API，其他应用/脚本可直接上传音频获取转写结果（离线、自动 GPU）。
下面示例按**默认端口 8970** 写；实际端口见 `data\echo-port.txt`（本机可能因端口保留段冲突被改成别的，如 18060）：

```bash
# 转写为文本（engine: sensevoice/qwen3asr/sherpa/whisper 模型名）
curl -X POST http://127.0.0.1:8970/api/stt/transcribe \
  -F "file=@录音.mp3" -F "engine=sensevoice" -F "lang=zh"

# 转写为带时间戳的句子（qwen3asr 原生句子 + ForcedAligner 时间戳）
curl -X POST http://127.0.0.1:8970/api/stt/sentences \
  -F "file=@录音.wav" -F "engine=qwen3asr" -F "model=0.6B" -F "lang=zh"
# → {"text": "...", "sentences": [{"start": 1.0, "end": 4.0, "text": "..."}]}

# 查询转写引擎加载状态（已加载模型 / GPU 设备）
curl http://127.0.0.1:8970/api/stt/status
```

Python 调用：

```python
import requests
r = requests.post("http://127.0.0.1:8970/api/stt/transcribe",
                  files={"file": open("录音.wav", "rb")},
                  data={"engine": "qwen3asr", "lang": "zh"})
print(r.json()["text"])
```

支持 wav/mp3/flac 等常见格式（自动转 16k 单声道）；模型懒加载，首次调用较慢、之后常驻复用；
上传上限 512MB；开启 `apiAuthEnabled` 后需带 `Authorization: Bearer <token>`（`POST /api/keys` 生成）。

## 安全

* API 与模型路由**只监听 `127.0.0.1`**，并且装了来源守卫（`app/netguard.py`）：
  `Host` / `Origin` 非回环一律 403，跨站页面既读不到数据也发不出有效写入，
  **DNS Rebinding** 与 `Origin: null`（`file://`、sandbox iframe）同样被拒。
  这一层是必要的，因为本地 API 默认不带 token——没有它，你打开的任意网页都能
  `POST /api/meeting/start` 让 ECHO 用服务进程开麦录音再把音频下载走。
* 不要为了手机访问把服务绑到 `0.0.0.0`：保持回环绑定，前面套带认证的反向代理
  （Caddy/Nginx + Basic Auth + TLS），并开启 `apiAuthEnabled` + Bearer Token。详见 [docs/DEPLOY.md](docs/DEPLOY.md#7-安全本机-api-只允许本机访问)。
* API 密钥只以 `sha256(token)` 存库，校验用 `hmac.compare_digest`；`GET /api/keys` 不回 token
  （明文仅在创建时返回一次）。开启 `apiAuthEnabled` 前先建好密钥并存到客户端，否则面板自身会被 401。
* 数据库、录音、历史、日志都在 `data/`（不入 git）；真实凭据只放在环境变量或
  `~/.dsh/.credentials.yaml`，`dsh-failover/config.json` 已在 `.gitignore` 里。

## 项目沿革

ECHO 是把一个早期实验实现（PowerShell 桥 + JSONL/文件状态、三进程互调）**整体重构**而来：
统一数据库、单进程、REST 唯一入口。转写/唤醒/说话人分离/简报清洗等模型与算法直接继承，
实验目录已可删除。详细设计见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 许可与致谢

本项目以 **MIT** 许可发布，见 [LICENSE](LICENSE)。

它站在这些开源项目的肩上（各自遵循其原始许可）：

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) / [CTranslate2](https://github.com/OpenNMT/CTranslate2) — Whisper 推理
- [FunASR](https://github.com/modelscope/FunASR) 与 ModelScope 上的 `iic/SenseVoiceSmall` — 中文短语音转写
- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) — 高精度转写与时间戳
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)（Apache-2.0）— 流式转写与 KWS 唤醒
- [pyannote.audio](https://github.com/pyannote/pyannote-audio) — 说话人分离（模型需自行在 HF 上接受条款后获取）
- [WebView2](https://learn.microsoft.com/microsoft-edge/webview2/) + .NET 7 — 右缘边条
- [mermaid](https://github.com/mermaid-js/mermaid) — 纪要里的图表渲染（`web/vendor/`）
