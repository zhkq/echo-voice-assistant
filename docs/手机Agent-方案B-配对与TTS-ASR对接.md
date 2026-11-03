# 手机 Agent 开发对接：方案 B 配对 + E2E + TTS/ASR

> 2026-11-02。给另一台专用手机 App 的开发 Agent 使用。
> 目标网络：**手机到 ECHO 只能经能力后端 relay**。手机不直连 ECHO 局域网地址。
> 参考实现：`C:\echo-dev\app\e2e.py`、`C:\echo-dev\app\relay.py`、
> `C:\echo-dev\android\...\data\E2E.kt`、`E2EInterceptor.kt`、`BackendTts.kt`。

---

## 0. 一句话架构

```
手机 --扫码拿到 ECHO 公钥/relay/一次性码--> 手机
    --(加密 claim)--> 后端 relay --> ECHO（解密、校验、发手机 token）
    --(E2E 信封)--> 后端 relay（只看到 api/e2e 密文）--> ECHO（解密、带手机 token 调本机 API）
    --(TTS 可选直连)--> 能力后端 /v1/tts（委托 token + DPoP）
```

手机只信任两样东西：

1. ECHO 屏幕二维码里、由 ECHO 自己签名的 ticket；
2. ticket 里带的后端 TLS 指纹。

**手机不向后端要 ECHO 公钥**。后端只是盲转发方。

---

## 1. 配对：二维码 ticket

### 1.1 ticket 从哪里来

ECHO 面板「设置 → 手机 / 手表 → 生成配对码」调用：

```http
POST http://127.0.0.1:8970/api/pair/phone/ticket
```

返回：

```json
{
  "ok": true,
  "version": 2,
  "ticket": "<base64url，二维码内容>",
  "payload": {"v":2,"host":"...","relay":"...","pub":"...","nonce":"...","exp":1780000000,"code":"123456","backend":{"baseUrl":"https://...","tlsPin":"sha256:..."}},
  "qrPng": "<base64 PNG>",
  "code": "123456",
  "expiresAt": "...",
  "ttlSeconds": 300,
  "target": "...",
  "baseUrl": "...",
  "relayUrl": "...",
  "hostId": "...",
  "backend": {"baseUrl":"...","tlsPin":"..."}
}
```

手机只扫二维码内容 `ticket`。兼容两种形式：

- 原始 ticket（base64url）；当前面板就是这个；
- `echo-pair://v2?ticket=<urlencode(ticket)>`。

**不要**扫旧版 `echo://pair?host=...&code=...`；那是旧配对串，没有 ECHO 公钥。

### 1.2 ticket payload 与签名

解码 base64url 后是 canonical JSON：

```json
{
  "v": 2,
  "host": "<ECHO host id>",
  "relay": "https://echoserver.zhkq.online:8900/v1/relay/<hostId>/",
  "pub": "<ECHO 身份公钥：P-256 uncompressed point，base64url>",
  "nonce": "<一次性 nonce>",
  "exp": 1780000000,
  "code": "123456",
  "backend": {
    "baseUrl": "https://echoserver.zhkq.online:8900",
    "tlsPin": "sha256:<hex>"
  },
  "sig": "<base64url ECDSA DER>"
}
```

校验：

- `v == 2`；
- `exp > now`，且不要使用过期 ticket；
- 用 `pub` 验证 `sig`；
- 签名对象是**去掉 `sig` 后的 canonical JSON**：
  `json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`；
- ECDSA：P-256 + SHA-256，DER 编码，再 base64url 无 padding。

`relay` 是后续所有普通 API 的入口；**不要用服务端返回的局域网 baseUrl 覆盖它**。
`code` 已经在 ticket 里，手机不需要人工输入。

### 1.3 生成设备密钥

- P-256（secp256r1）；
- 私钥放 Android Keystore 或 EncryptedSharedPreferences（参考实现是软件 P-256 + EncryptedSharedPreferences）；
- 公钥编码：uncompressed point，65 字节，`0x04 || X(32) || Y(32)`，base64url 无 padding；
- 每个设备长期使用一对；撤销后重新生成/重新配对。

---

## 2. 加密 claim（配对请求）

### 2.1 手机生成临时密钥

- 临时 P-256 `E_eph`；
- 与 ECHO 公钥 `PK_E` 做 ECDH：

```
shared = ECDH(E_eph_private, PK_E)
```

### 2.2 派生 claim key

```text
key = HKDF-SHA256(
  ikm  = shared,
  salt = UTF8(ticket.nonce),
  info = UTF8("echo-pair-v2:claim"),
  len  = 32
)
```

### 2.3 设备签名

