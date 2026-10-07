# ECHO 手机触点（Android APK）设计

> 起因（用户 2026-10-07 原话）：
> *"今天还要开始设计手机 app 作为 echo 的触点，手机作为 echo 的前置触点，跟 echo 配对，
> 用来完成交互，你先设计一下，先设计 apk 格式，未来再想 ios"*
>
> 本文是**设计**，不是实现。每一步都标了"现状 / 要新增 / 判据"，可以直接照它开工。
> 全部事实都来自代码（带 `路径:行号`），不靠印象。

---

## 0. 先把"手机与 ECHO 是什么关系"钉死

一句话：**手机是 ECHO 主机的第二个前端**（第一个是 DSH 边条里那个面板），
**不是** ECHO 的另一个后端，**也不参与** GPU 能力后端那套配对。

这条边界决定了后面所有设计，而且它有一个很实在的收益：**手机端不需要新造任何凭据体系**。

| 关系 | 谁 ↔ 谁 | 凭据 | 产物 |
|---|---|---|---|
| **能力后端配对**（既有） | ECHO 主机 ↔ 另一台 GPU 机器 | 一次性配对码 → `clientId`/`secret` | `{DATA}/backend.json`（DPAPI 包封） |
| **手机 ↔ ECHO**（本设计） | 手机 ↔ ECHO 主机 | **API Key（Bearer）** | 手机侧安全存储 |

⚠️ **明确不做**：把 `backend.json` / `local-pair.json` / DPAPI 包封搬到手机上。
理由（调研实证）：
* `pairing.pair()` 的结果是"**主机**去连 GPU 后端"的凭据（`app/capabilities/pairing.py:269-325`）；
* `local-pair.json` 是**后端启动时写给主机的**（`server/localpair.py:51,60-80`），语义就是本机；
* `backend.json` 的 secret 在 Windows 上用 DPAPI 包封（`credentials.py:62-72`）——
  那是**用户+机器绑定**的，搬到手机毫无意义。

> 用户说的"跟 echo 配对"，在手机这条链路上应当是**"让手机拿到访问这台 ECHO 的钥匙"**，
> 而不是复用 GPU 后端那套配对。

---

## 1. 手机只做四件事（判据：凡不属于这四件的一律放 ECHO 侧）

1. **收**：开麦录音（一段话，或按键说话）
2. **放**：把 ECHO 回来的音念出来
3. **显**：显示这一轮的文字（用户原话 / ECHO 回复）
4. **连**：与 ECHO 保持可达、认识自己的身份、断线能重试

**不做**（全部留在 ECHO 侧）：
* 不做业务判断（会议意图 / 回顾意图 / 唤醒词匹配 —— 那是 `assistant.classify_meeting_intent`、
  `daily_review.wants_start` 的事）；
* 不做转写（手机只把音频发上去，见 §2 的三档）；
* 不做技能编排（DSH 那边的事）；
* **不存历史**（历史在 ECHO 的库里；手机只做缓存与离线队列，见 §7）。

---

## 2. 语音链路的三档，以及为什么默认选"全远端"

手机录音 → 转写 → 发命令 → 等回复 → 念出来。每一环都有"本机 / 远端"两种选择：

| 档 | 转写在哪 | 念在哪 | 优点 | 缺点 |
|---|---|---|---|---|
| **A 全远端**（默认） | ECHO 主机（本机引擎或能力后端） | ECHO 合成，下发音频 | 手机零模型、与主机能力一致、换模型不用更新 APK | 需要网络；每次交互有往返 |
| B 手机本机 | 手机（sherpa-onnx / Android） | 手机系统 TTS | 离线可用、延迟低 | APK 体积大、两套模型要各自维护、识别效果与主机不一致 |
| C 混合 | 手机先试离线，失败转远端 | 优先本机 TTS | 折中 | 逻辑最复杂（两套都要做对） |

