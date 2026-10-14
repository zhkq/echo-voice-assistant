# ECHO 触点（Android）

手机端 App，配合本机 ECHO 语音助理后端（`app/api.py`）使用：**配对 → 发文字指令 → 看指令历史**。

- 包名 / applicationId：`com.echo.contact`
- 应用名：ECHO 触点
- 当前范围：**阶段3（配对/四分区）+ 阶段4（语音链路）+ 阶段5（常驻听写，2026-10-15）**。

---

## 1. 目录结构

```
android/
├── settings.gradle.kts            # 仓库与插件源（google / mavenCentral，FAIL_ON_PROJECT_REPOS）
├── build.gradle.kts               # 根：AGP 8.5.2 / Kotlin 1.9.24（apply false）
├── gradle.properties              # -Xmx2048m、AndroidX、非传递 R 类、并行与缓存
├── local.properties               # 本机 SDK 路径（不进版本库）
├── gradlew / gradlew.bat          # Gradle wrapper（8.7）
├── tools/
│   └── real_backend_harness.py    # 真后端联调插座（临时数据根，见 §6）
└── app/
    ├── build.gradle.kts
    ├── proguard-rules.pro
    └── src/
        ├── main/
        │   ├── AndroidManifest.xml
        │   ├── java/com/echo/contact/
        │   │   ├── EchoContactApp.kt        # 把令牌来源接到 PairStore
        │   │   ├── MainActivity.kt          # 顶层路由：未配对 → 配对页；已配对 → 主页
        │   │   ├── data/
        │   │   │   ├── ApiClient.kt         # OkHttp/Retrofit 工厂 + Bearer 拦截器 + baseUrl 归一化
        │   │   │   ├── EchoApi.kt           # Retrofit 接口定义（路径自带 api/ 前缀）
        │   │   │   ├── PairStore.kt         # EncryptedSharedPreferences 存令牌
        │   │   │   └── dto/Dtos.kt          # 与后端逐字段对齐的 DTO
        │   │   ├── service/ListenService.kt # 常驻听写（阶段5）：前台服务 + 静音分段
        │   │   ├── service/ListenState.kt   # 听写状态与通知文案（纯逻辑，可 JVM 单测）
        │   │   └── ui/                      # PairViewModel / MainViewModel / 两个 Compose 页 / Theme
        │   └── res/                         # strings / themes / colors / 图标
        └── test/java/com/echo/contact/
            ├── data/dto/DtosContractTest.kt # 后端 JSON ↔ DTO 的字段契约
            ├── data/EchoApiWireTest.kt      # 真发 HTTP：路径 / body / Bearer 头
            └── data/EchoApiRealBackendTest.kt # 真后端联调（默认跳过）
```

---

## 1.5 功能范围（2026-10-09 起：四个分区，照面板的页签）

| 分区 | 做什么 | 接口 |
| --- | --- | --- |
| **仪表盘** | 整机状态：版本/运行时长/忙不忙 + **9 个组件**逐个带灯、智能体、会议摘要、回顾摘要、最近 5 条指令；**自动刷新（3 秒，与面板 `panelAutoRefresh` 一致，只在本页可见时跑）** | `GET /api/status` |
| **指令** | **录音说话**（ECHO 转写）+ 打字发送 + 本次提交的实时状态 + 朗读试听 + 历史（带关键词筛选）+ **看某条的会话** | `POST /api/assistant/voice-command`（裸 wav）、`POST /api/assistant/command`、`GET /api/commands`、`GET /api/commands/{id}/session` |
| **会议** | 状态（录音中/转写中/已转写…）+ **开始/停止录音** + 转写进度 + 会议列表 + **看正文**（转写逐行、带说话人与时间，可逐句念）+ 看纪要/主题/转写原文 | `GET /api/status`、`GET /api/meetings`、`GET /api/meetings/{id}`、`/meetings/{id}/file`、`POST /api/meeting/start|stop`、`GET /api/transcribe/status` |
| **回顾** | **口述今天**（手机录 → ECHO 转写 → 交给回顾整理 → 念播报）+ 今天做没做 + 念播报 + 按天历史 + 某天每一轮的原文/播报 + 停掉主机上正在进行的回顾 | `POST /api/stt/transcribe`、`POST /api/daily-review/submit`、`GET /api/daily-review`、`/summary`、`/history`、`/history/{date}`、`POST /api/daily-review/stop` |

### 手机**能**做回顾 —— 纠正我先前的一个错误结论