对 canonical JSON 签名：

```json
{
  "v": 2,
  "nonce": "<ticket.nonce>",
  "ephPub": "<E_eph 公钥 base64url>",
  "devicePublicKey": "<设备长期公钥 base64url>"
}
```

签名：设备私钥 P-256 + SHA-256，DER，base64url 无 padding。

### 2.4 加密 claim

plaintext 是 canonical JSON：

```json
{
  "code": "<ticket.code>",
  "deviceName": "Pixel",
  "devicePublicKey": "<设备长期公钥>",
  "signature": "<设备签名>"
}
```

- AES-256-GCM；
- IV：12 字节随机；
- AAD：

```json
{"nonce":"<ticket.nonce>","v":2}
```

canonical JSON 的 UTF-8 字节。

请求：

```http
POST <ticket.relay>/api/pair/phone/claim/v2
Content-Type: application/json

{
  "v": 2,
  "nonce": "<ticket.nonce>",
  "ephPub": "<临时公钥>",
  "iv": "<base64url iv>",
  "ciphertext": "<base64url ciphertext>"
}
```

**这个请求不套 E2E 信封**，它自己已经是加密 claim。

### 2.5 解密响应

- 响应 key：

```text
HKDF-SHA256(shared, salt=UTF8(nonce), info=UTF8("echo-pair-v2:response"), 32)
```

- AAD：`UTF8("echo-pair-response-v2")`；
- AES-256-GCM；
- 解密后 canonical JSON，字段：

```json
{
  "ok": true,
  "token": "<手机 API token>",
  "keyId": 28,
  "name": "Pixel",
  "baseUrl": "<推荐 base URL，可能是 relay>",
  "echoPublicKey": "<PK_E>",
  "deviceId": "<32 字符设备 id>",
  "note": "...",
  "tts": {
    "baseUrl": "https://echoserver.zhkq.online:8900",
    "token": "<TTS 委托 token>",
    "expiresIn": 3600,
    "jkt": "<设备公钥 SHA-256 base64url，完整值>",
    "tlsPin": "sha256:<hex>"
  }
}
```

`tts` 可能为 `null`：说明 ECHO 没配好后端或没有 `tts` scope。手机仍可走 ECHO `/api/tts` 兜底。

存储：

- 手机 API token（用于 E2E 内层 Authorization）；
- `echoPublicKey`；
- `deviceId`；
- 设备私钥；
- `tts` token/baseUrl/tlsPin；
- **E2E 的 base URL 优先用 `ticket.relay`**（不要用可能不可达的局域网地址）。

---

## 3. 配对后的常规 API：E2E 信封

除 `api/pair/*` 外，所有请求都经 relay 的 `api/e2e`，后端只能看到密文。

### 3.1 静态会话键

```text
shared = ECDH(设备私钥, PK_E)
key = HKDF-SHA256(
  ikm  = shared,
  salt = UTF8("echo-e2e-session-v2"),
  info = UTF8("device:" + deviceId),
  len  = 32
)
```

每次请求随机 IV，不需要每次 ECDH。

### 3.2 内层明文

```json
{
  "id": "<每次请求唯一 UUID/随机串>",
  "ts": 1780000000,
  "method": "POST",
  "path": "api/assistant/voice-command",
  "query": "context=review&requestId=...",
  "headers": {
    "authorization": "Bearer <手机 API token>",
    "content-type": "audio/wav"
  },
  "body": "<standard Base64（带 padding）>"
}
```

规则：

- `path` **不要前导 `/`**；
- `query` 不要带 `?`；
- `headers` 只允许 `authorization` 和 `content-type`；
- `ts` 是 Unix 秒，ECHO 允许 ±300 秒；
- `id` 必须每次唯一，ECHO 会拒绝重复 id；
- `body` 是**标准 Base64 + padding**（不是 URL-safe）；这是踩过的坑；
- 空 body 用 `""`。

### 3.3 外层请求

```http
POST <ticket.relay>/api/e2e
Content-Type: application/json
（不要带 Authorization）

{
  "v": 2,
  "device": "<deviceId>",
  "iv": "<base64url 12 字节>",
  "ciphertext": "<base64url AES-GCM(inner canonical JSON)>"
}
```

AAD：`UTF8("echo-e2e-v2")`。

### 3.4 外层响应

```json
{
  "v": 2,
  "device": "<deviceId>",
  "iv": "<base64url>",
  "ciphertext": "<base64url>"
}
```

用同一会话键解密，AAD 同上；内层：

```json
{
  "status": 200,
  "contentType": "application/json",
  "body": "<standard Base64（带 padding）>"
}
```

**注意两套 Base64 不能混**：