**决定：先做 A**，把 B 留成"网络不好时的降级档"（§8 阶段 3）。
理由：ECHO 已经有一套**跑通的**能力后端（Qwen3-ASR）与本地引擎（sherpa），
手机再塞一套只会让"同一句话在两处识别结果不同"，而用户最不需要的就是这种不确定性。

### 2.1 A 档的三条已知缺口（必须先在 ECHO 侧补齐）

| 缺口 | 现状（代码） | 影响 |
|---|---|---|
| **① 网络可达性** | 只绑回环 `app/main.py:254`；且 `netguard` 要求 Host 回环，否则 403（`app/netguard.py:74-83`，装载 `main.py:147-148`） | 手机**根本连不上**。这是**第一优先**要解决的 |
| **② 没有"文本→音频"接口** | 唯一的 TTS 接口是 `POST /api/control/tts/test`（`app/api.py:2126-2131`），**只在本机喇叭播、不回音频**；`server/` 侧也没有 TTS 端点 | 手机收不到"要念的内容"，只能自己合成（那就退化成 B 档） |
| **③ 没有"上传音频→一句话"的单一入口** | `POST /api/stt/transcribe`（`api.py:1088-1112`）只回**文本**；`POST /api/assistant/command`（`api.py:609`）只收**文本** | 两步能拼（手机做两次调用），但多一次往返；若要"一步到位"需新增 |

> ①②是**硬缺口**；③只是"少一次往返"，可以先用两步拼（分工更清晰，见 §3）。

---

## 3. 最小 API 增量（设计目标：**只加 3 个**）

原则：**能复用就不新增**。调研结论是"绝大部分能力已有现成接口"，
真正缺的只有三处，而且都很小。

### 新增 1：配对入口（让手机拿到钥匙）

```
POST /api/pair/phone        （新增）
  入参: {}                  （必须在**本机回环**上调用 —— 与"起本机后端"同一条信任链）
  出参: {
    "ok": true,
    "baseUrl": "http://192.168.1.170:8970",   ← ECHO 自己算出来的可达地址
    "token": "<明文，只回这一次>",              ← 复用 db.create_api_key
    "keyId": 3,
    "expiresAt": "",                          ← 先不过期（见 §6）
    "qr": "echo://phone?host=...&token=..."    ← 手机扫这个
  }
```

* **复用**：`db.create_api_key`（`app/db.py:1058-1063`，明文只回一次、库里只存 sha256）
  与 `KeyCreateIn.name` 的默认值 `"mobile"`（`api.py:167-168`）—— 这条链本来就是给手机预留的。
* **为什么不复用 `/api/capability/pair-local`**：那条是"主机去连 GPU 后端"（`api.py:245-261`），
  语义完全不同，混用会让两个概念纠缠。
* **判据**：新接口必须在 `netguard` 的"回环"分支才能调（`netguard.py:74-83`），
  即**只有坐在这台机器前的人**能发钥匙；手机侧无法自助发钥匙。

### 新增 2：语音往返（一步拿到文本）

```
POST /api/assistant/voice-command     （新增，兼容两档）
  Content-Type: audio/wav（**裸 body**，与能力后端同一约定；不是 multipart）
  查询参数: ?engine=&lang=              （可选，缺省用设置里的引擎）
  出参（立刻返回，不阻塞）: {
    "ok": true, "commandId": 41,
    "text": "明天天气怎么样",           ← 转写结果（手机可立刻显示）
    "session": "session-…"
  }
```

* **内部实现**：`stt.transcribe`（`api.py:1088` 那条路的同一段代码）→ `assistant.send_text`（`api.py:609`）。
  也就是把现有两步在服务端串起来，**不新增业务逻辑**。
* **为什么也要保留两步**：调试/降级时手机仍可分别调 `/api/stt/transcribe` 与 `/api/assistant/command`
  —— 这条新接口只是"省一次往返"的便捷档。
* **`source` 填什么**：`"mobile"` —— 库里注释与面板翻译表都早已备好（`db.py:115`、`web/app.js:1132`），
  且**目前没有任何代码写这个值**，手机正好是第一处。

### 新增 3：把一句话念出来（返回音频）

