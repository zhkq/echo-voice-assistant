# ECHO 架构设计

> 本文档说明 ECHO 的整体架构、关键设计决策与扩展路径。
> 本项目把早期实验实现（PowerShell 桥 + JSONL/文件状态）整体重构为单进程 + 单一数据库。

## 1. 设计目标

| 目标 | 含义 |
|---|---|
| 单一事实来源 | 配置、历史、会议、状态全部进同一个 SQLite 库 |
| 触点统一 | 面板、手机 App、DSH 技能、命令行都走同一套 REST API |
| Windows 稳定 | 一个进程一个端口；音频/热键/唤醒在主进程内；进程看护只留给 DSH |
| 可扩展 | 表结构带迁移；事件流/API 密钥为移动端预留；会议行支持 kind 扩展 |

## 2. 总体架构

```
                        ┌────────────────────────────────────────┐
  触点（统一走 REST）   │            ECHO 单进程 (Python/FastAPI)    │
 ┌───────────┐          │  ┌────────┐  ┌────────┐  ┌──────────┐  │
 │ Web 面板   │─────────▶│  │  api   │─▶│assistant│─▶│  dsh     │──▶ DSH Web :3080
 │ (SPA :8970)│          │  │ 路由层  │  │ 流水线  │  │ RPC 客户端│   (执行/技能)
 │ 手机 App   │─────────▶│  └───┬────┘  └───┬────┘  └──────────┘
 │ (未来)      │          │      │           │
 │ DSH 技能    │─────────▶│  ┌───▼────┐  ┌──▼───────────┐
 └───────────┘           │  │  db    │  │  meeting      │──▶ data/meetings/*.wav
                         │  │ SQLite │  │  录音/转写/纪要 │
                         │  └────────┘  └──────┬────────┘
                         │  ┌──────────────────▼──────────┐
                         │  │ audio: stt / tts / recorder │
                         │  │        wake / diarize        │──▶ models/（本地模型）
                         │  └──────────────────────────────┘
                         │  ┌──────────────────────────────┐
                         │  │ hotkey(ctypes) · runtime     │
                         │  │ manager(DSH 进程) · services  │
                         │  └──────────────────────────────┘
                         └────────────────────────────────────────┘
```

**为什么单进程？** 语音链路（录音→转写→DSH→播报）是强时序依赖；
早期实现用 PowerShell 桥 + Python 服务 + C# 钩子三进程互调，故障面大。
单进程后：共享配置/DB/模型单例，重启即整体恢复，日志统一。

**启动编排（`app/boot.py`）**：启动拆为「阶段 0：面板 HTTP 立即可用（<1s）」
+ 后台分阶段拉起 9 个组件（server / dsh / stt-cmd 命令转写·常驻 / stt-meeting
会议转写·按需 / tts / wake / hotkey / meeting / diarize），每组件独立
状态机（pending→starting→online/failed/disabled/idle）、进度与耗时，支持
重试/启停；`stt-cmd` 常驻预载、`stt-meeting` 按需加载并可卸载。面板「启动」
页签实时展示进度与 `source=boot` 日志。`/api/status` 热路径读取缓存的设备
信息，不再因模型加载而卡顿。

**模型路由（可选组件）**：`dsh-failover/proxy.py`（默认 `127.0.0.1:8899`，可由
`dsh-failover/config.json` 的 `port` 覆盖）把多个上游组成「模型组」，
按通道号顺序派发，并做主动探测 + 被动健康/熔断（连续失败短暂摘除该通道）；ECHO 启动时把它
注册成 DSH 的本地 provider `echo-auto`。之所以在本机放一层，是因为 DSH 的 `agent-default-model`
只存单一 `{provider, model}`、`dsh-llm-retry` 也只在同一 provider 内重试 —— 跨上游派发
DSH 自己做不到。详见 [dsh-failover/README.md](dsh-failover/README.md)。

## 3. 数据库设计（data/echo.db，WAL）

版本化迁移：`meta.schema_version` + 顺序迁移列表（`db.MIGRATIONS`），
未来加表/加字段只 append 一条迁移。

