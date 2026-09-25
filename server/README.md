# ECHO 能力后端（`server/`）

无状态的**能力面**：收音频 → 出文本 / 说话人时间轴 / 向量。**不存任何业务数据。**
设计与取舍全在 `docs/ECHO能力后端-服务端设计.md`；这里只讲**怎么跑、怎么测**。

---

## 一、最快跑起来（本机开发）

```bash
# 1) 装上服务端自己的依赖（与客户端那份刻意分开）
pip install -r server/requirements.txt

# 2) 跑
python -m server.main --config data/backend-dev/server.yaml
#    → 监听 127.0.0.1:8900
```

`data/backend-dev/server.yaml` 是**本机开发配置**（在 `data/` 下，已被 gitignore）。
它与出厂清单有两处**故意不同**，都是为了"不下载新模型"就能自测：

| | 出厂清单 | 本机开发配置 | 为什么 |
|---|---|---|---|
| ASR 实现 | `qwen3asr`（走 modelscope 缓存，首次要下 1 GB+） | `sensevoice`（`models/sensevoice` 已有） | 自测不该卡在下载上 |
| ASR 模型数 | 一个（`slot: asr.long` + `supports: [asr.text]`） | 同样一个 | 让 `variant=short` 与 `long` 都走通 |

> **诚实提示**：SenseVoice 不给句级时间戳，所以 `?timestamps=1` 会如实回
> `"timestamps": "none"`。这是**对的** —— 服务端不编 `estimated`（设计 §6.5 A）。

不想用这份配置就自己写一个，或干脆不给 `--config`（用内置默认值 + 环境变量）。

## 二、一条命令自测（推荐）

```bash
python scripts/smoke-echo-backend.py                       # 连本机 8900
python scripts/smoke-echo-backend.py --base-url http://gpu-01:8900
python scripts/smoke-echo-backend.py --skip-inference      # 只跑查询与拒绝路径，秒级
```

它会走完 21 项，每项打印 `PASS/FAIL` 与实测数字（延迟、turn 数、错误码）：

- **查询**：`/v1/health`（含临时目录统计与并发快照）、`/v1/ready`、`/v1/capabilities`
  （槽、`limits`、`vectorSpaceId` 只有一个 —— 铁律 L5）
- **能力**：`/v1/asr`（冷/热两次，看真实延迟）、`timestamps` 只报 `exact|none`、
  `/v1/diarize`、`/v1/speaker/embed`
- **拒绝路径**：空 body→400、错 Content-Type→415、声报超限→413、
  不存在的模型→404、`mode=turns`→400
- **并发闸门**：同一身份两路 → 恰好一路 `409 client_busy`
- **清理**：跑完临时目录归零

它**不修改服务端任何东西**（除配对那些步会建一个客户端），退出码 0/1。

跑之前它会自己找测试音频：优先 `data/backend-dev/sample30s.wav`，
否则从 `data/meetings/` 的真实录音里切 30 秒（比合成正弦波有意义得多）。

## 三、手工一条条看（想理解每一步在干什么）

```bash
B=http://127.0.0.1:8900
W=data/backend-dev/sample30s.wav

curl -s $B/v1/health | python -m json.tool
curl -s $B/v1/capabilities | python -m json.tool
curl -s -X POST "$B/v1/asr?variant=short" -H 'Content-Type: audio/wav' \
     --data-binary @$W | python -m json.tool
curl -s -X POST "$B/v1/diarize" -H 'Content-Type: audio/wav' \
     --data-binary @$W | python -m json.tool
```

> **在 PowerShell 里别用上面这几条**：`--data-binary @file`、空 body、
> 重复的 `Content-Length` 这几样，引号转义能折腾很久，还容易做出**不是你想发**的请求
> （这份文档第一版就是这么写坏的）。PowerShell 下用第二节那个脚本。

## 四、单测与门禁

```bash
python -m unittest tests.test_admin_console           # 管理面：登录 / 隔离 / 写面的七道闸
python -m unittest tests.test_server_contract          # 服务端契约与护栏
python -m unittest tests.test_line_endings             # server/ 必须保持 LF
powershell -File scripts/check-windows.ps1             # 全量门禁
```

`tests/test_server_contract.py` 里几组值得知道的：