```
POST /api/tts                 （新增）
  入参: {"text": "...", "engine": "auto"（可选）, "format": "mp3"（默认，或 wav）}
  出参: 音频字节（Content-Type: audio/mpeg | audio/wav）
        失败时: 4xx/5xx + {"detail": "..."}（说清是"没装 edge-tts"还是"引擎返回空"）
```

* **复用**：`app/audio/tts.py` 的合成路径。现状是"合成后**直接本机播放并删掉临时文件**"
  （`tts.py:182-206`）—— 新接口要的是"**把那段字节返回**，不播"，所以要在
  `tts` 层加一个"合成到内存/返回路径"的入口（**不要**在 api 层重写合成逻辑）。
* **顺手修一个既有毛病**（今天刚发现的）：朗读失败时 `speak_text` 只 `print` 不写日志
  （`app/providers/__init__.py:214-228`，已在 `14e8a66b` 修为写 `warn/tts` 日志）。
  新接口的失败**必须**同样留痕，否则手机端"没声音"又会变成无头案。
* **判据**：`POST /api/tts` 返回的字节，能被 `soundfile` 解码出**非空**波形；
  且当 `ttsEngine=off` 时返回 **409**（不是静默成功）——"关了朗读"与"合成失败"必须能区分。

### 明确**不新增**的

| 看似需要 | 其实用现成的 |
|---|---|
| 查状态 | `GET /api/status`（`api.py:388`）—— ⚠️ 注意：**开了 `apiAuthEnabled` 之后它也要 token**（`api.py:388-389`），与注释/文档说法相反 |
| 查这一轮的回复 | `GET /api/commands?limit=`（`api.py:909`），按 `id` 取那条即可 |
| 会议 | `/api/meeting/*`、`/api/meetings/*`（`api.py:1153-1660`）全套现成 |
| 每日回顾 | `/api/daily-review/*`（`api.py:670-790`）全套现成，含 `/go`（`api.py:719`，**真进模式**） |
| 看某个会话 | `GET /api/commands/{id}/session`（`api.py:930`） |

---

## 4. 配对流程（人只做一步：扫码）

```
[ECHO 主机]                                    [手机]
面板「设置 → 手机」点「配一台手机」
   → POST /api/pair/phone（本机回环）
   → 显示二维码（内含 echo://phone?host=…&token=…）
                                             打开 App → 首页「扫一扫」
                                                → 存 baseUrl + token（Android Keystore）
                                                → 调 GET /api/status 自检
                                                → 首页显示「已连：<主机名>」
```

* **二维码内容**：`echo://phone?host=<ip:port>&token=<明文>&name=<主机名>`（与既有
  `echo://pair?host=…&code=…&fp=…` 形态一致，便于复用同一套解析习惯；
  但 **scheme 用 `phone` 而不是 `pair`**，避免与 GPU 后端配对混淆）。
* **为什么必须扫码而不是手输**：token 是长随机串，手输必错；且二维码能顺便带上**正确的
  LAN 地址**（多网卡机器上"我该填哪个 IP"是个真问题 —— 由 ECHO 自己算，见 §5）。
* **重配**：面板「已配手机」列表（复用 `GET /api/keys`，`api.py:2196`）+ 「撤销」按钮
  （`DELETE /api/keys/{kid}`，`api.py:2202`）；撤销后手机下次调用得 401，App 提示"请重新扫码"。

---

## 5. 网络可达性设计（**第一优先**，不然上面全部无从谈起）

现状：`app/main.py:254` 只绑 `127.0.0.1`，且 `netguard` 拒绝非回环 Host（403）。

**方案（分三步走，每一步都能单独验收）：**

### 步 1：加一个"允许局域网"的开关（**默认关**）

* 新增设置 `serverBindMode`（`loopback` 默认 / `lan`）+ `serverLanHost`（绑哪个网卡，留空=所有）。
* 打开 `lan` 时 `netguard` 必须相应放行**内网地址段**（不是"放行一切"）：
  私有网段白名单（`10/8`、`172.16/12`、`192.168/16`、`fe80::/10` 等），
  与 `app/netloc.py` 已有的回环判定放在一起（**一处判据**）。
