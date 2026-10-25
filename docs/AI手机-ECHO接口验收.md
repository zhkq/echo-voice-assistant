# ECHO 手机接口验收

> 版本：v1.0（2026-10-22）
> 目的：App 联调前，ECHO 侧逐条验证 [主设计 §6](AI手机-ECHO-App-设计.md) 的契约；发现问题先改 ECHO 或先改文档，**不要让 App 去猜**。
> 基线：ECHO dev `2.1.7`；局域网（`serverBindMode=lan`）与中继两条路都要过。
> 配套：[P0 开发清单](AI手机-App-P0开发清单.md) · [MCP 工具契约](AI手机-MCP工具契约.json) · [请求集](AI手机-ECHO接口验收.http)

---

## 0. 先决条件

- ECHO 已启动，`GET /health` 返回 `{"ok": true}`。
- 要测局域网：设置 `serverBindMode=lan`（会自动强制 `apiAuthEnabled=true`）。
- 要测中继：`relayEnabled=true`，且 `GET /api/relay` 显示已连上、有 `publicUrl`。
  ⚠️ **发配对码这一条仍要求 `serverBindMode=lan`**（`POST /api/pair/phone` 只看 lan 档）；中继配对也要先在 PC 上开 lan 档生成码。这是 ECHO 现行行为，不是本文新增的限制。
- 准备一台「设备」：手机或另一台机器（不要用 ECHO 本机，否则会命中回环豁免，测不出鉴权）。
- 准备一段 `sample.wav`：**16 kHz 单声道 PCM16**，1–5 秒即可。
- 工具：`curl.exe`（Windows 自带）或 VS Code REST Client（用 [.http 请求集](AI手机-ECHO接口验收.http)）。

> 端口以实际为准：`data/echo-port.txt` 里是**实际监听端口**；下面统一写 8970。

---

## 1. 五分钟冒烟

### 1.1 生成配对码（只能在 ECHO 本机）

```powershell
# 必须从 127.0.0.1 调；从别的机器调会 403
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8970/api/pair/phone'
```

期望：

```json
{
  "ok": true,
  "code": "482913",
  "expiresAt": "2026-10-22 23:12:00",
  "ttlSeconds": 300,
  "baseUrl": "http://192.168.1.170:8970",
  "addresses": ["192.168.1.170"],
  "pairString": "echo://phone?host=192.168.1.170:8970&code=482913"
}
```

失败：

- `403`：不是从回环调的（或经中继/代理进来）。
- `409`：没开 `serverBindMode=lan`，或本机没有私有地址。

### 1.2 兑换令牌（设备侧）

```bash
curl.exe -s -X POST "http://192.168.1.170:8970/api/pair/phone/claim" \
  -H "Content-Type: application/json" \
  -d "{\"code\":\"482913\",\"name\":\"smoke-test\"}"
```

期望：

```json
{
  "ok": true,
  "token": "echo_…（只出现这一次）",
  "keyId": 3,
  "name": "smoke-test",
  "baseUrl": "http://192.168.1.170:8970",
  "note": "令牌只出现这一次，请设备侧立刻存进安全存储"
}
```

记下 `token`；后面所有请求都带：

```
Authorization: Bearer <token>
```

失败：

- `403`：码不对/过期/已用过/错太多次被锁。
- `422`：body 不是对象（例如把 `code` 写成数字）。

### 1.3 状态

```bash
curl.exe -s "http://192.168.1.170:8970/api/status" \
  -H "Authorization: Bearer <token>"
```

断言：

- `version` 非空；
- `agent` 里有 `online`/`status`；
- `meeting` 里有 `active`/`paused`/`source`；
- `busy`/`busyOwner` 存在。

### 1.4 指令（不执行危险动作）

```bash
curl.exe -s -X POST "http://192.168.1.170:8970/api/assistant/command" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d "{\"text\":\"请只回复一句：收到\",\"source\":\"mobile\"}"
```

期望：

```json
{ "ok": true, "message": "已提交", "commandId": 123, "source": "mobile" }
```

轮询：

```bash
curl.exe -s "http://192.168.1.170:8970/api/commands/123" \
  -H "Authorization: Bearer <token>"
```

