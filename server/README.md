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

## 五、试鉴权（配对 → 令牌）

```bash
# 1) 配置文件里把 auth 打开并配好密钥
#    auth: {enabled: true, mode: jwt, jwt_secret: "<openssl rand -hex 32>"}

# 2) 发一个一次性配对码（管理面还没做，所以走命令行）
python -m server.main --config data/backend-dev/server.yaml --new-pairing-code
#    → echo://pair?host=127.0.0.1:8900&code=7K2M9QX4

# 3) 用码换凭据 → 换令牌 → 带令牌调用
python scripts/smoke-echo-backend.py --pair-code 7K2M9QX4

# 其它管理动作
python -m server.main --config <cfg> --list-clients
python -m server.main --config <cfg> --revoke cli-07a3f2
```

`--revoke` 是**另一个进程**：服务端最迟 `auth.revoke_poll_s`（默认 5 秒）后生效 ——
这是有意的取舍（用 5 秒换掉"每请求查库"），文档里没有把它写成"立即"。

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