| 位置 | 编码 |
|---|---|
| ticket | base64url，无 padding |
| claim `ephPub/iv/ciphertext/signature` | base64url，无 padding |
| E2E 外层 `iv/ciphertext` | base64url，无 padding |
| E2E 内层 `body` | **标准 Base64，带 padding** |
| DPoP | base64url，无 padding |

### 3.5 解密失败/状态处理

- 外层 401：手机 token 失效 → 重新配对；
- 外层 400/502：检查 `id/ts`、Base64、设备是否被撤销；
- 内层 `status` 才是 ECHO API 的真实 HTTP 状态；
- 不要把已经读空的响应体交回 HTTP 客户端，否则只会得到 `closed`。

---

## 4. ASR 调用

ASR 全部走 **ECHO 的 API**，由 ECHO 转发给能力后端。手机**不直接**调 `/v1/asr`（手机没有 ECHO client 凭据）。

下面是 E2E **内层**的 path/body/headers。

### 4.1 一步到位：语音指令

```http
POST api/assistant/voice-command?engine=&lang=&context=&requestId=
Authorization: Bearer <手机 token>
Content-Type: audio/wav

<裸 WAV 字节>
```

- 必须是**裸音频 body**，不是 multipart；
- `context=review` 时落库为回顾轮次；普通指令不传；
- `requestId` 用于回顾轮次幂等；
- ECHO 转写后下发给智能体。

响应：

```json
{
  "ok": true,
  "message": "...",
  "text": "<ECHO 转写文本>",
  "commandId": 123,
  "source": "mobile",
  "duplicate": false
}
```

拿到 `commandId` 后轮询：

```http
GET api/commands/123
Authorization: Bearer <手机 token>
```

直到 `status=done` 或 `failed`。`reply` 是完整详情；`brief` / `speakText` 是**给耳朵听的结论**。自动朗读只念结论，不念 `reply` 全文。

### 4.2 只要文本：STT

```http
POST api/stt/transcribe
Authorization: Bearer <手机 token>
Content-Type: multipart/form-data; boundary=...

file=<wav bytes>
```

响应：

```json
{"text":"...","engine":"echo-server","model":"...","timestamps":"..."}
```

### 4.3 要句子时间戳

```http
POST api/stt/sentences
Authorization: Bearer <手机 token>
Content-Type: multipart/form-data; boundary=...
```

响应：

```json
{"text":"...","sentences":[{"start":0.0,"end":1.2,"text":"..."}],"timestamps":"..."}
```

### 4.4 回顾一轮（裸音频）

```http
POST api/daily-review/turn
Authorization: Bearer <手机 token>
Content-Type: audio/wav

<裸 WAV>
```

响应里有 `text`、`spoken`、`reply`、`ok`、`error` 等。朗读只念 `spoken`，不念 `reply`。

### 4.5 ASR 错误

| HTTP | 含义 | 处理 |
|---|---|---|
| 422 | 没识别出文本 / 音频太短 | 提示重说 |
| 409 | `client_busy`（同一个人的上一条还在跑） | 不立即重试，提示稍后 |
| 503 | `server_busy` / `model_loading` | 按 `Retry-After` 退避 |
| 502 | 转写失败 | 显示原因 |

---

## 5. TTS 调用

### 5.1 手机直连能力后端（推荐，独立 1 QPS）

用配对响应里的 `tts`：

```http
POST <tts.baseUrl>/v1/tts
Authorization: Bearer <tts.token>
DPoP: <ES256 DPoP proof>
Content-Type: application/json

{"text":"你好","voice":"","rate":""}
```

- 这是**独立于 ECHO 的 phone 身份**，后端按 `sub=phone:<deviceId>` 做 1 QPS；
- 响应 `audio/mpeg`（或 `audio/wav`），直接播放；
- `tts.token` 默认 1 小时有效；过期后回退 ECHO `/api/tts` 或重新配对；
- 如果 `tts.tlsPin` 是 `sha256:<hex>`，HTTPS 请求要固定校验该指纹；
- 不要用 ECHO 的 client token 调这个端点。

### 5.2 DPoP proof

Header：

```json
{"typ":"dpop+jwt","alg":"ES256","jwk":{"kty":"EC","crv":"P-256","x":"...","y":"..."}}
```

Payload：

```json
{
  "htm": "POST",
  "htu": "/v1/tts",
  "iat": 1780000000,
  "jti": "<每次唯一>",
  "ath": "<base64url(SHA256(access_token))>"
}
```