断言：`status` 最终 `done`，`brief`/`reply` 至少有一个非空；失败时 `error` 有原因。

### 1.5 会议（手机麦）

```bash
CID="phone-$(date +%Y%m%d%H%M%S)-a1b2"
START="$(date +%Y-%m-%dT%H:%M:%S)"

curl.exe -s -X POST "http://192.168.1.170:8970/api/meeting/start?source=phone&clientId=$CID&startedAt=$START" \
  -H "Authorization: Bearer <token>"

curl.exe -s -X POST "http://192.168.1.170:8970/api/meeting/segment?clientId=$CID&seq=1&source=phone" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: audio/wav" \
  --data-binary "@sample.wav"

curl.exe -s -X POST "http://192.168.1.170:8970/api/meeting/stop?clientId=$CID" \
  -H "Authorization: Bearer <token>"
```

断言：

- `start` 回 `meetingId`、`duplicate=false`；**再调一次** `start` 应 `duplicate=true` 且 `meetingId` 相同。
- `segment` 回 `segmentCount=1`、`seqCount=1`；**重传同一段**应 `duplicate=true` 且段数不增。
- `stop` 回 `ok=true` 后，`GET /api/meetings` 里出现这一场，状态进入转写/完成。

> 这段脚本用 Git Bash 的 `date`；PowerShell 里用 `(Get-Date).ToString("yyyy-MM-ddTHH:mm:ss")`。

### 1.6 回顾

```bash
curl.exe -s -X POST "http://192.168.1.170:8970/api/daily-review/start" \
  -H "Authorization: Bearer <token>"

# 准备（2026-10-25）：立刻返回 ack/topics；不碰麦克风
curl.exe -s -X POST "http://192.168.1.170:8970/api/daily-review/pre" \
  -H "Authorization: Bearer <token>"

# 准备进度（App 每 2.5s 轮询这一条；只读）
curl.exe -s "http://192.168.1.170:8970/api/daily-review/pre" \
  -H "Authorization: Bearer <token>"

# 只停准备（用户点「先开始说」）
curl.exe -s -X POST "http://192.168.1.170:8970/api/daily-review/pre/stop" \
  -H "Authorization: Bearer <token>"

curl.exe -s -X POST "http://192.168.1.170:8970/api/daily-review/turn" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: audio/wav" \
  --data-binary "@sample.wav"

curl.exe -s -X POST "http://192.168.1.170:8970/api/daily-review/stop" \
  -H "Authorization: Bearer <token>"
```

断言：

- `start` 回 `sessionId`；`ready=false` 时 `error`/`reason` 说清缺什么。
- `pre`（POST）**立即**返回 `ok=true` + `ack`（含「回顾准备」/「企微」）+ `topics`（4 条）+ `etaSec`；再调一次回 `already=true`；今天扫完再调回 `done=true`。
- `GET /pre` 的 `phase` 从 `pre` 走到 `done`、`finished=true`、`reply` 带计数；**只有 done+finished 才算完成**。全程电脑麦克风不亮、日志无 `start_review`。
- `pre/stop` 回 `{ok:true, preStopped:true|false, pre:{phase:"stopped"}}`。
- `turn` 可能耗时 2–3 分钟；`ok=false` 时仍有 `spoken` 或 `error`，HTTP 仍是 200。
- `stop` 回 `{ok:true, stopped:true|false, preStopped:true|false}`；`false` 不是故障。
- **绝对不要**为了测试去调 `/api/daily-review/go`（会开电脑的麦）。
- 经中继调 `/pre` 也要验一次：`https://<后端>/v1/relay/<clientId>/api/daily-review/pre` 返回 200（旧版 ECHO 是 404，正是本轮 App 降级的原因）。

### 1.7 录音格式（m4a / amr 负例）

先造两个样本（ffmpeg 有装的话）：

```bash
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=1" -ar 44100 -ac 2 -c:a aac sample.m4a
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=1" -ar 8000 -ac 1 -c:a libopencore_amrnb -b:a 12.2k sample.amr
```

断言：