| 表 | 职责 | 关键点 |
|---|---|---|
| `settings` | 配置 | 键值 + `grp` 分组 + `value_type` 类型 + `options` 候选；面板据此动态渲染表单 |
| `commands` | 命令历史 | 生命周期 `pending→sent→running→done/failed`；存 `reply` 全文与 `brief` 简报 |
| `dsh_sessions` | DSH 会话登记 | `kind` 唯一：`command`（命令）、`summary`（纪要） |
| `meetings` | 会议 | 状态机 `recording→transcribing→done/error`；存转写配置快照 |
| `speakers` | 会议说话人 | `UNIQUE(meeting_id,label)`，可改名/合并 |
| `speaker_embeddings` | 会议说话人平均声纹 | 转写时留存（`(meeting_id,label)` 主键）；改名入库与「识别本场」的数据源 |
| `voiceprints` | 常用联系人声纹库 | 联系人名 + 嵌入样本；与会议**弱关联**（删会议不清库），同场同人只留最新一条 |
| `lines` | 转写行 | 句级时间戳（段内相对秒）；`kind` 预留（speech/note/action…） |
| `summary_runs` | 纪要生成记录 | 可追加要求重新生成，留档 |
| `component_states` | 组件状态 | 面板轮询数据源，DB 持久化上次状态 |
| `logs` | 运行日志 | 替代散落 .log 文件 |
| `events` | 事件流 | 审计/统计/未来移动端推送 |
| `api_keys` | 外部触点密钥 | 手机 App 预留；`apiAuthEnabled` 开启后强制 Bearer 校验 |

**为什么不用 JSON/JSONL？** 早期实现的 `config.json`×3 + `commands.jsonl` + 文件/SQLite
混合导致：改配置要改多个文件、历史无法查询、会议数据两处不一致。
SQLite 单库：事务、索引、查询、迁移一应俱全，个人单机规模毫无压力。

## 4. 命令流水线（assistant.py）

```
触发(hotkey/mediakey/wake/web/skill/api)
  → beep(start) → record_command()（开口检测+静音收尾）
  → beep(done) → 本地转写（SenseVoice 最快 / sherpa 流式 / whisper 按配置）
  → 剥离唤醒前缀（"小尼小尼/帮我…"）
  → 会议意图分流（"开始录音/结束录音" → meeting 模块，不打扰 DSH）
  → 语音复述确认（可选）→ session.prompt(mode=queue) 发送到专用 command 会话
  → 轮询 session.history 等最终回复（running=false 或事件序列稳定）
  → brief_text() 精简 → 语音简报（edge-tts→SAPI 兜底）+ 命令历史入库
```

要点：
- **专用会话**：命令与纪要分别绑定 `command` / `summary` 会话，避免与人工聊天混流。
- **简报清洗**：markdown/URL/emoji/来源行去除，截断到 maxBriefChars（已实测验证的规则）。
- **并发保护**：`busy` 锁，同一时刻只允许一条命令流，防止误触叠加。

## 5. 会议模块（meeting.py）

- 录音：`MeetingRecorder` 线程，按 `meetingSegmentMinutes` 自动分段写 `data/meetings/<时间戳>/NN.wav`，实时电平回调。
- 转写：whisper（`meetingSttModel`）或 SenseVoice（whisper 取时间戳骨架 + 字符级对齐切句）。
- 说话人分离：pyannote 全离线（`models/pyannote`），`SpeakerRegistry` 跨片段保持身份，可改名/合并。
- 声纹识别（常用联系人）：转写时留存每个说话人的平均嵌入（`speaker_embeddings`）；
  把说话人**改名成联系人**即把样本存进声纹库（`voiceprints`，可关）；下一次会议转写时
  逐段与库做余弦匹配，过「阈值 + 与次优的间隔」两道门才自动命名（宁缺毋滥），
  同一联系人被分离成多簇时自动合并；已有会议可用「识别本场」重跑（不重新分离）。