- 签名：设备私钥 P-256 + SHA-256；
- JWT 签名必须是 JOSE 的 **raw r||s（64 字节）**，不是 DER；
- `jwk` 必须是设备公钥，且指纹等于 token 的 `cnf.jkt`；
- 后端会校验 `htu/ath/jti`，不要复用 proof。

### 5.3 兜底：ECHO 的 TTS

如果 `tts` 为空/过期，可通过 E2E 调 ECHO：

```http
POST api/tts
Authorization: Bearer <手机 token>
Content-Type: application/json

{"text":"你好","engine":"echo-server"}
```

响应是音频字节。ECHO 自己走 `ttsEngine=echo-server` 时也使用能力后端，但计费/限速身份是 ECHO 的 client，和手机 TTS 的 phone 身份是**两条独立的 1 QPS**。

### 5.4 TTS 错误

| HTTP | 含义 | 处理 |
|---|---|---|
| 401 | 委托 token 无效/过期/撤销 | 回退 ECHO `/api/tts` 或重新配对 |
| 429 | 该 phone 超过 1 QPS | 按 `Retry-After` 退避 |
| 503 | 后端 TTS 不可用 | 回退 ECHO 或系统 TTS |
| 502 | 合成失败 | 显示原因并回退 |

---

## 6. 安全要求（必须做）

- 二维码 ticket 的 ECHO 公钥和签名是唯一信任根；**不许**从后端接口获取 ECHO 公钥。
- 后端只应看到 `api/e2e` 的 `iv/ciphertext`，不能看到手机 token、路径、音频/文本。
- 手机 token 只放在 E2E 内层 `authorization`，不要放在外层 Header。
- 委托 TTS token 只用于 `/v1/tts`，不要用于 ECHO API。
- 后端 TLS 指纹按 ticket 里的 `tlsPin` 固定；没配 HTTPS/指纹时至少使用系统 TLS 校验。
- 私钥、token、设备 id 不写日志；错误日志最多写状态码和异常类型。
- 每次 E2E 请求新 `id`，时钟与 ECHO 偏差不超过 5 分钟。
- 设备撤销后必须重新配对；旧 token、旧会话键都不可继续使用。

---

## 7. 状态与错误处理

| 场景 | 显示/处理 |
|---|---|
| ticket 过期/二维码扫到旧串 | 重新生成二维码 |
| claim 403 | 码过期/已被用，重新扫码 |
| E2E 外层 400 | 检查 `id/ts`、Base64、设备是否撤销 |
| E2E 内层 401 | 手机 token 失效，重新配对 |
| E2E 外层 502 | relay 或 ECHO 解密失败，显示具体错误，不要重试死循环 |
| ASR 409 | 忙，不立即重试 |
| TTS 429 | 按 Retry-After 退避 |
| 设备被撤销 | 停止请求，提示重新扫码 |

**不要**把 E2E 解密失败伪装成 `closed`；必须保留真实错误原因（外层状态/异常类型），否则只能看到“连不上”。

---

## 8. 最小实现清单

1. 扫 ticket，校验签名，拒绝旧 `echo://pair` 串。
2. 生成/保存 P-256 设备密钥。
3. ECDH + HKDF 加密 claim；解密响应；保存 token/deviceId/ECHO 公钥/tts。
4. 实现 E2E 信封拦截器：
   - 外层 URL `<relay>/api/e2e`；
   - 内层 `id/ts/method/path/query/headers/body`；
   - 内层 body 标准 Base64；
   - 响应内层 body 标准 Base64；
   - 外层去掉 Authorization。
5. ASR：`assistant/voice-command` 或 `stt/transcribe`；轮询 `commands/{id}`。
6. TTS：优先直连 `<tts.baseUrl>/v1/tts` + DPoP；失败回退 ECHO `api/tts`。
7. 朗读只念 `speakText` / `brief` / 首段摘要，不念 `reply` 详情。
8. 测试：ticket 验签、claim 双向解密、E2E 空 body/非空 body、ASR、TTS 429。

---

## 9. 参考实现与测试

- ECHO 侧协议：`app/e2e.py`
- ECHO relay 解密/加密：`app/relay.py` 的 `_execute_e2e`
- Android 参考：
  - `android/app/src/main/java/com/echo/contact/data/E2E.kt`
  - `.../data/E2EInterceptor.kt`
  - `.../data/BackendTts.kt`
  - `.../ui/PairViewModel.kt`
- 测试：
  - `tests/test_e2e_pairing.py`
  - `android/app/src/test/java/com/echo/contact/data/E2ETest.kt`

当前真实环境：

- ECHO 稳定版：`http://127.0.0.1:8970`
- 远端 relay/能力后端：`https://echoserver.zhkq.online:8900`
- relay 地址必须从 ticket 取，不要写死。