* **判据**：开了之后 `GET /api/status` 从手机（同网段）能通、从公网不能通；
  **且开了 `lan` 就强制要求 `apiAuthEnabled=true`**（否则等于把面板裸奔在局域网上）——
  这两项要**联动**，不能各改各的。

### 步 2：面板里明确"这是给手机开的"

* 「设置 → 手机」那张卡里显示：当前绑在哪、局域网地址是什么、开了会不会有风险、怎么关。
* **判据**：文案必须说清"开了之后，同一局域网内**拿到 token 的设备**可以下命令、开关机、删会议"
  （因为 scopes 现在**不校验**，见 §6）。

### 步 3（可选，推荐）：反代 + TLS

* 现有文档已给正解（`docs/DEPLOY.md:198-204`：反代 + Basic Auth + TLS + `apiAuthEnabled`）。
* 手机端**固定 https 时才用**；App 里做"仅允许 https 或本机 http"的校验。

> ⚠️ 不做的：**不把 ECHO 直接暴露到公网**。手机触点的使用场景是"同一个家的 wifi / 同一个内网"。

---

## 6. 安全设计（现在**必须**说清，因为有一个已知的坑）

### 已知坑：`api_keys.scopes` **存了但没有任何地方校验**

调研实证：`app/` 里 `scopes` 只出现在表定义/读写与 `backend_admin.py:252` 一句文案，
**没有任何按 scope 判权**。后果：**手机 token 等于全权** —— 能关机、重启、删会议、
改设置。对"手机触点"这个场景这**太宽**了。

**设计决定（两档，先做 A）：**

* **A（本期）**：**如实告知 + 白名单式收窄**。加一个"手机端只许调这些接口"的服务端闸
  （按 token 的 `name=="mobile"` 或新增 `kind` 字段判），
  **默认白名单**：状态查询、命令下发、命令查询、STT、TTS、回顾读写、会议只读。
  **默认拒绝**：`/api/control/echo/stop`、`/api/system/restart`、`/api/meetings/{id}` 的 DELETE、
  `/api/settings` 的 PUT。
* **B（后续）**：真正按 `scopes` 判权（表已经在了，只差校验点），并把 scopes 下发到手机端展示。

**另外两条底线：**

* token **只存 Android Keystore**（EncryptedSharedPreferences），不写日志、不进备份。
* App 侧**不缓存**任何 ECHO 业务明文（历史只现取现显），避免手机丢了一个人就看到全部工作记录。

---

## 7. 手机端架构（Android，Kotlin + Compose）

```
:app
├─ data
│   ├─ EchoApi        （Retrofit/OkHttp；Bearer 拦截器；超时与重试）
│   ├─ PairStore      （baseUrl + token，EncryptedSharedPreferences）
│   └─ Outbox         （离线队列：待发的音频/文本，Room；联网后按序补发）
├─ audio
│   ├─ Recorder       （AudioRecord → 16k 单声道 wav；VAD：静音 1.4s 收尾，
│   │                  与主机的 dailyReviewSilenceMs 语义一致，避免"两边判静音不一致"）
│   └─ Player         （ExoPlayer/MediaPlayer 播 /api/tts 回来的音频；播完再开麦）
├─ ui
│   ├─ VoiceScreen    （主界面：一个大按钮 + 波形 + 本轮文字 + 上一轮回复）
│   ├─ HistoryScreen  （现取现显 GET /api/commands）
│   └─ SettingsScreen （主机地址/连通自检/重新扫码/退出）
├─ service
│   └─ ListenService  （前台服务 + 常驻通知："点一下说话"；可选持续听）
└─ widget             （桌面小组件：一键说话）
```

**关键取舍：**