| 请求 | 样本 | 期望 |
|---|---|---|
| `POST /api/meeting/segment?clientId=…&seq=1&source=phone` | `sample.m4a` | **409**，`detail` 说“不是音频（WAV/FLAC 都不是）” |
| 同上 | `sample.amr` | **409**，同上 |
| `POST /api/assistant/voice-command` | `sample.m4a` | **422**，音频解码失败 |
| 同上 | `sample.amr` | **422** |
| `POST /api/daily-review/turn` | `sample.m4a` / `.amr` | **422** |
| 同上三条 | `sample.wav` | 正常 |

说明：

- 这是**有意的上传契约**，不是 bug。会议段只认 `RIFF`/`fLaC` 魔数；语音/回顾走 libsndfile，实测 1.2.2 不认 MPEG-4/AAC 与 AMR。
- `audiofile.decode_to_wav()` 在**已落盘段**上能用 ffmpeg 解 m4a/amr；但它不是上传闸门，且桌面机不保证有 ffmpeg。**不要用它来推断“服务端收 m4a/amr”**。
- App 侧必须在手机本地转成 16k 单声道 PCM16 WAV 再上传；见主设计 §6.5「录音格式」。

---

## 2. 契约清单

逐条打勾；每条都要留证据（响应片段/截图/日志）。

| # | 接口 | 断言 | 失败场景 | 证据 |
|---|---|---|---|---|
| 1 | `POST /api/pair/phone` | 回 `code/expiresAt/ttlSeconds/baseUrl`；码 6 位 | 非回环 403；未开 lan 409 | |
| 2 | `POST /api/pair/phone/claim` | 回 `token/keyId/baseUrl`；令牌只回一次 | 错码/过期/锁定 403；字段类型错 422 | |
| 3 | `GET /api/status` | 含 `version/agent/busy/busyOwner/meeting` | 无令牌 401；坏令牌 401 | |
| 4 | `POST /api/assistant/command` | 回 `commandId`；`ok=false` 时 HTTP 200 | 空文本 400 | |
| 5 | `GET /api/commands/{id}` | 回 `status/brief/reply/speakText/origin` | 不存在 404 | |
| 6 | `GET /api/commands` | 回 `total/items`；不含 `source=review` | 无 | |
| 7 | `POST /api/meeting/start` | `clientId` 幂等；重复 `duplicate=true` | 重名/冲突 200+ok=false | |
| 8 | `POST /api/meeting/segment` | `(clientId,seq)` 幂等；回 `segmentCount` | 无会/不匹配 409；非音频 409 | |
| 8b | 录音格式 | 会议段收 WAV/FLAC；语音/回顾收 WAV（普通 FLAC 可） | m4a/amr：段 409、语音/回顾 422 | |
| 9 | `POST /api/meeting/stop` | `clientId` 护栏；不误停别场 | `clientId` 不符 `ok=false` | |
| 10 | `GET /api/meetings` | 行含 `source/transcribing/has_summary` | 无 | |
| 11 | `GET /api/meetings/{id}` | `lines/speakers/segment_count` 存在 | 不存在 404 | |
| 12 | `GET /api/meetings/{id}/file` | JSON `{exists,content}`；summary 带 `summaryState` | 坏 kind 400 | |
| 13 | `GET /api/meetings/{id}/audio` | 回 WAV；Range 可用 | 段不存在 404 | |
| 14 | `GET /api/transcribe/status` | map 的键可能是 id 或 name | 无 | |
| 15 | `POST /api/daily-review/start` | 回 `sessionId/accessOk`；幂等 | 未配好 `ok=false` | |
| 16 | `POST /api/daily-review/turn` | 回 `text/spoken/registered`；业务失败仍 200 | 非音频 422 | |
| 17 | `POST /api/daily-review/submit` | 回 `spoken/registered`；阻塞但可完成 | 空文本 400 | |
| 18 | `POST /api/daily-review/stop` | 回 `stopped`；false 不是故障 | 无 | |
| 19 | `GET /api/daily-review/history` | 回 `items/total/today` | 无 | |
| 20 | `GET /api/daily-review/history/{date}` | 回 `turns[]`；坏日期 200+ok=false | 坏日期 | |
| 21 | 中继全链路 | 只带 `Authorization`+`Content-Type` 也能过；不依赖 `Accept`/会话头 | 被拒路径 403 | |
| 22 | 网络拒收 | `PUT /api/settings`、`DELETE /api/meetings/{id}`、`POST /api/system/restart`、`POST /api/control/echo/stop` 从网络 403 | — | |

