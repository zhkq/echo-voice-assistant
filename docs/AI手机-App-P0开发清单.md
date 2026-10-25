# ECHO 手机 App · P0 开发清单

> 版本：v1.0（2026-10-22）
> 配套：[主设计](AI手机-ECHO-App-设计.md) · [5 页线框](AI手机-App-线框.html) · [MCP 工具契约](AI手机-MCP工具契约.json) · [ECHO 接口验收](AI手机-ECHO接口验收.md)
> 目标平台：AI 手机自有开发平台（以 Android 能力模型为基线）
> 原则：**P0 只做“配对 → 录音 → 发指令 → 回顾”闭环 + 5 页骨架 + 7 个 MCP 工具**；其余明确放 P1/P2。

---

## 0. P0 的 Definition of Done

在真机上完成下面这条主链路，且失败路径都有明确文案，才算 P0 完成：

1. 电脑面板生成 6 位配对码 → App 输入 → 连上 ECHO（局域网或中继）。
2. 首页点「开始录音」→ 手机麦克风开录 → 第一段落盘后 ECHO 建会 → 段按序上传 → 点「结束」→ ECHO 开始转写。
3. 首页输入一条文字指令 → 拿到 `commandId` → 轮询到 `done` → 显示 `brief`（并可按设置朗读）。
4. 首页点「开始回顾」→ 建今天的回顾会话 → 录一轮 → 上传 `/daily-review/turn` → 显示/播放 `spoken` → 结束回顾。
5. 三个历史页签能打开、分页、看详情（会议详情 P0 只到纪要；逐字稿/音频/议题是 P1）。
6. 设置页能配对、看连接、开关 MCP、复制智能体配置。
7. MCP 的 7 个 P0 工具在真机走通：`echo_status`、`echo_meeting_start`、`echo_meeting_stop`、`echo_command_send`、`echo_command_wait`、`echo_review_start`、`echo_review_stop`。
8. 失败路径：未配对、离线、401、麦克风拒绝、ECHO 忙、回顾未就绪——都有各自的话，不静默、不画假空态。

**不算 P0**：语音指令（P1）、TTS 双引擎（P1；P0 只用系统 TTS 或先不朗读）、会议逐字稿/音频/议题（P1）、离线完整补传与暂停补报（P1）、MCP 暂停/恢复/朗读/会议读工具（P1）、搜索（P2）、LAN MCP（P2）。

---

## 1. 依赖与前置

| 依赖 | 说明 | 谁提供 |
|---|---|---|
| ECHO dev 2.1.7+ | 必须支持 `source=phone&clientId`、`/meeting/segment`、`commandId`、`/daily-review/turn`、`/meeting/pauses` | ECHO 侧 |
| 配对可达 | 局域网：`serverBindMode=lan`（自动强制鉴权）；中继：`relayEnabled` 且已连后端 | 用户/ECHO 侧 |
| 平台能力 | 本地回环 HTTP、前台服务、麦克风权限、通知权限 | 手机平台 |
| 智能体 | 本机运行、能连 `127.0.0.1:18790`、支持 MCP HTTP + Bearer | 手机智能体平台 |
| 契约 | [主设计 §6](AI手机-ECHO-App-设计.md) 的字段为准；[MCP JSON](AI手机-MCP工具契约.json) 为工具导入源 | 本套文档 |

> 任一平台能力不满足：按主设计 §7.3 的降级处理，不要偷偷改契约。

---

## 2. 里程碑

| 里程碑 | 内容 | 出口 |
|---|---|---|
| **M0 骨架** | 工程、主题、5 页导航、状态模型、空页面 | 5 个页签可切换；无业务 |
| **M1 配对** | 配对页、claim、令牌存储、连接探活、状态 bar | 真机配对成功；401/离线可分 |
| **M2 录音** | 本地录音、清单、段上传、建会/停会、前台服务 | 录一场、传上去、主机开始转写 |
| **M3 指令** | 文字发送、`commandId`、轮询、详情、历史 | 一条指令端到端 |
| **M4 回顾** | 建会话、一轮语音、文字兜底、结束、历史 | 一次回顾端到端 |
| **M5 历史** | 会议列表/纪要详情、回顾按天、指令分页 | 三个页签可用 |
| **M6 MCP** | 服务、鉴权、7 工具、生命周期、审计、配置 UI | 智能体调 7 工具 |
| **M7 硬化** | 四态、权限、日志脱敏、测试矩阵、真机验收 | 本文 §0 全部通过 |