| 决定 | 为什么 |
|---|---|
| **原生 Kotlin，不用 Flutter** | 音频采集/播放与前台服务的稳定性对"触点"是命门；原生能直接控 `AudioRecord`/`AudioFocus`/前台服务类型。**代价**：将来 iOS 要重写 UI 层（业务层是同一套 HTTP，不是白写） |
| **UI 只做 4 个屏** | 触点不是管理台。管理（改设置、看会议、管模型）仍在主机面板上做 |
| **不做本地 ASR（阶段 1）** | 见 §2：避免"两处识别结果不同"；降级档留到阶段 3 |
| **录音格式固定 16k 单声道 wav** | 与主机侧完全一致（`api.py:1044-1045` 的规范），避免"手机上能录、主机不收" |

### 关键时序（含"打断"这条必做的边界）

```
用户按住说话
  → 录音（本地 VAD 判停）→ POST /api/assistant/voice-command（裸 wav）
  → 立刻显示转写文本（回包里的 text）
  → 轮询 GET /api/commands?limit=1 直到这条 id 的 status=done（或 failed）
  → POST /api/tts {text: reply 的 brief} → 播放音频
  → 播放期间**不开麦**（与主机侧"录完才播"同一条纪律）
```

**打断（barge-in）**：播放中用户按说话键 → **立刻停播 + 取消这轮 TTS**（本地即可），
不要求服务端配合。这是"车里/手上忙着"场景的基本期待。

**超时**：ECHO 侧等 DSH 的上限是 `REPLY_TIMEOUT_S`（`assistant.py:90` 注释提到放宽到 300 秒）；
手机端轮询上限要**大于**它（建议 330 秒），否则会出现"手机说超时、主机其实答完了"。

---

## 8. 分期（每期都能单独验收）

| 期 | 内容 | 验收判据（能自己跑） |
|---|---|---|
| **0** | ECHO 侧：`serverBindMode`+netguard 放行内网、**强制联动 `apiAuthEnabled`** | 手机浏览器打得开 `http://<ip>:8970/`；公网打不开 |
| **1** | ECHO 侧：`/api/pair/phone` + 面板二维码 + 已配手机列表（复用 `/api/keys`） | 扫一次码，手机端拿到 token 并能 `GET /api/status` |
| **2** | ECHO 侧：`/api/tts`（返回音频，不播）+ 失败留痕 | `curl` 回来的是能解码的非空音频；`ttsEngine=off` 时 409 |
| **3** | App 骨架：扫码配对 + 状态自检 + 文字命令（先不碰音频） | 手机打字发一条命令，能在主机 `GET /api/commands` 看到 `source=mobile` |
| **4** | App 语音：录音 → `/api/assistant/voice-command` → 轮询 → `/api/tts` 播放；含打断 | 手机说一句"明天天气怎么样"，听到播报 |
| **5** | 体验：桌面小组件、前台服务、离线队列、错误人话化 | 断网时的提示能说清"是没网还是没配对" |
| **6** | 手机本机转写/朗读**降级档**（可选） | 关掉 wifi 仍能对主机说话（若在同一台机器旁） |
| **7** | iOS（本期不做设计） | — |

---

## 9. 明确不做

* **不做**手机 ↔ GPU 后端的直接配对（不该让手机碰 `backend.json`/DPAPI）。
* **不做**公网暴露（不做端口映射/内网穿透的自动化）。
* **不做**手机端技能编排（DSH 的事）。
* **不做**手机端存历史（只做缓存与离线队列）。
* **不在本期做** iOS。
* **不新增**第二套鉴权体系（就用 `api_keys`+Bearer，缺的是**收窄**而不是**换一套**）。

---

## 10. 一处必须顺手修的既有不一致（设计时发现）

`GET /api/status` 在代码里**也要 token**（`app/api.py:388-389` 带 `optional_auth`，
而 `apiAuthEnabled=true` 时 `optional_auth` 会拒绝无 token 请求），
但 `app/api.py:5` 的注释与 `docs/DEPLOY.md:204` 都写着"status 免鉴权"。
手机端**不能**依赖后者 —— 要么改文档、要么真的给 status 开豁免（**我倾向改文档**：
"探活也要钥匙"更安全，而且手机侧本来就该先有 token）。