---

## 3. 中继专项

中继的硬约束（详见主设计 §6.1）：

- 只放行 `api/*` 与 `health`；
- 只转发 `Authorization` 与 `Content-Type`；
- 响应体整体缓冲，不能用 SSE；
- 单次 24MB、主机侧 600 秒、按 IP 120 次/分钟。

验收：

1. 用中继 base `https://<后端>/v1/relay/<hostId>` 走一遍 1.2–1.6。
2. 抓包/日志确认：**不带** `Mcp-Session-Id`、`Accept: text/event-stream` 也能成功。
3. 用 `curl` 故意少带 `Authorization` → 中继 401；带被拒路径 → 403 `relay_forbidden_path`。
4. 大段：24MB 边界（16k mono PCM16 约 12 分钟）应 413，而不是超时。
5. 中继 `baseUrl` 为空时，App 用自己请求的地址兜底——确认 `claim` 响应可以 `baseUrl=""`（不算错误）。

---

## 4. 错误体与状态码

| 场景 | 期望 |
|---|---|
| 无令牌/坏令牌（局域网） | 401 `{"detail":"无效或缺失 API 密钥"}` |
| 网络拒收（改设置/删会议/停服/重启） | 403，`detail` 说清“不允许从手机/手表…” |
| 配对码错 | 403，`detail` 说“码不对或已过期”/“试错太多，锁 N 秒” |
| 会议不匹配/无会 | 409 `{"detail":"…"}`，且**没有副作用** |
| 段不是音频 | 409（或 422，按实现），`detail` 说“不是音频” |
| 音频不是音频（voice/review） | 422，`detail` 说“音频解码失败/请用裸 body” |
| 业务失败（忙/没听清） | 200 + `{"ok":false,"message"/"error":"…"}` |
| 中继主机离线 | 502/503 + `{"code":"relay_host_offline","message":"…"}` |
| 中继限流 | 429 + `retryAfter` |

---

## 5. 已知缺口与建议

这些不是 App 的锅；验收时若遇到，按「建议」排期：

1. **`/api/keys*` 从网络可达**：拿着手机令牌能列/建/删密钥。App 不需要；建议加进 `_NETWORK_DENIED`。
2. **`GET /api/logs`、`GET /api/settings` 可能含敏感信息**：建议对网络调用脱敏或收紧。
3. **`/api/assistant/command` 没有通用幂等键**：App 超时后不能安全重试；建议支持 `requestId`（不只 `review`）。
4. **会议列表没有 `total`**：App 只能靠 `len(items)==limit` 判断下一页；建议补 `total`。
5. **`meeting/pause` 注释与实现不一致**：现行是“写零、时间轴连续”，安卓旧注释还写“不写盘”；建议统一。
6. **可选 `GET /api/app/manifest`**：返回 `apiVersion/features/limits`，App 不用猜版本。

> **不是缺口**：m4a/amr 直传不支持是**有意的上传契约**（会议段只认 WAV/FLAC）。手机侧转 WAV 即可，见主设计 §6.5「录音格式」。若将来一定要 ECHO 收，按“入口归一化”做（先解成 16k 单声道 WAV 再落盘），**不要**把收段闸门直接放行 m4a/amr 原样存盘——那会连带改播放、压缩与转写。

---

## 6. 验收记录

| 日期 | 测试人 | ECHO 版本 | 模式 | 结果 | 备注 |
|---|---|---|---|---|---|
| | | | 局域网 | | |
| | | | 中继 | | |

**结论**：☐ 通过　☐ 有条件通过（列出条件）　☐ 不通过（列出阻塞项）

---

## 7. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-22 | 首版：配对、状态、指令、会议、回顾、中继、错误体、缺口 |