依赖顺序：`M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7`；M5 可与 M3/M4 并行，M6 依赖 M2/M3/M4 的控制器。

---

## 3. 任务分解

### M0 骨架

| ID | 任务 | 交付 | 验收 |
|---|---|---|---|
| M0-01 | 工程与构建 | 可安装的空 App；包名/签名/图标 | 真机装上能打开 |
| M0-02 | 设计令牌 | 颜色/字号/间距/圆角；深色模式 | 与线框一致；对比度 ≥4.5:1 |
| M0-03 | 5 页导航 | 首页/指令/会议/回顾/设置；详情页路由；返回 | 页签顺序正确；返回不丢页签 |
| M0-04 | `SessionController` | 连接/录音/回顾/指令四组状态；`StateFlow`（或平台等价） | UI 只读它；无第二份状态 |
| M0-05 | 本地存储骨架 | token、prefs、cache 的读写接口 | 重启后 token 还在（若已配对） |

### M1 配对与连接

| ID | 任务 | 交付 | 验收 | 接口 |
|---|---|---|---|---|
| M1-01 | 配对页 | 6 位码输入、地址、扫码（可选）、连接按钮 | 码输完自动聚焦；错误就地显示 | — |
| M1-02 | claim 客户端 | `POST /api/pair/phone/claim` | 成功存 `token/keyId/baseUrl`；403 不自动重试 | §6.2 |
| M1-03 | 令牌存储 | Keystore/EncryptedPrefs；禁日志/备份 | 抓日志无 token；卸载重装需重配 | — |
| M1-04 | 连接探活 | `GET /api/status`（Bearer）；`/health` 区分服务/令牌 | 401 → `AUTH_FAILED`；超时 → `OFFLINE` | §6.3 |
| M1-05 | 状态 bar | 连接/离线/401/录音/补传 | 与主设计 §4.1 状态表一致 | — |
| M1-06 | 解绑/重配 | 清本地 token 与缓存；重新配对 | 不调用 `/api/keys` | §6.9 |

### M2 录音（手机麦）

| ID | 任务 | 交付 | 验收 | 接口 |
|---|---|---|---|---|
| M2-01 | 录音器 | 16 kHz 单声道 PCM16；30–60 秒一段；WAV 落盘；**不要直传 m4a/amr** | 1 小时录音内存稳定；文件可播 | — |
| M2-01A | 格式兜底（条件任务） | 仅当平台录音器只能出 m4a/amr：本地解码 → 16k 单声道 PCM16 WAV → 再上传；转码在前台服务里做 | 转码后的 WAV 能被 `/meeting/segment` 收下；AMR 质量风险已告知 | §6.5 |
| M2-02 | 本地清单 | `clientId`、`startedAt`、段序号/路径/格式、上传状态 | 杀进程后能恢复清单 | §8.1 |
| M2-03 | 建会 | 首段落盘后 `POST /api/meeting/start?source=phone&clientId&startedAt` | 重复调用 `duplicate=true`；不建空会 | §6.5 |
| M2-04 | 段上传 | `POST /api/meeting/segment?clientId&seq&startedAt&source=phone` 裸 wav | 重传不重复；`segmentCount` 对账 | §6.5 |
| M2-05 | 停会 | 收尾尾段 → `POST /api/meeting/stop?clientId` | 主机开始转写；不误停别场 | §6.5 |
| M2-06 | 前台服务 | 录音通知、麦克风指示、锁屏继续 | 通知可见；点通知回 App | §9.1 |
| M2-07 | 失败处理 | 权限拒绝/麦克风被占/存储不足 | 每类有独立文案；不静默 | — |
| M2-08 | 中断恢复 | 进程死亡后重启：标中断、可补传/结束 | 不自动续录 | §5.2 |

> P0 不做：暂停/恢复（P1）、离线完整补传（P1）、暂停补报（P1）。但**段上传失败重试**要在 P0 做，否则弱网必挂。
> 线框里的「暂停」按钮属于 P1；P0 的录音卡只放「开始 / 结束」。
> **格式**：会议段只收 WAV/FLAC；m4a/amr 直传会 409（语音/回顾是 422）。平台只能出 m4a/amr 时，M2-01A 必做；AMR 是 8k 窄带，不建议会议用。

### M3 指令（文字）