| 组 | 在钉什么 |
|---|---|
| `NoBusinessCouplingTests` | 服务端不认识业务：源码不含 `meeting`/`command`/`summary`/`voiceprint`，不 import 业务层，路由表**恰好**等于白名单 |
| `EventLoopNotBlockedTests` | 两个并发请求**真的**并行（不是串行）—— 用 `httpx.ASGITransport` + `asyncio.gather`，判据是墙钟 |
| `ErrorCodeSurvivesLargeBodyTests` | 大 body 下错误码也必须送到 —— **必须走真 socket**，RST 在进程内复现不了 |
| `AdmissionWiringTests` | 闸门真的接在路由上；预检在读 body **之前** |
| `EnginePoolTests` | 单飞 / 引用计数 / LRU / 显存预算 / **不回退 CPU** |
| `Auth*Tests` | 配对码一次性、只存哈希、`alg:none` 被拒、撤销下一个请求就生效 |
| `AuthSchemaTests` | 库里只有白名单内的表、列名不命中"内容"黑名单 |

`tests/test_admin_console.py` 里与写面有关的那几组：

| 组 | 在钉什么 |
|---|---|
| `WriteGuardTests` | 每个写端点（**从 openapi 现读**）：未登录 401、跨站 `Origin` 403、缺 `X-ECHO-Admin` 403、缺 CSRF 403；未登录时**库里一个字节都不动** |
| `ConfirmTests` | 撤销 / 轮换 secret / 作废码缺 `confirm` → 400 且无副作用 |
| `PairingCodeWriteTests` | 明文配对串只出现一次、库里只有哈希、**用掉一次就失效**、作废后兑不了 |
| `ClientWriteTests` | 禁用/启用、撤销后旧令牌**下一个请求就 401**、改 scopes/配额、轮换后新 secret 能用而旧 secret/旧令牌立刻死 |
| `AuditTests` | 每个写动作（**含失败**）一条审计，操作者是**登录的管理员名** |
| `SharedImplementationTests` | 命令行与管理面**同一串配对串、同一套 store 方法**（`server/ops.py`） |
| `ReadOnlyNotLockedTests` | 只读端点只要登录，**不要求**写请求那几个头 |

## 五、试鉴权（配对 → 令牌）

```bash
# 1) 配置文件里把 auth 打开并配好密钥
#    auth: {enabled: true, mode: jwt, jwt_secret: "<openssl rand -hex 32>"}

# 2) 新建客户端 = 发一张带着名字与 scope 的配对码
python -m server.main --config <cfg> --new-client "张三的办公本" --scopes "asr diarize"
#    → echo://pair?host=127.0.0.1:8900&code=7K2M9QX4

# 3) 用码换凭据 → 换令牌 → 带令牌调用
python scripts/smoke-echo-backend.py --pair-code 7K2M9QX4
```

### 管理动作（命令行）

管理面网页（设计 §8.4）**还没做**。运维真正需要的那几个动作在命令行里齐了：

| 命令 | 干什么 |
|---|---|
| `--new-client NAME [--scopes "asr diarize"]` | 新建客户端（发一张带名字与 scope 的配对码） |
| `--new-pairing-code` | 只发码，不预先指定名字与 scope（对端自报） |
| `--list-clients` / `--list-codes` | 看已配对的 / 待用的码（含剩余时间；**码只看得到哈希**） |
| `--show-client ID` | 一个客户端的详情（名字 / scopes / 状态 / 版本号 / 时间） |
| `--revoke ID` | 撤销（`token_version` +1，**立即**失效） |
| `--disable ID` / `--enable ID` | 禁用（回 **403**）/ 启用（原令牌直接能用） |
| `--set-scopes ID --scopes "asr"` | 改权限，**下一个请求就生效** |
| `--rotate-secret ID` | 换 secret（新的只出现这一次）；**旧令牌与旧 secret 立刻全失效** |
| `--rotate-secret ID --grace-hours 24` | 例行轮换不打断客户端：宽限期内**旧 secret 仍可换令牌**，并顺带再发一张配对码。⚠️ **不能用于 secret 泄漏**（旧 secret 照样进得来） |
| `--set-quota ID --daily-audio-minutes 120` | 每日音频分钟数上限（0 = 用全局默认；**不清零已用量**） |
| `--stats [--since-hours 24]` | 调用汇总：谁在用、失败多少、多少分钟音频、p95 耗时（`0` = 全部） |
| `--list-calls N` | 最近 N 条调用**元数据**（这张表里没有音频/文本/嵌入/说话人数） |
| `--new-admin NAME` | 建管理员账号（或重置其口令）；**口令只打印这一次** |
| `--list-admins` / `--disable-admin NAME` / `--enable-admin NAME` / `--delete-admin NAME` | 管理面账号的增删改（禁用后他手上的会话**下一个请求就失效**） |

### 管理面（控制台 + 写端点）

在配置里设 `server.admin_listen`（例如 `127.0.0.1:8901`）就会起一个**独立端口**上的
管理页面：`http://<host>:<port>/admin/`。