我一开始在界面上写了"手机不能开始回顾：那要占用 ECHO 主机那支麦克风"。**那是错的** ——
我只看了 `/daily-review/go`（那条确实开主机麦克风），漏了 **`POST /api/daily-review/submit`**：
它收的是**口述文本**（[api.py:867](app/api.py#L867)，注释写明"面板调试与文本兜底用"），
返回里的 **`spoken` 就是会被念出来的那句话**（`daily_review.py:349`）。

所以手机的正解是**三步**，与主机麦克风那条路得到**同一个结果**：

```
录音（16 kHz 单声道，可长） → POST /api/stt/transcribe（multipart）拿文本
                          → POST /api/daily-review/submit {text} 拿 spoken
                          → 手机 TTS 念 spoken
```

而且这条路对**车里**才是对的 —— 主机那支麦克风在车里根本听不到你说话。

两个必须处理的细节（都是读代码发现的，已实现）：

1. **`submit` 是阻塞的**：服务端要把口述交给智能体整理、等回复才返回
   （`daily_review.py:393`）。实测（2026-10-09 晚，看那两轮回顾落库的行）：
   一轮 **180.5 秒**成功、紧接着一轮 **180.9 秒**报 `DSH 在超时内没有回复`
   —— 服务端等待上限约 **180 秒**，而客户端默认读超时只有 **60 秒**。
   不改的话手机会先放弃，后端其实还在干、结果也已经落库
   （表现就是"手机上失败了，但面板里能看到那次回顾"）。所以 `ApiClient` 现在支持**按调用选超时**，
   `submit` 与转写都用 `LONG_READ_TIMEOUT_S = 330`（比服务端宽一截，保证是后端先放弃）。
2. **业务失败是 `HTTP 200 + ok=false`**（[api.py:879-883](app/api.py#L879-L883) 明说这是刻意的：
   "没等到回复不是客户端错误"），所以**不能只看状态码**；而且失败时也一定有 `spoken` 兜底
   （三层：`broadcast` / `first_paragraph` / `fallback`），车里总有一句可念。
   这条由 `EchoApiRealBackendTest` 拿真后端钉住（空数据根 → 稳定复现 `200 + ok=false`）。


### 语音走 ECHO 的转写，不走手机的系统识别

**手机侧的系统识别在这台设备上不可用**（2026-10-09 真机实测）：识别**服务**在（`isRecognitionAvailable()` 为真），
但嵌入式 API **一个回调都不给**，而且**没有任何界面**能打开
（`queryIntentActivities(RECOGNIZE_SPEECH)` = 0，且第三条 `<queries>` 声明已确认在 APK 里）。

所以正解是设计文档 §2 的 A 档：**手机只当麦克风**，录 16 kHz 单声道 wav →
`POST /api/assistant/voice-command`（**裸 body，不是 multipart**）→ 拿 `commandId` → 轮询 → **手机念**答复。

实测 ECHO 那条转写链路（TTS 造已知句 → `/api/stt/transcribe` 回读比对）：
**三句语义全对**（差别只有"中文数字→阿拉伯数字"和补标点），**139–203 ms**（SenseVoice/GPU 热态）。
> 边界：那是**合成的干净音频**，不等于车里/有噪音时的效果。

`SpeechInput.kt` / `RecognizeOutcome.kt` 与它们的用例**保留但不接界面**：
换一台识别服务对外的手机就能直接用；本机只留证据与结论，不装作能用。

### 关于「电脑放家里、用 DDNS 从公网访问」（2026-10-09 晚间分析）

> 用户提的设想：电脑留在家里，路由器把端口映射到公网，手机通过 DDNS 域名访问。
> 我读了守卫与鉴权的代码，结论是：**今天这么做是走不通的，而且不该走**；
> 正解是隧道或"带认证的反代"，两条都不需要放开 ECHO 自己的守卫。

**① 两道守卫是写死的，公网来源一律 403。**
`netguard.local_only_guard`（[netguard.py:280-299](app/netguard.py#L280-L299)）放行非回环请求要同时满足三条：
`lan_mode_enabled()` **且** `is_lan_host(host)` **且** `not is_public_peer(peer)`。
而 `is_public_peer` = `not (ip.is_private or is_loopback or is_link_local)`（[netguard.py:222-237](app/netguard.py#L222-L237)）
→ **公网对端必被拒**，而且是在鉴权**之前**就被拒。另外 `is_lan_host` 只认"本机自己的名字"
（自己的 IP + 主机名 + `serverLanHost`），**DDNS 域名不在其中**（除非把 `serverLanHost` 设成那个域名）。
`docs/手机触点-App设计.md` 与 `docs/DEPLOY.md:202-210` 也已经定过同一个口径，§9 写明"不做公网暴露"。

**② 一个反直觉的实测：Tailscale 会被自己的守卫挡掉。**
`100.64.0.0/10` 是 Tailscale 的默认网段，而 Python 的 `ipaddress` 对它 **`is_private = False`**
（实测：`100.101.102.103 → is_private=False → is_public_peer=True`）。
所以走 Tailscale 直连 ECHO 会得到 403，**看起来像"隧道没通"，其实是守卫判它是公网**。
→ 隧道要选**私有网段**的：**WireGuard 用 `10.x`**（`is_private=True` ✅）。

**③ 走"带认证的反代"时，有个坑必须知道：反代会变成"本机调用"。**
`optional_auth`（[api.py:108-123](app/api.py#L108-L123)）的顺序是：
`apiAuthEnabled=false` → 全程放行；否则算 `_is_loopback_call()`，
**本机且不带令牌 → 直接 `return None`**，而且**只有"非本机"才去查那 6 条拒收清单**（`api.py:119-122`）。
反代如果部署在**同一台机器**上（`DEPLOY.md` 建议的"保持 loopback 绑定 + 前置反代"正是这样），
它转发过来的请求 Host 与对端都可能被判成回环 → 于是：

- **免令牌**：App 那把配对令牌在这条路上是**装饰性的**；
- **跳过那 6 条拒收**：`POST /api/control/echo/stop`、`/api/system/restart`、
  `DELETE /api/meetings/{id}`、`/api/meetings/clean-short`、`PUT /api/settings`、
  `/api/settings/reset` —— **在反代路径下全部不再受限**。

这不一定是漏洞（"本机进程就是这台机器的用户"是既定取舍），但它意味着
**安全边界完全押在反代那一层**。所以：

- 反代必须做 **TLS + 认证（Basic Auth / mTLS）**，并且**只放行手机真正需要的路径**，
  不要 `location / { proxy_pass ... }` 一把梭 —— 那样等于把 `PUT /api/settings` 也交出去；
- **要么让反代保留真实 Host**（`proxy_set_header Host $host;` / Caddy 默认行为），
  这样 ECHO 仍按"网上来的调用"处理（要令牌 + 查拒收清单）；
  要么明知"等价于本机"并据此收紧反代。**这两种配置的安全含义完全不同，必须自己确认走的是哪种** ——
  判据：从外网 curl 一下 `PUT /api/settings`，看它是 **403 forbidden: 不允许从手机/手表改设置**（保留 Host）
  还是**直接生效**（变成回环豁免）。

**④ 真要直接暴露，先看这份代价清单**（都是代码里读到的）：

| 项 | 现状 | 公网暴露的后果 |
| --- | --- | --- |
| 传输加密 | 后端纯 HTTP，App 还开着 `usesCleartextTraffic` | **Bearer 令牌明文过网**，链路上谁都能拿走 |
| 令牌权限 | `verify_api_key` **不看 `scopes`**（[db.py:1095-1105](app/db.py#L1095-L1105)） | 手机令牌 ≡ 面板令牌：能发指令、**开/停录音**、读全部会议转写与指令历史、清空历史、下模型、停能力后端；只有那 6 条被挡 |
| 令牌寿命 | `api_keys` 只有 `enabled`/`last_used_at`，**没有 TTL** | 泄露即长期有效，只能 `DELETE /api/keys/{id}` 撤销 |
| 无令牌面 | `/`、`/web/*`（整个面板 UI）、`/manifest.json`、`/sw.js`、`/health` 都不带鉴权 | 公网一探就知道这儿有台 ECHO，还能拿到面板页面 |
| 配对入口 | `/api/pair/phone/claim` 天生不带鉴权（唯一一条） | 6 位码 + 5 分钟 + 用掉即删 + 错 20 次锁 |
| 配对锁 | **全局锁**，不是按来源各算一份（[phone_pair.py:24](app/phone_pair.py#L24) 明写） | 公网上谁都能故意错兑 20 次，**把你也锁在门外**（5 分钟）—— 现成的 DoS |

**⑤ 我建议的取舍**：自家场景用**隧道**，别端口映射。

- **WireGuard（`10.x` 网段）**：家里路由器或 PC 上跑一个 peer，手机当另一个 peer。
  - 没有公开端口，没密钥连握手都完不成；传输本身加密（不必再给 ECHO 加 TLS）；
  - **零改代码**就能过现有守卫：对端是 `10.x`（`is_private=True`）✅；
    再把 `serverLanHost` 设成 PC 的 WireGuard 地址 → `is_lan_host` 通过 ✅，
    而且 `_phone_base_url()` 会把 `10.x` 公布给手机 ✅（App 里就填 `http://10.x:8970`）；
  - DDNS 仍然有用：**WireGuard 的 endpoint** 填你的 DDNS 域名，但 ECHO 的 API 只在隧道内可达。
- 若哪天真要对外开一个 HTTP 入口：按 `DEPLOY.md:208-210` 走**反代 + Basic Auth + TLS**，
  并且按上面 ③ 把 Host 与路径都收紧；那时我再把"允许公网"做成一个**显式开关**
  （而不是悄悄把 `is_public_peer` 放宽），并配一份威胁清单。

> 今晚**没有**改任何守卫或鉴权代码 —— 上面全是"读代码 + 实测判断"的结论，
> 等你决定走哪条路再动，动的时候要连带改 `docs/` 里的口径与用例。

---

### 故意**没有**的按钮（服务端会 403）

`app/api.py` 的 `_NETWORK_DENIED` 只有 **6 条**，网上来的调用一律 403：

| 方法 | 路径 | 原因 |
| --- | --- | --- |
| POST | `/api/control/echo/stop` | 不允许从手机/手表停掉 ECHO |
| POST | `/api/system/restart` | 不允许从手机/手表重启 ECHO |
| DELETE | `/api/meetings/{id}` | 不允许从手机/手表删除会议 |
| POST | `/api/meetings/clean-short` | 不允许从手机/手表批量删会议 |
| PUT | `/api/settings` | 不允许从手机/手表改设置 |
| POST | `/api/settings/reset` | 不允许从手机/手表重置设置 |

所以 App 里**没有**停服务/重启/删会议/改设置的入口 —— 提供了也只会得到一个必然失败的按钮。
（反过来要知道：`verify_api_key` **不看 `scopes`**，手机令牌与面板令牌权限相同，
所以"手机能力更小"这件事**只由客户端给不给按钮决定**，服务端就这 6 条。）

### 探活用 `/health`，不是 `/api/status`

`GET /health` 是唯一**不带 `/api` 前缀、也不要令牌**的一条（main.py:179）。
拿 `/api/status` 探活的话，令牌坏掉会得到 **401**，很容易被误判成"服务没起来"。
现在：探活失败 → "连不上…"；探活成功但 status 401 → **"能连上，但令牌无效或缺失，请重新配对"**。

### 列表与详情的字段名**故意不一致**（最容易静默写错的地方）

- 列表：`has_summary`（snake）、`speakerNames`（**字符串数组**）、`segments`（**int 段数**）
- 详情：`hasSummary`（camel）、`speakers`（**对象数组**）、`segments`（**array**，原段数搬去了 `segment_count`）

混用不会报错，只会永远拿到默认值 —— 所以 DTO 分成两份、由 `MeetingDtosContractTest` 钉住。
另外 `/api/meetings` **没有 `total`**（只有 `/api/commands` 有），分页逻辑不能一套通用。

---

## 2. 工具链（本机固定版本，已装在仓库外的 `.tools/`）

| 组件 | 版本 | 本机路径 |
| --- | --- | --- |
| JDK | 17.0.20.1（Microsoft OpenJDK） | `C:\echo-dev\.tools\jdk\jdk-17.0.20.1+1` |
| Android SDK | platform `android-34` + build-tools `34.0.0` | `C:\echo-dev\.tools\sdk` |
| Gradle | 8.7 | `C:\echo-dev\.tools\gradle\gradle-8.7` |
| AGP / Kotlin | 8.5.2 / 1.9.24 | 由 Gradle 解析 |

SDK 位置写在 `local.properties`（`sdk.dir`），**不进版本库**；换机器改这一行即可。

---

## 3. 怎么构建

```powershell
$env:JAVA_HOME = 'C:\echo-dev\.tools\jdk\jdk-17.0.20.1+1'

# 用 wrapper（首次会从腾讯云镜像下 gradle-8.7-bin.zip，约 134 MB）
.\gradlew.bat :app:assembleDebug

# 或直接用本机已装的那份 Gradle（不下载发行包）
& C:\echo-dev\.tools\gradle\gradle-8.7\bin\gradle.bat :app:assembleDebug
```

产物：`app\build\outputs\apk\debug\app-debug.apk`

**交付层里也会带一份 APK**（用户 2026-10-15："apk 也放到交付包里"）：`scripts\build_delivery.py`
自动把上面这个产物（或 `dist\ECHO-android-*.apk`，有的话优先用它）拷进交付资料夹，命名
`ECHO-android-app-debug.apk`，并写进 `清单.txt` —— 同事拿到资料夹就有主机和手机两半。
找不到 APK 时它**只警告不拦**（真要不带用 `--no-apk`，要指定别的包用 `--apk PATH`）。
所以**出交付层之前先跑一次上面的 `assembleDebug`**；判据在
`tests/test_delivery_standard.py::ApkInDeliveryTests`。

跑单测（纯 JVM，不需要设备）：

```powershell
.\gradlew.bat :app:testDebugUnitTest
```

### 为什么 wrapper 指向腾讯云镜像

`services.gradle.org` 在**本机网络下不可达**（实测 `无法连接到远程服务器`），
所以 `gradle/wrapper/gradle-wrapper.properties` 里的 `distributionUrl` 指向
`https://mirrors.cloud.tencent.com/gradle/gradle-8.7-bin.zip`（同一份官方 zip，实测 200、134184980 字节）。
要换回官方源就把这一行改成 `https\://services.gradle.org/distributions/gradle-8.7-bin.zip`。

> 注：`gradle-wrapper.jar` 是从本机 Gradle 8.7 发行包的 `lib/plugins/gradle-wrapper-8.7.jar`
> 里取出的官方 `gradle-wrapper.jar`（43453 字节）——因为 `gradle wrapper` 任务要先联网校验 URL，
> 而那个域名在这台机器上不通。

---

## 4. 配对流程

1. 电脑端 ECHO 面板/网页生成 **6 位一次性配对码**（5 分钟寿命、用掉即删）。
2. App 里填**后端地址** + 配对码 + 设备名，点「开始配对」。
3. App 调 **不带令牌**的 `POST /api/pair/phone/claim`，body `{code, name}`。
4. 成功回 `{ok, token, keyId, name, baseUrl, note}`；**令牌只出现这一次**，
   App 立刻写进 `EncryptedSharedPreferences`（Android Keystore，AES256）。
5. 之后所有请求带 `Authorization: Bearer <token>`。

失败时后端返回 **403 + `{"detail": "..."}`**（例如「配对码无效或已过期」），App 直接显示 `detail`。

> 后端**故意不做回环限制**（`api.py:2531` 注释写明：那会让手机根本连不上），
> 守卫是「6 位 + 5 分钟 + 用掉即删 + 连错 20 次锁 5 分钟」。

### 手机上「后端地址」填什么

填的是 **ECHO 面板/API 自己那个服务根**（`serverPort`，默认 **8970**）——**不要**填 8900/8901，
那是「能力后端」（`server/`，转写/分离）的端口，手机连它没有配对口。

- **真机 + Wi-Fi**：填电脑的**局域网 IP**，例如 `http://192.168.1.5:8970`
  （`127.0.0.1` 在手机上指手机自己，连不到电脑）。
- **USB 调试**：可以先 `adb reverse tcp:8970 tcp:8970`，这时填 `http://127.0.0.1:8970` 才成立。
- 端口以**实际在听的那个**为准：后端用 `ports.active_port()` 算出真实端口再写进配对回包的
  `baseUrl`（app/api.py:2462 注释说明：首选端口被占时会让过位，写配置值会「配对成功但一直连不上」）。
  所以**优先用配对回包里的 `baseUrl`**，别照抄配置里的首选值。
- 后端只绑回环时，必须走上面任一条；`usesCleartextTraffic="true"` 已开，明文 HTTP 可用。

### `/api` 前缀这件事（踩过）

后端是 `router = APIRouter(prefix="/api")`（app/api.py:28），而配对回包的 `baseUrl`
给的是**服务根**（`http://host:8970`，**不带 `/api`**）。所以：

- `EchoApi` 里每条路径**自己带 `api/` 前缀**（`api/pair/phone/claim`、`api/commands`、…）；
- `ApiClient.normalizeBaseUrl()` 容忍用户粘进来带 `/api` 的地址，
  避免拼成 `/api/api/...` 变成 404。

这一条由 `EchoApiWireTest` 钉着（真发一次本地 HTTP 请求，逐条断言路径）。

---

## 5. 接口契约（都按 `app/api.py` 实读，不是猜的）

| 用途 | 端点 | 关键字段 |
| --- | --- | --- |
| 配对换令牌 | `POST /api/pair/phone/claim` | 入 `{code, name}`；出 `{ok, token, keyId, name, baseUrl, note}`；失败 403 `{detail}` |
| 发文字指令 | `POST /api/assistant/command` | 入 `{text, source, workspace, session_id}`；出 `{ok, message}` —— **没有 commandId** |
| 指令历史 | `GET /api/commands?limit=&offset=&q=&since=` | 出 `{total, items}`；`limit` 上限 500，`total` 是**过滤后**总数 |
| 命令的会话 | `GET /api/commands/{id}/session?limit=12` | 出 `{ok, session_id, messages[{role,seq,text}], title, cwd, exists, running, error, anchored}` |
| TTS | `POST /api/tts` | 裸音频字节（`audio/mpeg` 或 `audio/wav`）；空文本 422、tts 关 409、合成失败 502 |

`items` 的每一行就是 `commands` 表的一行（`app/db.py:112`）外加 `_command_row` 摊平出来的字段：

```
id, ts, source, text, status, session_id, reply, brief, duration_ms, error, meta
+ backend, intent          # app/api.py:1071 摊平
```

- `status` 生命周期：`pending → sent → running → done / failed`
- 界面取的是 **`ts` / `status` / `reply`**（不是 `created_at` / `state` / `result`）
  —— 这组名字写错过一次，`DtosContractTest` 现在钉着。

**为什么文字指令不能轮询单条结果**：`/assistant/command` 只回 `{ok, message}`，没有 `commandId`；
只有语音那条路 `/assistant/voice-command` 才回 `commandId`（阶段4 用得上）。
所以现在发完是直接刷新 `GET /api/commands` 列表来观察落库与执行状态。

---

## 6. 验证到什么程度

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 干净重编 + 打 APK | `clean :app:assembleDebug` | ✅ BUILD SUCCESSFUL，产出 `app-debug.apk`（约 17.9 MB） |
| APK 元数据 | `aapt2 dump badging` | ✅ `com.echo.contact`、label `ECHO 触点`、minSdk 26 / targetSdk 34、launcher `com.echo.contact.MainActivity` |
| **WAV 头字节** | `:app:testDebugUnitTest` | ✅ `WavTest` 8/8（与 Python `wave` 模块**逐字节**一致） |
| 后端 JSON ↔ DTO | `:app:testDebugUnitTest` | ✅ `DtosContractTest` 6/6 |
| 会议/转写 DTO 契约 | `:app:testDebugUnitTest` | ✅ `MeetingDtosContractTest` 7/7（含"列表 vs 详情故意不一致"与"同名不同类型"） |
| 回顾 DTO 契约 | `:app:testDebugUnitTest` | ✅ `ReviewDtosContractTest` 3/3 |
| 路径 / 请求体 / 鉴权头 | `:app:testDebugUnitTest` | ✅ `EchoApiWireTest` 16/16（含"语音是裸 body 不是 multipart""会议 POST 不带 body""探活走 /health""翻页带 offset"） |
| 认领规则（"哪行是我刚发的"） | `:app:testDebugUnitTest` | ✅ `SubmitMatchTest` 6/6 |
| 历史分页合并规则 | `:app:testDebugUnitTest` | ✅ `CommandPagingTest` 9/9（翻页重叠去重 / 刷新头部不回退） |
| 识别兜底规则 | `:app:testDebugUnitTest` | ✅ `RecognizeOutcomeTest` 5/5 |
| 客户端缓存/超时语义 | `:app:testDebugUnitTest` | ✅ `ApiClientCacheTest` 5/5（缓存键必须带超时） |
| **真后端联调** | `-Decho.realBackend=…`（见下） | ✅ `EchoApiRealBackendTest` 6/6（真 api.py + 真配对码 + 真后台线程 + 真 SQLite） |

- **默认**（不带参数）：**65 个用例执行、6 个跳过、0 失败**（共 71 个）—— 常规构建自足。
- **带真后端插座**：**71 个用例执行、0 跳过、0 失败**。

### 真机接不上的那一环，我用"离线可判"的方式补上了

语音链路里**唯一没法用单测覆盖**的是"我手写的 wav 头，ECHO 认不认"。这一环
**不需要真机也能验**，而且已经验完：

1. `WavTest`：把 `WavWriter.wrap()` 的输出与 **Python 标准库 `wave` 模块**写出的
   前 44 字节**逐字节比对**（三组参数：16 k 单声道 / 16 k 一秒 / 44.1 k 立体声）。
   这是跟一个**独立实现**比，而不是"自己跟自己一致"。
2. **端到端**：用真 ECHO 的 `POST /api/tts` 合成一句话 → `ffmpeg` 转成
   **16 kHz 单声道**（正是手机录音的格式）→ **剥掉原头、用我这套布局重新包一遍**
   （音频一字节没动）→ 交给真 ECHO 的 `POST /api/stt/transcribe`：
   - ffmpeg 造的头 → 836 ms → 「提醒我下午3点给张工打个电话。」
   - **我的头** → 350 ms → 「提醒我下午3点给张工打个电话。」
   → **同样的文本**，说明我那个 44 字节头被 ECHO 的解码器照常接受。
   （顺带查明：ffmpeg 的头不是 44 字节 —— 它在 `fmt` 与 `data` 之间插了一个 26 字节的
   `LIST` 元数据 chunk，所以 `我的头 == ffmpeg 的头` 是 False，但两者都能用。）

### 真机反馈回来的两个问题（都已修 + 都有用例钉住）

**① 点完发送没有反馈，刷新后才出现结果。**

根因（读代码定位，不是猜）：`POST /api/assistant/command` **立刻**返回
`{ok: true, message: "已提交"}`，真正的活在**后台线程**里干（[assistant.py:792](app/assistant.py#L792)）；
那一行 `commands.status` 之后才走 `pending → sent → running → done/failed`，
`reply` 更是**后来**才写进去（后端最长等 `REPLY_TIMEOUT_S = 300` 秒，[assistant.py:91](app/assistant.py#L91)）。
所以原来"发完只刷一次"必然刷到空结果 —— 这不是网络慢，是**时序错了**。

修法：发出去立刻显示「正在提交 → 已受理 → 执行中」，然后**每 1.5 秒轮询那一行直到有结论**
（上限 330 秒，比后端自己的 300 秒宽），列表同步实时刷新。
文字这条路**不回 `commandId`**，所以靠 `id > 发送前最大 id` + `source == "mobile"` + 文本相同 来认领
（抽成 `pickSubmitted`，由 `SubmitMatchTest` 钉住）。

**② 界面改成仪表盘风格与内容。**

配色逐值抄 `web/app.css` 的 `:root`（`#0f1117 / #171a23 / #2a2f3e / #4f8cff / #3ecf8e /
#e6b84c / #ef5b5b / #a78bfa`），状态灯规则也照面板 `.dot/.badge`：
`online|active`绿、`offline|error`红、`idle|disabled|paused`黄、`transcribing|sent|running`紫、`unknown`灰；
**红只留给故障**（录音中是"进行中"，用紫）。面板只有深色，所以手机端也**固定深色**、不跟随系统。

内容：顶部三格运行状态条（服务 / 智能体 / 语音合成）→ 整机卡（版本 + 运行时长 + `busy` +
**9 个组件**逐个带灯）→ 会议卡（录音中 / 识别设备）→ 发送框 → 本次指令卡 → 指令历史。
字段全部来自 `GET /api/status` 实测结构（`components[] / agent / stt / meeting / busy / uptime / version`）。

### ③ 这一版还加了「朗读试听」（安卓系统 TTS vs ECHO 合成）

就一个小框 + 两个按钮，用来听效果、不做设置项：

- **手机念** → `android.speech.tts.TextToSpeech`（本机合成，引擎是厂商预装的，**可能离线**）
- **ECHO 念** → `POST /api/tts` 拿音频字节再播（音色与电脑一致，**必须有网**）
- 每条回复旁边还有一个 🔊，用的是**手机**那条路（这正是要评估的对象）

判断"能不能拿来给语音和回顾用"的关键是那行小字里的**是否需要联网**：
面板口径一致地认为"可离线"才够格当降级档。系统引擎名/音色/联网要求都来自实际查询
（`voice.isNetworkConnectionRequired`），不是猜的。

> 踩坑记录（两个 API 其实不存在，都是我凭记忆写错的）：
> `TextToSpeech.getDefaultEngine()` 和 `EngineInfo.priority` **都不是公开 API**。
> 真正的默认引擎要从系统安全设置 `tts_default_synth` 读；读不到就**如实显示"读不到引擎名"**，
> 不编一个"优先级最高的"糊上去。

> 另一个坑（已在清单里处理）：Android 11+ 的**包可见性**。不声明
> `<queries><intent><action android:name="android.intent.action.TTS_SERVICE"/></intent></queries>`，
> `TextToSpeech` 会**看不到**系统里的引擎，表现是"这台设备没有朗读引擎"——而引擎其实装着。
> 已用 `aapt2 dump xmltree` 确认这条真的进了 APK 的清单。

### ④ 手机识别测试（`SpeechRecognizer`，2026-10-09 加）

也是为了"先看效果"：按一下 **🎤 说一句**，用**系统**识别引擎（输入法那个麦克风背后的同一个东西）
把话转成字，边说边出临时结果，定稿后**自动填进指令框**，可以直接发给 ECHO。

两个必须处理的坑（不处理就会**误报"这台设备没有识别服务"**）：

1. **包可见性**：清单里要有
   `<queries><intent><action android:name="android.speech.RecognitionService"/></intent></queries>`
   （和 TTS 那条并列）。已用 `aapt2 dump xmltree` 确认两段都进了 APK 的清单。

   > ⚠️ **而且这是三条、不是两条**（2026-10-09 真机第二次踩到）：
   > 识别**服务**（`android.speech.RecognitionService`）和识别**界面**
   > （`android.speech.action.RECOGNIZE_SPEECH`）是**两个不同的 action**，要分别声明。
   > 只声明服务那一条时，`isRecognitionAvailable()` 照常返回 **true**（所以"服务可用"），
   > 但拉起系统识别界面会抛 `ActivityNotFoundException` —— 提示会长成
   > "这台设备没有可打开的系统识别界面"，**读起来像"手机没装"，其实是我漏声明**。
   > 界面上那行现在会分别报"服务可用（包名）｜识别界面 N 个（包名）"，
   > 就是为了让这种误判一眼看得出来。
2. **`RECORD_AUDIO` 是运行时权限**：第一次点会弹权限框，不许可就一定失败。这是本 App 第一处运行时权限。

界面上那行小字会显示 **识别服务：可用/不可用 · 引擎包名** —— "是谁在识别"这件事
直接影响识别效果与一致性，所以不要靠猜。

**它验不到什么**（如实说明）：`SpeechRecognizer` 只给文本、**不给音频**，所以这条路
送不了音频给 ECHO 的 ASR。而且**"离线"保证不了**：`EXTRA_PREFER_OFFLINE` 只是**建议**，
厂商引擎可以无视；识别这边也没有 TTS 那种"是否离线"的判据可查（`Voice.isNetworkConnectionRequired`
是 TTS 才有的）。所以**"手机识别 = 不出机"不成立**，音频可能被送到厂商云。

#### 真机踩到的两个坑（都已修，2026-10-09）

**① 点「说完了」没反应。** 三个原因：`onState` 被我忽略（引擎在"识别中"时界面什么都不显示）；
`stop()` 里的 `runCatching` 把异常吞了（违反"不许静默失败"）；**最要命的是没有任何收尾保证** ——
`stopListening()` 之后引擎可能**一个回调都不给**，而收尾只写在 `onFinal`/`onError` 里。
现在：显示引擎阶段、异常变人话、给 2.5 秒宽限后**用临时结果兜底定稿**（并如实标注），
外加 15 秒总兜底。规则抽成 `resolveRecognizeOutcome`，由 `RecognizeOutcomeTest` 钉住。

**② 再点就提示"识别服务忙"。** 根因是**我放弃了却没释放会话**：只清 UI 状态，
会话还挂在引擎上，下一轮必然 `ERROR_RECOGNIZER_BUSY`。
现在 `release()`（cancel + destroy + 置空）在放弃/取消/异常三条路上都调，
并且**每一轮 start 都用全新的 recognizer**；一旦收到 BUSY 立刻彻底放掉。

**③ 补了一条「用系统界面说」。** 因为有些国产 ROM 的识别服务**不服务嵌入式 API**
（一个回调都不给），但它自己的识别界面是好的。这条走 `RecognizerIntent` + 系统 UI，
我们只收文本 —— 用来判断"这台设备到底能不能识别"，比在这边猜准得多。
（这条**不需要**我们的录音权限，录音是系统那个界面在做。）

### ⑤ 启动图标 = 面板折叠边条的 logo（2026-10-09）

按 `web/rail.html` 的 `.logo` 逐值抄：`linear-gradient(160deg,#4f8cff,#3a6fe0)`
蓝色渐变方块 + 白色粗体 **EC/HO 2×2** 网格（Segoe UI Bold）。

自适应图标拆两层：背景 = `drawable/ic_launcher_background.xml`（渐变铺满，形状交给启动器裁），
前景 = `mipmap-*/ic_launcher_foreground.png`（白色字母，**只落在中央安全区**，
否则圆形遮罩会切到）。两层都由 `android/tools/make_launcher_icon.py` **可重复生成**，
别手工改 PNG。

> 踩坑：第一版生成器把 dp 和"像素 = dp×密度"搞混了，超采样画布算出来只有 **16×16**，
> 字母全贴在画布外 → 生成了一个**什么都没有**的图标（预览图是纯蓝一片才发现）。
> 现在脚本里加了"不透明像素太少就**响亮失败**"，不再可能静默写出空图标。

### 这两组用例是有牙的（做过反向对照）

把 `EchoApi` 里 `api/commands` 临时改回 `commands`（模拟"漏了 `/api` 前缀"这个真实错误）
→ `:app:testDebugUnitTest` 变成 **`tests completed, 1 failed`**
（`历史走 api commands 且 limit 进查询串`，`ComparisonFailure`），改回后恢复全绿。
所以上面的绿不是「空跑出来的绿」。

轮询那条更是**刻意做成对抗性的**：真后端插座里预置了一条**文本完全相同**的 `done` 旧记录，
新测试故意发**同一句话** —— 若认领规则漏了 `id > 发送前最大 id`，就会立刻"找到"旧记录、
报出一个假的成功结果（这是最误导的失败方式）。实测：**轮询 2 轮**才拿到结论，
认领到的是新行（`failed` / `DSH 未运行`）而不是旧的 `done` 行。

### 这两组用例为什么值得存在

这两个错法**都不会编译报错**，只会在运行时表现成「后端好像坏了」：

- 字段名写错（`created_at`/`state`/`result` ↔ 真实的 `ts`/`status`/`reply`）：
  Gson **静默丢弃**不认识的字段 → 界面上那一行永远是空白，不报任何错；
- 漏 `/api` 前缀 → 每次请求 404。

### 真后端联调（真 `app/api.py`，不是打桩）

上面的 MockWebServer 只能证明「App 发出去的形状对」，证明不了
「**真后端**收下这个形状、并吐回我们的 DTO 认得的 JSON」。后者用一个插座补上了：

```
android/tools/real_backend_harness.py
```

它把真 `app.api.router` 用 uvicorn 起在回环的临时端口（默认 18970），并且：

- 用**临时目录**当数据根（`ECHO_DATA`）→ **不碰真实 `data/`**；
- `db.init()` 建库后预置一条真实指令（`db.add_command` + `db.update_command`，status=done）；
- 用 `phone_pair.issue()` 生成一张**真配对码**，把 `{port, code, expected*}` 写成 `harness.json`；
- **把智能体打桩**：`assistant.get_client()` 换成一个 `ping()` 恒为 `False` 的替身，
  并哑掉 `tts_mod.play_beep` / `notify`。

> ⚠️ **最后那一条是踩出来的，不是一开始就有的。** 第一版没有打桩，于是
> `POST /api/assistant/command` 走真 `_dispatch` 时**连上了这台机器上真在跑的桌面 DSH**，
> 把我那句测试指令「把客厅灯打开」真的当成一轮任务跑了（14 步 / 21 次工具调用，
> 在 `~/.dsh/sessions/--C-echo-dev-data-command--/session-65f0e505-…` 里留了一个会话）。
> 它全程只读（`Get-Date`/`Get-NetTCPConnection`/`curl /api/status`/`grep`），
> 自己建的两个 `_probe_*` 临时文件也自己删了，`C:\echo-dev\data\command` 现在是空的；
> 但**那本来就不该发生**：插座的语义是"验 App"，不是"往别人正在用的助理里插一句话"。
> 打桩之后重跑，那一行稳定落到 `failed` / `DSH 未运行`、**`session_id` 为空**，
> 也就是真实的异步链路照样全跑，但一个包都出不了这个进程。

跑法（插座先跑起来）：

```powershell
# 终端 A：起插座（它是常驻的，验证完就停掉）
C:\echo-dev\venv\Scripts\python.exe android\tools\real_backend_harness.py `
    --json C:\echo-dev\android\build\harness.json --port 18970

# 终端 B：打开「真后端」用例
.\gradlew.bat :app:testDebugUnitTest "-Decho.realBackend=C:\echo-dev\android\build\harness.json"
```

`EchoApiRealBackendTest` **默认是跳过的**（`assumeTrue`），所以常规 `gradlew test`
完全自足、不依赖任何外部进程 —— 实测不带参数时它是 `skipped=2`。

**它验到了什么**（实测一次，0 失败）：

| 环节 | 证据 |
| --- | --- |
| 真 HTTP 往返 | `pairClaim` 打到真 `POST /api/pair/phone/claim`，拿回真令牌（非空） |
| 真令牌真的是钥匙 | 插座那份临时库里 `api_keys` 恰好 **1 行**，`name=mobile`、`scopes=["device"]`，`created_at` 正是用例跑的那一秒 |
| 真 JSON 能被 DTO 认 | `GET /api/commands` 的真响应里按 id 找到预置那条，`ts`/`status=done`/`reply`/`text`/`source`/`duration_ms=1234` 全部断言通过 —— 其中 `ts`、`status`、`reply` 正是原先写错就永远为空的那三个 |
| UTF-8 | `assertEquals("已经打开了客厅的灯。", reply)` 通过（中文一字不差） |

**它没验到什么**（如实说明）：

- **令牌鉴权没被真正行使**：`optional_auth` 对**回环调用豁免**（[api.py:97](app/api.py#L97) 写明，
  2026-10-09 有意为之，否则面板自己就瞎了），而联调走的是 `127.0.0.1`，所以带不带 `Bearer` 都放行。
  「网上来的调用必须带令牌」这条**没有**被这轮验证覆盖。
- **没有跑智能体那一段**：`POST /api/assistant/command` 会去 `assistant.send_text()`，
  可能触发拉起 harness（DSH）——那是分钟级且会动用户环境的动作，所以**故意没打**这个端点。
  它已由 `EchoApiWireTest` 钉住「路径/请求体/Bearer 头」，但「真后端真把指令交给智能体」未验。

### 没做的验证（如实说明）

- **没有装到真机/模拟器上跑过**（本机没有设备、也没有装模拟器镜像），
  所以「点开 App、走完配对与发指令」在**手机运行时**上尚未验证。
  具体没被证明的还有：`EncryptedSharedPreferences` 在真机 Keystore 上的行为、
  以及 Compose 界面本身的显示与交互。
- 本机 ECHO **从未被启动过**（`8970/8900/8901` 全程无监听），没有抢用户正在用的后端。
  （用户后来自己起了开发版，我全程只用**只读**接口，外加 `POST /api/tts`、
  `POST /api/stt/transcribe` 这两个**不改状态**的合成/转写调用 ——
  没有调用过 start/stop 会议、没有提交过真回顾、没有改设置、没有删除任何东西。
  期间为了确认"我有没有污染真实库"，我**只读**查过 `data/echo.db` 的 `commands` 表。）

---

## 7. 已经做完 vs 还没做

**已完成（阶段 3 + 阶段 4 的语音链路 + 阶段 5 常驻听写）**：配对、四分区界面（仪表盘/指令/会议/回顾）、
文字指令 + 实时轮询、**语音指令**（录 wav → ECHO 转写 → 下发 → 手机念）、
历史与筛选、看会话、会议（状态/开始停止/转写进度/列表/**正文与纪要**）、
回顾（**手机口述**、念播报、按天历史）、朗读试听、启动图标、
**后台听写**（前台服务里一直听：锁屏/切走都能用，一句说完自动发出去并把回复念出来）。

**还没做**（都不影响明天测）：

- **免提自激**：听写期间"正在念回复"那几秒是**不收音**的（见 §8 的取舍），
  要做到"边播边听"需要 AEC（回声消除）—— 现在没做，所以念的时候你插话会丢。
- **会议音频播放**没做：`/meetings/{id}/audio?seg=N` 能取（还支持 Range），
  但手机上逐段听一小时会议不是这里该干的事；目前只给了正文与纪要。
- **指令历史分页**：现在是**真正的 offset 翻页**（"加载更早的"）。
  之前那版是错的 —— 写着"再拉 100 条"，其实每次 `offset=0`，
  只是把最新的前 100 条重拉一遍，**永远看不到第 101 条以前**。
  合并规则由 `CommandPagingTest` 钉住：翻页重叠要按 id 去重（否则 `LazyColumn` 的 key 撞了会崩），
  而轮询刷新头部时**不能把用户翻出来的更早那些行打回 30 条**。
- **手表**：本仓库那份可行性分析里的路径 A/B/C 都还只是分析，没动。
- **出网/公网访问**：见上面那节分析，等用户定方向。

---

## 8. 阶段5：常驻听写（2026-10-15）

**它解决什么**：界面里那次"按住说一句"是**前台临时录音**，界面一走就没了。
而真实场景是**开车**：手机在支架上/兜里、接着蓝牙耳机，人要的是"抬手说一句就行"。

**怎么工作**（`service/ListenService.kt`）：

```
前台服务（麦克风型） → AudioRecord(16k 单声道) → VoiceSegmenter（静音分段）
   → POST /api/assistant/voice-command（裸 wav）拿 commandId
   → 轮询 GET /api/commands?limit=5（1.5 秒一次，最多 90 秒）
   → 手机系统 TTS 念 brief/reply
```

- **分段判据**在 `audio/VoiceSegmenter.kt`：**纯 Kotlin、不碰 Android API**，
  于是能用 JVM 单测逐帧钉住（`VoiceSegmenterTest`，8 条）：
  门限用 RMS（默认 0.012，与面板同口径）、静音 1200 ms 收尾、太短（<300 ms）丢弃、
  说太久（>15 s）强制切、**前导静音只留 400 ms 预卷**（否则干等的十几秒会原样进 wav）。
- **状态与文案**在 `service/ListenState.kt`（同样是纯逻辑，`ListenStateTest` 6 条）：
  档位是 OFF / STARTING / LISTENING / THINKING / SPEAKING / FAILED，
  通知与界面上那两行字由 `textsOf(phase, detail, segments)` 统一给 ——
  **锁屏时那是用户判断"它到底在干什么"的唯一依据**，所以逐档钉住、标题不许撞车。
- **界面入口**：指令页第二张卡「后台听写」开关；打开时按需要 RECORD_AUDIO 与
  POST_NOTIFICATIONS（通知权限**给不给都开**，不给只是看不到常驻通知）。
- 通知里有「停止听写」，`exported=false`，`foregroundServiceType="microphone"`。
- **省电自动停**（`service/IdleAutoStop.kt`，纯逻辑 + 5 条单测）：一直没人说话 **20 分钟**
  就自己停，原因写进卡片（"听了 20 分钟也没听到你说话，先停了省电"）。
  **只有"真的送出一段"才算有动静** —— 否则开车放一路音乐就永远停不下来。

**三个刻意的取舍**（写清楚，免得后来者以为是漏了）：

1. **处理 / 朗读期间不收音**（服务里的 `paused`）：否则手机把自己念的回复又听一遍，
   会自激成"自问自答"。代价是念回复的那几秒你说的话会丢 —— 换来的是不会自激。
   要做到"边播边听"需要 AEC，见 §7「还没做」。
2. **不自动重启开麦**：`START_STICKY` 只重建服务壳；麦克风这种东西，
   **用户没点头就不该自己开**。被系统回收了就由用户在通知/界面里再点一次。
3. **连不上就如实说**：网络错误 / 后端 4xx / 转写为空，都写进通知正文与状态
   （"说完没反应"是最难查的体验），且**不因此停掉整个服务** —— 继续听下一句。

**没验到的**（与前面几节同一个口径，如实写）：本轮加了 19 条 JVM 单测（分段 8 + 状态文案 6 +
省电自动停 5）与 `assembleDebug` 编译通过，但**仍然没有真机**：
前台服务在真机上的麦克风占用、锁屏后的存活、Android 14 对麦克风型前台服务的
合规判定（必须在前台且已授权才允许开），这三条只有装机才能验。