| ID | 任务 | 交付 | 验收 | 接口 |
|---|---|---|---|---|
| M3-01 | 输入与发送 | 首页输入行 + 发送；防双击 | 空文本不发；发送中禁用 | §6.4 |
| M3-02 | 发送 | `POST /api/assistant/command` `{text,source:"mobile"}` | 拿到 `commandId`；200+ok=false 显示 message | §6.4 |
| M3-03 | 轮询 | `GET /api/commands/{id}` 1s→2s→3s | done 显示 `brief`；failed 显示 `error`；5 分钟超时 | §6.4 |
| M3-04 | 指令历史 | `GET /api/commands?limit=30&offset` 分页；按天分组 | 触底加载；失败不画空态 | §6.4 |
| M3-05 | 指令详情 | 原文/回复/摘要/耗时/状态 | `brief` 优先；`reply` 可展开 | §6.4 |
| M3-06 | 朗读（可选） | 系统 TTS 念 `speakText`/`brief` | 可在设置关；失败不阻塞 | §6.7 |

### M4 回顾

| ID | 任务 | 交付 | 验收 | 接口 |
|---|---|---|---|---|
| M4-01 | 开始 | `POST /api/daily-review/start`；进入回顾模式 | `ready=false` 显示 `reason`；幂等 | §6.6 |
| M4-01A | 准备 | `POST /api/daily-review/pre`（立刻回 `ack/topics/etaSec`）→ 2.5s 轮询 `GET /pre` → 完成显示 `pre.reply`；`POST /pre/stop` 跳过 | 不碰麦；`phase=done && finished` 才算完成；旧 ECHO 404 降级；再调 `already/done` 不重复扫 | §6.6 |
| M4-02 | 一轮语音 | 录一轮 → `POST /api/daily-review/turn` 裸 wav | 显示 `text`/`spoken`；长超时 ≥180s；`ok=false` 单独提示 | §6.6 |
| M4-03 | 文字兜底 | `POST /api/daily-review/submit` | 2–3 分钟不崩；按钮防双击 | §6.6 |
| M4-04 | 结束 | 本地停 + `POST /api/daily-review/stop` | `stopped=false` 不报错 | §6.6 |
| M4-05 | 回顾历史 | `GET /api/daily-review/history`；按天 | 轮数/播报预览；失败有文案 | §6.6 |
| M4-06 | 某天详情 | `GET /api/daily-review/history/{date}` | 播报/轮次/`registered` 三态 | §6.6 |

> **绝不调用 `/daily-review/go`**：那会开电脑的麦。抓包验收。

### M5 历史与详情

| ID | 任务 | 交付 | 验收 | 接口 |
|---|---|---|---|---|
| M5-01 | 会议列表 | `GET /api/meetings?limit=20&offset` | 状态 chip、来源、摘要预览、转写进度 | §6.5 |
| M5-02 | 会议纪要 | `GET /api/meetings/{id}/file?kind=summary` 的 `content` | JSON 解析；`summaryState` 三态 | §6.5 |
| M5-03 | 转写进度 | `GET /api/transcribe/status` | 键可能是 id 或 name；不假设数字 | §6.5 |
| M5-04 | 回顾详情 | 轮次列表 + 播报文本 | `registered` 为 `null` 不说话 | §6.6 |

> P1 才做：议题、逐字稿、音频播放、重新转写。

### M6 MCP（7 个 P0 工具）

| ID | 任务 | 交付 | 验收 | 依据 |
|---|---|---|---|---|
| M6-01 | HTTP 服务 | `127.0.0.1:18790/mcp`；JSON-RPC 2.0；无会话 | `initialize`/`ping`/`tools/list` 正常 | §7.2 |
| M6-02 | 鉴权 | Bearer MCP Token；Host/Origin 校验 | 无/错 token 401；非回环 Host 403 | §7.2/§7.7 |
| M6-03 | `echo_status` | 连接/录音/回顾/主机状态 | 不含 ECHO Token/MCP Token/`dshWebUrl` | §7.4 |
| M6-04 | `echo_meeting_start` | 调 `SessionController.startMeeting()` | 幂等；权限/后台限制有错误码 | §7.4 |
| M6-05 | `echo_meeting_stop` | 调 `stopMeeting()` | 空闲幂等；返回 syncing/transcribing | §7.4 |
| M6-06 | `echo_command_send` | 调 `CommandController.send()` | 返回 roundId；忙/离线有错误码 | §7.4 |
| M6-07 | `echo_command_wait` | 轮询 `GET /api/commands/{id}` ≤25s | 超时返回 running，不报错 | §7.4 |
| M6-08 | `echo_review_start` | 调 `ReviewController.start()` | `not_ready` 带 reason；幂等 | §7.4 |
| M6-09 | `echo_review_stop` | 调 `ReviewController.stop()` | 空闲幂等 | §7.4 |
| M6-10 | 生命周期 | 开关、前台服务、端口占用、进程死亡 | 关闭即解绑；端口冲突明确报错 | §7.3 |
| M6-11 | 审计/限流 | 最近 200 次；120 次/分钟 | 不记正文；超限 `rate_limited` | §7.7 |
| M6-12 | 设置 UI | 开关、端点、令牌显示/复制/轮换、允许操作、最近调用、复制配置 | 令牌轮换旧配置 401 | §4.5 |