**2026-09-25 起它可写了**（用户要"发授权"这类动作能在面板上完成）。读的部分还是那五个页签
（概览 / 模型 / 客户端 / 调用 / 存了什么）；写的部分见下表。

| 方法 + 路径 | 干什么 | 要 `confirm` |
|---|---|---|
| `POST /admin/api/pairing-codes` | **发授权**：发一张配对码；body `{name, scopes, ttlSeconds, createdBy}`；响应里的 `pairingCode.url` 是**一次性明文** | 否 |
| `DELETE /admin/api/pairing-codes/{id}` | 作废一张**未使用**的码（`{id}` = 码的哈希，清单里给的就是它） | **是** |
| `POST /admin/api/clients/{id}/disable` / `enable` | 禁用（403）/ 启用（原令牌直接能用） | 否 |
| `POST /admin/api/clients/{id}/revoke` | 撤销：`token_version + 1`，**立刻**失效 | **是** |
| `POST /admin/api/clients/{id}/scopes` | 改权限，body `{scopes}`（空串 = 不限） | 否 |
| `POST /admin/api/clients/{id}/quota` | 改每日音频分钟数，body `{dailyAudioMinutes}`（0 = 全局默认） | 否 |
| `POST /admin/api/clients/{id}/rotate-secret` | 换 secret，**新明文只回显一次**；可带 `{graceHours}`（默认 0 = 旧的立刻失效） | **是** |

只读补充：`GET /admin/api/pairing-codes`（待用码 + 剩余时间）、
`GET /admin/api/clients/{id}`（详情，**没有哈希**）、`GET /admin/api/admins`
（管理员账号清单，**只读** —— 改账号仍然只在命令行）。

#### 写面的七道闸（缺一条都算没做完）

1. **仍只监听 `server.admin_listen`**（本机 `127.0.0.1:8901`）。能力面 `0.0.0.0:8900` 没动。
2. **必须管理员会话**：没有有效会话一律 **401**，不会降级成"只读放行"。
3. **CSRF / DNS-rebinding**：写请求必须 (a) `Origin`（或 `Referer`）**是本站**
   （`http://127.0.0.1:8901` / `http://localhost:8901` …），(b) 带 `X-ECHO-Admin` 头，
   (c) 带会话里的 `X-CSRF-Token`。任一条不满足 → **403**，并落一条审计。
   cookie 是 `HttpOnly + SameSite=Strict`（**没有 `Secure`**：管理面是明文 http，
   设了 `Secure` 浏览器根本不会存 —— 那是"登录成功但下一请求 401"的经典坑）。
4. **审计**：每个写动作（**含失败**）一条 `admin_audit`，操作者是**登录的管理员名**；
   失败把原因写进 `target`（表只有四列，加列要走 §8.5 评审）。
5. **危险动作二次确认**：撤销 / 轮换 secret / 作废码 —— 面板先弹确认框，
   后端还要求 body 里带 `confirm`（值 = 目标 id 或 `true`），缺了或不对就是 **400**。
6. **秘密只回显一次**：轮换出的 secret、刚发的配对串只在**那一次响应**里出现；
   之后任何接口都不返回明文（配对码库里本来就只有哈希）。面板把它放在一个
   "只显示这一次"的框里，**刷新即消失**。
7. **复用命令行那条路的实现**（`server/ops.py`）：`main._admin_cli` 与管理面写端点调的是
   同一个 `issue_pairing_code` / `revoke_client` / `rotate_client_secret` / …，
   不在管理面里另写一套。

> **用 `curl` 手测写端点**：必须自己带上那三个头（浏览器会自己带 `Origin`），否则 403：
>
> ```bash
> C=http://127.0.0.1:8901
> # ① 登录：把 cookie 存进 jar.txt，把响应体存进 login.json（里面有 csrf）
> curl -s -c jar.txt -o login.json -X POST $C/admin/api/login \
>      -H 'Content-Type: application/json' \
>      -d '{"username":"ops","password":"<口令>"}'
> CSRF=$(python -c "import json;print(json.load(open('login.json'))['csrf'])")
> # ② 发一张配对码：三个头一个都不能少
> curl -s -b jar.txt -X POST $C/admin/api/pairing-codes \
>      -H 'Content-Type: application/json' -H 'X-ECHO-Admin: 1' \
>      -H "X-CSRF-Token: $CSRF" -H "Origin: $C" \
>      -d '{"name":"张三的办公本","scopes":"asr diarize","ttlSeconds":3600}'
> # ③ 撤销（危险动作，要带 confirm）
> curl -s -b jar.txt -X POST $C/admin/api/clients/cli-1/revoke \
>      -H 'Content-Type: application/json' -H 'X-ECHO-Admin: 1' \
>      -H "X-CSRF-Token: $CSRF" -H "Origin: $C" -d '{"confirm":"cli-1"}'
> ```
>
> **只读端点不需要这几个头**（`GET /admin/api/clients` 之类带上 cookie 就够）——
> 别把只读也一起锁死。