- 纪要：导出 `transcript.md` → DSH `summary` 会话生成 → `summary.md`；可追加要求重新生成。
- 未来扩展：`lines.kind` 支持把句子标记为待办/决议；新增"生成汇报稿/行动项清单"等只需
  增加 DSH 侧 prompt 模板 + 面板按钮，数据模型无需变更。

## 6. Windows 稳定性设计

| 风险 | 对策 |
|---|---|
| CUDA 模型重复加载崩溃（WinError 127） | 每引擎进程内单例，失败自动回退 CPU（已实测验证） |
| 热键依赖 C# 编译/PowerShell 宿主 | ctypes `RegisterHotKey` + `WH_KEYBOARD_LL`，纯 Python 常驻线程 |
| 唤醒子进程保活复杂 | KWS 直接在主进程线程，配置实时生效 |
| DSH 挂掉影响面板 | `manager` 按 pid 启停 + 端口探活，面板显示 online/offline |
| 崩溃不留痕 | 所有异常写 `logs` 表 + `_transcribe-errors` 式兜底日志 |
| 误触发 | 命令 busy 锁；唤醒确认帧 + 冷却 + 静音门控 |

## 7. 扩展路径

- **手机 App（已预留）**：REST API + CORS 全开；`api_keys` 表 + `Authorization: Bearer`
  鉴权（设置里开启 `apiAuthEnabled`）；`events` 表可做推送/统计；会议/历史接口即查即用。
- **更多触达**：托盘图标（后续可在 runtime 加 NotifyIcon 常驻）、快捷命令 App 内嵌、DSH 技能调用 API。
- **会议编辑生成**：lines.kind + summary 模板化，详见第 5 节。
- **多人/云同步**（远期）：`settings`/`events` 已是键值+时间戳结构，可增量同步。

## 8. 模块地图

| 文件 | 职责 |
|---|---|
| `app/db.py` | 数据层（schema/迁移/各域 CRUD） |
| `app/config.py` | 配置定义与读写（DB settings 表） |
| `app/dsh.py` | DSH RPC 客户端 + 会话管理 + 回复轮询 |
| `app/assistant.py` | 命令流水线 + 简报 + 通知 |
| `app/meeting.py` | 会议业务编排 |
| `app/audio/stt.py` | 转写引擎（whisper/sensevoice/sherpa） |
| `app/audio/tts.py` | edge-tts/SAPI 合成 + 提示音 |
| `app/audio/recorder.py` | 命令录音 + 会议录音线程 |
| `app/audio/wake.py` | KWS 唤醒线程 |
| `app/audio/diarize.py` | pyannote 说话人分离 |
| `app/voiceprint.py` | 常用联系人声纹库（采样入库 / 余弦匹配 / 库管理） |
| `app/hotkey.py` | ctypes 热键/媒体键钩子 |
| `app/runtime.py` | 监听器生命周期（按配置拉起/热重载） |
| `app/boot.py` | 启动编排器（组件注册表/状态机/分阶段调度/重试启停） |
| `app/manager.py` | DSH 进程启停 + 自 pid |
| `app/services.py` | 组件状态上报/快照 |
| `app/llm_router.py` | 模型路由配置读写 + 把模型组注册进 DSH（`~/.dsh/settings.yaml`，幂等往返写入） |
| `app/router_admin.py` | 面板用的路由视图：注册态、成员（通道号/昵称）、派发统计与熔断状态聚合 |
| `app/failover_proxy.py` | 路由进程的启停/探活（boot 组件「模型路由」的后端） |
| `app/api.py` / `app/main.py` | REST 路由 + 应用工厂 + 静态托管 |
| `web/` | 控制面板 SPA |
| `dsh-failover/` | 模型路由进程（OpenAI 兼容入口，默认 `127.0.0.1:8899`、可改 `config.json` 的 `port`）：按通道派发 + 主动探测 + 熔断 |
| `sidebar/` | 右缘折叠条/边条宿主（C# WinForms + WebView2，`echo-sidebar.exe`） |