> P1 才做：`echo_meeting_pause/resume`、`echo_speak`、`echo_review_say`、`echo_meetings_list`、`echo_meeting_get`。

### M7 硬化与验收

| ID | 任务 | 交付 | 验收 |
|---|---|---|---|
| M7-01 | 四态 | 空/加载/失败/离线 | 每页都有；失败不画空 |
| M7-02 | 权限流 | 麦克风/通知/电池 | 拒绝有解释和「去设置」 |
| M7-03 | 日志脱敏 | 不记 token/正文/音频路径 | 抓日志验证 |
| M7-04 | 弱网 | 段重试、超时、断网恢复 | 断网 2 分钟可恢复上传 |
| M7-05 | 安全负例 | 401/403/409/422；MCP 越权 | 按 §10 清单逐条过 |
| M7-06 | 真机演示 | 主链路脚本 | §0 的 8 条全过 |

---

## 4. 测试矩阵

| 层 | 用例 | 判据 |
|---|---|---|
| 单元 | 连接状态机 | UNPAIRED/PAIRING/CONNECTED/OFFLINE/AUTH_FAILED 迁移正确 |
| 单元 | 会议状态机 | IDLE→STARTING→RECORDING→STOPPING→SYNCING→DONE/FAILED |
| 单元 | 回顾状态机 | IDLE→PREPARING→ACTIVE→SUBMITTING→ACTIVE→ENDED |
| 单元 | WAV 写盘 | 16k/mono/PCM16；时长与采样数一致 |
| 单元 | DTO 解析 | 缺字段/未知枚举不崩；`segments` 在列表是数组、详情是数量 |
| 单元 | 令牌存储 | 不落日志；轮换后旧值失效 |
| 集成 | 配对 | 正确码/错码/过期/锁定/中继 |
| 集成 | 录音 | 建会幂等、段幂等、停会护栏、段数对账 |
| 集成 | 指令 | 发送/轮询/失败/超时/重复点击 |
| 集成 | 回顾 | start/pre/turn/submit/stop；`/go` 从未被调用 |
| MCP | 协议 | initialize/tools/list/tools/call/ping；未知方法 -32601 |
| MCP | 鉴权 | 无 token 401；错 token 401；Host/Origin 403；轮换 401 |
| MCP | 工具 | 7 个 P0 工具成功/失败/幂等/并发 |
| 设备 | 后台 | 锁屏、切后台、Doze、杀进程 |
| 设备 | 网络 | Wi-Fi↔蜂窝、断网、中继切换 |
| 安全 | 越权 | App 不调 `/api/keys`、`/api/settings`、`/api/logs`；MCP 无管理工具 |

---

## 5. 风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| 平台禁止后台开麦 | Agent 调 `echo_meeting_start` 失败 | 返回 `foreground_required` + 通知；用户点开 App 再开 |
| 平台禁止常驻本地端口 | MCP 不可用 | 改用平台工具注册（同一份 schema）；或只在前台可用 |
| 智能体在云端 | 连不到 `127.0.0.1` | 不在本文范围；需要端侧代理/公网通道 |
| 中继丢头/无 SSE | MCP 不能依赖会话 | MCP 只做无会话 JSON；ECHO REST 也不依赖自定义头 |
| 回顾一轮 2–3 分钟 | 客户端超时/用户以为卡死 | 长读 180s；显示“整理中”；按钮防双击 |
| 手机时钟不准 | `startedAt` 偏 | 服务端分开记 `receivedAt`；App 用本地开录时刻 |
| 段乱序/重传 | 转写重复/缺失 | `(clientId, seq)` 幂等；按 seq 补传；对账段数 |
| 进程被杀 | 录音中断/清单丢 | 清单落盘；不自动续录；可补传/结束 |
| 令牌泄露 | 别人能控制 ECHO | Keystore；不日志；可吊销；MCP 令牌独立 |
| 录音器只出 m4a/amr | 直传 409/422，会议传不上去 | 本地转 WAV（M2-01A）；AMR 质量差不建议会议；优先 `AudioRecord` 直接录 WAV |