**面板上有什么**（与上表一一对应）：

- 「客户端 / 发授权」页签：**发授权表单**（名字 / scopes / 有效期 / 备注）→ 生成后
  一个黄色框里显示**一次**明文配对串，带「复制」按钮与"只显示这一次"的提示；
- 同一个页签下方是**待用码列表**（剩余时间 / 到期时间 / 作废按钮）；
- 客户端表格每行有 **禁用·启用 / 撤销 / 改 scopes / 改配额 / 轮换 secret**；
  撤销与轮换先弹确认框（后端还要 `confirm`，见第 5 条闸）；
- 轮换出来的新 secret 同样在那个"只显示这一次"的框里 —— **刷新页面它就不见了**
  （后端也不会再给）；
- 「存了什么」页签底部多了**管理员账号清单（只读）**与**最近的管理动作**（含失败）。

几条需要知道的语义：

- **名字 / scope 跟着码走**：`--new-client` 填的名字与 scope 存在配对码上，兑换时直接用。
  **码上带的盖过对端自报的** —— 自报的能改名，那份清单就不算清点了。
- `--rotate-secret` **同时把 `token_version` +1**：v1 没有宽限期，新 secret 只能由管理员
  带外交给用户，客户端本来就要重新配对，所以不给"旧 JWT 还能再用一小时"的窗口。
- `--enable` 之后**不需要重新换令牌**（`disabled` 与 `token_version` 是两回事）。
- `--set-quota` **不清零今天的已用量**（额度按自然日算，改上限不该变成"送你一次重置"），
  而且用量计数在**进程内** —— 多实例各算一份（设计 §7.2）。
- `--stats` 的 **p95 是「这一组里第 95 百分位那条的耗时」**（SQLite 没有百分位函数；
  数据量小、够看），别当成严格分位数。
- `--revoke` 是**另一个进程**改库：服务端最迟 `auth.revoke_poll_s`（默认 5 秒）后生效 ——
  这是有意的取舍（用 5 秒换掉"每请求查库"），文档里没把它写成"立即"。
- 这些动作**不打 HTTP，直接开库**。⚠️ 所以**它的安全边界就是"能读到鉴权库文件"**
  （也就是 shell 权限）。别把库文件放到别人读得到的地方。
- 给 `pairing_codes` 加列时做了**最小的补列迁移**（已存在的库自动补 `name`/`scopes`）。
  再多就得换真正的迁移工具，别在那段上面长出第二套逻辑。

## 六、容器里跑（Linux + Docker）

```bash
docker compose -f server/compose.yaml up -d --build
python scripts/smoke-echo-backend.py --base-url http://127.0.0.1:8900
```

**两个卷的保留策略是相反的，别合并**（详见 `server/compose.yaml` 顶部）：

| 卷 | 内容 | 能不能换 tmpfs |
|---|---|---|
| `/var/echo/tmp` | 临时文件 | **能**（丢了没关系） |
| `/var/echo/state` | 鉴权库（客户端凭据） | **绝对不能**（换了就所有客户端要重新配对） |

## 七、几个"看起来像坏了，其实是设计"的现象

| 现象 | 原因 |
|---|---|
| `variant=short` 和 `long` 落到同一个 `modelId` | 出厂清单没有常驻小模型（设计 §14-2），短请求由长档的 `supports: [asr.text]` 兜住 |
| `timestamps` 回 `none`，哪怕你要了时间戳 | 服务端**不编** `estimated`；"按字数均摊"是客户端拿不到时间戳时的兜底 |
| 两个并发请求，一路 `409 client_busy` | 每客户端硬性 1 路。**这条重试永远不会成功** —— 该等自己那条回来 |
| `503 server_busy` 且带 `Retry-After` | 服务端总通道满。**这条才该退避重试**，且**不排队** |
| 第一次请求特别慢 | 模型是按需加载的，第一次付加载时间。`/v1/health` 里能看状态 |
| 有 GPU 但报 `model_failed` | 服务端**刻意不回退 CPU**（会把所有客户端一起拖慢）。查驱动 / `--gpus all` / spec 的 `device` |
| 大 body 出错时 body 是空的 | **这是 bug，不是设计** —— 已修（`audio.drain`）。若再见到，说明有新的早退路径漏了抽干 |