---

## 6. 交付物清单

- [ ] 可安装的 P0 APK/包。
- [ ] 5 页 + 配对页 + MCP 页。
- [ ] MCP 服务（7 工具）与 `复制智能体配置`。
- [ ] 真机演示脚本（按 §0 的 8 条）。
- [ ] 测试报告（§4 矩阵逐项）。
- [ ] 已知问题清单（含 P1 待办）。
- [ ] 抓包证据：`/daily-review/go` 未被调用；MCP 无 ECHO Token 输出。

---

## 7. 接口依赖清单（P0 冻结）

P0 只依赖下面这些字段；改字段先改 [主设计 §6](AI手机-ECHO-App-设计.md) 与 [ECHO 接口验收](AI手机-ECHO接口验收.md)：

| 用途 | 接口 | 关键字段 |
|---|---|---|
| 配对 | `POST /api/pair/phone/claim` | `token,keyId,baseUrl` |
| 状态 | `GET /api/status` | `version,agent,busy,busyOwner,meeting` |
| 指令 | `POST /api/assistant/command` | `ok,message,commandId` |
| 指令结果 | `GET /api/commands/{id}` | `status,brief,reply,speakText,error` |
| 指令列表 | `GET /api/commands` | `total,items[]` |
| 建会 | `POST /api/meeting/start` | `meetingId,duplicate,clientId` |
| 传段 | `POST /api/meeting/segment` | `duplicate,segmentCount,seqCount` |
| 停会 | `POST /api/meeting/stop` | `ok,message` |
| 会议列表 | `GET /api/meetings` | `items[]` |
| 会议详情 | `GET /api/meetings/{id}` | `lines,speakers,segment_count,hasSummary` |
| 纪要 | `GET /api/meetings/{id}/file?kind=summary` | `exists,content,summaryState` |
| 回顾开始 | `POST /api/daily-review/start` | `sessionId,accessOk,error` |
| 回顾一轮 | `POST /api/daily-review/turn` | `text,spoken,registered,error` |
| 回顾文字 | `POST /api/daily-review/submit` | `spoken,registered,error` |
| 回顾结束 | `POST /api/daily-review/stop` | `ok,stopped` |
| 回顾历史 | `GET /api/daily-review/history` | `items[]` |
| 回顾详情 | `GET /api/daily-review/history/{date}` | `turns[]` |

---

## 8. 一页排期（按开发顺序）

| 周 | 内容 | 周末应能演示 |
|---|---|---|
| 第 1 周 | M0 骨架 + M1 配对 | 真机配对、状态 bar |
| 第 2 周 | M2 录音（本地/建会/传段/停会） | 录一场、主机开始转写 |
| 第 3 周 | M3 指令 + M5 会议列表/纪要 | 发指令、看会议 |
| 第 4 周 | M4 回顾 + M5 回顾历史 | 一次回顾端到端 |
| 第 5 周 | M6 MCP 7 工具 + 设置 | 智能体调 7 工具 |
| 第 6 周 | M7 硬化 + 验收 | §0 的 8 条全过 |

> 这是**单人或小团队**的粗排期；并行开发时 M5 可提前，M6 可在 M2–M4 控制器稳定后插入。

---

## 9. 十分钟演示脚本

1. 电脑面板生成 6 位码；App 输入 → 显示「已连接 · 局域网 · v2.1.7」。
2. 首页点「开始录音」→ 通知出现、计时开始；说 10 秒；点「结束」。
3. 打开「会议」页 → 这场显示「转写中」→ 完成后显示「已完成」；点开看纪要。
4. 回首页发一条文字指令「请只回复：收到」→ 显示「已提交」→ 完成后显示摘要。
5. 点「开始回顾」→ 录一句 → 显示「整理中」→ 播放/显示 `spoken` → 点「结束回顾」。
6. 打开「回顾」页 → 今天有一条；点开看播报。
7. 设置 → 智能体接入：打开 MCP → 复制配置 → 在智能体里问「ECHO 现在在录吗？」（调 `echo_status`）。
8. 让智能体调 `echo_meeting_start` / `echo_meeting_stop`；确认首页状态同步变化；抓包确认没有 `/daily-review/go`、MCP 输出里没有 ECHO Token。
