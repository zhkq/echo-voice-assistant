# 方案 B：手机 / ECHO / 能力后端三方信任与 E2E 配对

> 2026-11-01。目标环境：**手机只能经后端 relay 访问 ECHO**，必须做全链路 E2E。
> 本文是协议与实现状态；代码分阶段落地。

## 1. 威胁模型

| 参与方 | 信任假设 |
|---|---|
| 手机 | 相信 ECHO 屏幕二维码里的 ECHO 公钥；相信 ECHO 给的后端 TLS 指纹 |
| ECHO | 相信自己的身份私钥；相信已配对的 backend client 凭据 |
| 后端 | **不可信转发方**：relay 可能被攻破/运营者好奇；但后端 TTS 服务本身按设计需要看到文本 |
| 网络 | 手机↔后端、ECHO↔后端都可能被观察；TLS 只保护到各自终结处 |

要防的是：

- relay 看到配对码 → 抢先兑换手机 token；
- relay 看到手机 token → 冒充手机对 ECHO 调 API；
- relay 看到 TTS token → 重放调用；
- relay 替换 ECHO 公钥/后端地址 → 中间人。

## 2. 密钥与职责

| 密钥 | 生成方 | 用途 |
|---|---|---|
| `SK_E` / `PK_E` | ECHO | ECHO 身份（P-256 ECDSA/ECDH）；二维码公开 `PK_E` |
| `SK_P` / `PK_P` | 手机 | 设备身份；P-256，Android Keystore 保存私钥 |
| `PK_B` / TLS | 后端 | 后端身份；手机用 ECHO 下发的 `tlsPin` 固定校验 |
| 委托 token | 后端签发 | `sub=phone:<id>`、`scope=tts`、`cnf.jkt=设备公钥指纹` |
| DPoP proof | 手机每次请求签 | 证明持有 `SK_P`，防止 relay/后端重放 token |

### ECHO 身份密钥存储

- `{DATA}/echo-e2e-identity.json`：私钥用平台 `protect_secret()`（Windows DPAPI / POSIX 0600）包封；
- 公钥可公开；
- 设备公钥记录在 `{DATA}/echo-e2e-devices.json`（不含令牌）。

实现：`app/e2e.py`。

## 3. 二维码 ticket

ECHO 面板显示二维码，内容不是后端生成的地址，而是 ECHO 自己签的 ticket：

```
echo-pair://v2?ticket=<base64url(canonical json)>
```

payload：

```json
{
  "v": 2,
  "host": "<echo host id>",
  "relay": "https://backend:8900/v1/relay/<host>/",
  "pub": "<ECHO P-256 public key, uncompressed, base64url>",
  "nonce": "<128-bit>",
  "exp": 1234567890,
  "code": "123456",
  "backend": {"baseUrl": "https://backend:8900", "tlsPin": "sha256/..."},
  "sig": "<ECHO ECDSA over payload without sig>"
}
```

- `PK_E` **不经过后端**；手机扫码即信任根。
- `tlsPin` 是 ECHO 在 ECHO↔后端配对时记下的证书指纹；手机用它固定后端。
- 6 位码放在 ticket 里，扫码即可配对；不用背码。
- ticket 5 分钟过期，一次性 nonce 由 ECHO 内存保存。

实现：`app/e2e.py::new_ticket` / `verify_ticket`；接口 `POST /api/pair/phone/ticket`。

## 4. 配对 claim（relay 只能看到密文）

手机：

1. 扫 ticket，取得 `PK_E`、`nonce`、`code`、`relay`、`tlsPin`；
2. 生成临时 P-256 密钥 `E_eph` 和设备长期 `P_dev`；
3. 用 `ECDH(E_eph, PK_E)` → HKDF → AES-GCM key；
4. 加密：
   ```json
   {
     "code": "123456",
     "deviceName": "Pixel",
     "devicePublicKey": "<P_dev>",
     "signature": "ECDSA(P_dev, transcript)"
   }
   ```
5. 发到 `POST /v1/relay/<host>/api/pair/phone/claim/v2`：
   ```json
   {"v":2,"nonce":"...","ephPub":"...","iv":"...","ciphertext":"..."}
   ```

ECHO：

1. 用 `SK_E` + `ephPub` 做 ECDH，解出 claim；
2. 校验 nonce、6 位码、设备公钥签名；
3. 生成手机 token（现有 API key），记设备；
4. 用同一 ECDH shared secret 派生 response key，加密返回：
   ```json
   {
     "ok": true,
     "token": "<ECHO phone token>",
     "baseUrl": "...",
     "echoPublicKey": "<PK_E>",
     "tts": {
       "baseUrl": "https://backend:8900",
       "token": "<delegated TTS token>",
       "expiresIn": 3600,
       "jkt": "...",
       "tlsPin": "sha256/..."
     }
   }
   ```

relay 全程只看到 `nonce/ephPub/iv/ciphertext`，以及加密后的响应。

实现：`app/e2e.py::decrypt_claim` / `encrypt_response`；接口 `POST /api/pair/phone/claim/v2`。

## 5. ECHO → 后端委托 TTS token

ECHO 已与后端配对，因此 ECHO 是唯一能申请委托的客户端：

```http
POST /v1/delegate
Authorization: Bearer <ECHO backend token>
Content-Type: application/json

{"subject":"<device id>","devicePublicKey":"<P_dev>","ttlSeconds":3600}
```

后端：

- 验证 ECHO 的 client token 和 `tts` scope；
- 校验 `P_dev` 是 P-256；
- 计算 `jkt = base64url(sha256(raw P-256 pub))`；
- 签发 JWT：
  ```json
  {
    "sub": "phone:<deviceId>",
    "act": "<ECHO client_id>",
    "scopes": "tts",
    "delegated": true,
    "cnf": {"jkt": "..."},
    "ver": 1,
    "exp": ...
  }
  ```
- 委托 token 不能再委托；
- ECHO 的 token_version 变化（撤销/轮换）会让委托 token 立即失效。

实现：`server/auth.py::issue_delegated`；`server/routes.py::/v1/delegate`。

## 6. 手机 → 后端 `/v1/tts`（DPoP）

```http
POST https://backend:8900/v1/tts
Authorization: Bearer <delegated token>
DPoP: <ES256 proof>
Content-Type: application/json

{"text":"你好"}
```

DPoP proof：

- header：`typ=dpop+jwt`、`alg=ES256`、`jwk`（手机 P-256 公钥）；
- payload：`htm=POST`、`htu` 路径 `/v1/tts`、`iat`、`jti`、`ath=base64url(sha256(access_token))`；
- 后端验证 proof 公钥指纹 == token `cnf.jkt`，再验 `htu/ath/jti` 重放；
- 后端按 `sub=phone:<id>` 做 1 QPS，和 ECHO client 的 1 QPS 独立。

实现：`server/dpop.py`、`server/routes.py::/v1/tts`、`server/auth.py` 的 delegated 分支。

## 7. 信任矩阵（配对完成后）

| 验证方 | 验证 | 依据 |
|---|---|---|
| 手机 → ECHO | 对端是二维码里那台 ECHO | `PK_E` + ticket 签名 + 加密响应 |
| ECHO → 手机 | 对端知道配对码且持有 `SK_P` | 加密 claim + 设备签名 |
| ECHO → 后端 | 后端身份 | ECHO↔后端已有配对 + TLS pin |
| 后端 → ECHO | ECHO 身份 | client_id/secret + JWT |
| 手机 → 后端 | 后端身份 | ECHO 下发的 `tlsPin` |
| 后端 → 手机 | 手机持有 `SK_P` | 委托 token `cnf` + DPoP |

## 8. 尚未完成（方案 B 的剩余部分）

1. **真机端到端验证**：需要重建/重启后端（`/v1/delegate`、DPoP、edge 7.3.1），
   再用新 APK 扫 ECHO 面板二维码，验证 claim、E2E API、TTS 直连、1 QPS。
2. **部署**：能力后端镜像/容器尚未重建；ECHO 稳定版/安装包尚未重新打包。
3. **撤销 UI/轮换**：后端委托撤销接口、ECHO `DELETE /api/pair/phone/device/{id}`
   和面板「E2E 设备身份」撤销按钮已有；设备撤销会同时调用后端
   `/v1/delegate/revoke`；API key 撤销仍单独走「已配设备」；设备密钥轮换还没有专门入口。
4. **后端 TTS 刷新**：委托 token TTL 1 小时；手机端目前过期后回退 ECHO `/api/tts`，
   还没有“向 ECHO 换新 token”的刷新路径。
5. **迁移**：旧 6 位码流程保留兼容；旧 App 仍走旧配对/明文 relay。

## 9. 当前已落地

- `app/e2e.py`：ECHO 身份密钥、ticket、配对加解密、设备记录、**常规 API E2E 信封**；
- `app/api.py`：`POST /api/pair/phone/ticket`、`POST /api/pair/phone/claim/v2`、
  `DELETE /api/pair/phone/device/{id}`；`GET /api/pair/phone` 返回 E2E 设备身份列表；
- `app/relay.py`：`api/e2e` 解密→本机执行→加密响应；后端 relay 只看到密文；
- `server/auth.py`：delegated JWT 校验/签发；`server/delegation.py`：撤销表；
- `server/routes.py`：`POST /v1/delegate`、`/v1/delegate/revoke`、`/v1/tts` 的 DPoP；
- `server/dpop.py`：DPoP proof 校验；
- `server/relay.py`：`api/e2e` 外层免令牌（内层 token 仍在 ECHO 解密后校验）；
- Android：
  - `data/E2E.kt`：P-256/QR ticket/claim/会话信封/DPoP；
  - `data/E2EInterceptor.kt`：常规 API 自动封 E2E；
  - `data/BackendTts.kt`：手机直连后端 TTS + DPoP；
  - `PairStore` 保存设备私钥/ECHO 公钥/TTS token；
  - `PairScreen` 加 ZXing 扫码；`PairViewModel.claimV2`；
  - `app-debug.apk` 已构建（2026-11-01）。
- 单测：`tests/test_e2e_pairing.py`、`tests/test_backend_delegate.py`、
  `android/.../E2ETest.kt`；Python 跨层 411 条通过。
- E2E 信封含一次性 `id` + `ts`，ECHO 端校验 5 分钟窗口并拒绝重放；
  Android `E2EInterceptor` 每次请求生成 UUID/时间戳。

## 10. 远端部署与实测（2026-11-01）

远端 GPU 机（`ssh 24`，`echoserver.zhkq.online:8900`）没有源码树，采用**只读 bind mount
覆盖镜像里的 server 包 + 独立 pylibs**，不动镜像、模型和状态卷：

```yaml
# /home/zysk/echo/backend/compose.yaml（已备份 compose.yaml.bak-schemeB-*）
volumes:
  - ./server-scheme-b:/opt/echo/server:ro
  - ./pylibs:/var/echo/pylibs:ro
environment:
  PYTHONPATH: "/var/echo/pylibs:/opt/echo"
```

- `pylibs` 由容器内 pip 安装 `edge-tts==7.3.1` + `cryptography>=42`；
- `docker compose up -d --force-recreate backend` 后：
  - `/v1/tts`、`/v1/delegate`、`/v1/delegate/revoke` 都返回 **401**（路由存在、鉴权生效）；
  - 用临时测试客户端 `scheme-b-test` 实测：`/v1/token` 200 → `/v1/delegate` 200 →
    DPoP `/v1/tts` **200 audio/mpeg 13968 B**；
- 给现有 ECHO client `cli-619e43` 补了 `tts` scope：
  `--set-scopes cli-619e43 --scopes asr,diarize,speaker,tts`；
- 临时测试客户端已 `--revoke cli-d54a28`。

### 临时 dev ECHO 的配对实测

用独立 `ECHO_DATA`、端口 `18970` 启动新 ECHO（不碰稳定版 8970），实测：

- `POST /api/pair/phone/ticket` → 200，返回 ticket/二维码；
- 手机侧临时 P-256 密钥 + 加密 claim → `POST /api/pair/phone/claim/v2` → 200；
- ECHO 响应可解密，拿到 `token`、`deviceId`、`echoPublicKey`；
- 用该 token 直连 `GET /api/status` → 200；
- `tts` 字段为空（临时 ECHO 没有后端凭据，符合预期；真实 ECHO 有 remote credentials）。

这验证了**二维码 ticket → 设备密钥加密 claim → ECHO 解密 → 加密返回 token → token 可用**，
不只停留在单测。

⚠️ **稳定版已部署**（2026-11-01 12:03，`deploy-stable.ps1 -Restart`）：
- 稳定版 runtime 补装 `qrcode 8.2 + Pillow 13`；
- 8970 重启后 3 秒恢复；
- 真实 ticket/claim/relay/E2E/TTS 已实测，见 §11。

回滚远端：
```bash
cd /home/zysk/echo/backend
cp compose.yaml.bak-schemeB-<时间> compose.yaml
docker compose up -d --force-recreate backend
```

## 11. 稳定版真实链路实测（2026-11-01 12:05）

稳定版 `D:\ECHO` 部署新 app 后，用 Python 模拟手机走真实链路：

- `POST /api/pair/phone/ticket` → 200（真实 relay 地址 + QR PNG + 后端 TLS pin）；
- 经真实 relay `POST /api/pair/phone/claim/v2` → **200**；
- 手机侧解密响应，拿到 `token`、`deviceId`、`tts` 委托 token；
- 经真实 relay `POST /api/e2e`（E2E 信封，含 id/ts/手机 token）→ **200**，
  解出的内层 `GET /api/status` 为 200；
- 用委托 token + DPoP 直连远端 `/v1/tts` → **200 audio/mpeg 19584 B**；
- ECHO 自身 `ttsEngine=echo-server` 后 `POST /api/tts` → **200 audio/mpeg 16704 B**；
- 测试设备/API key 已撤销，`DELETE /api/pair/phone/device/{id}` 返回
  `ttsRevoked=true`，设备身份列表回到 0。

这说明：
- 配对、常规 API E2E、后端委托、DPoP、ECHO 自身 TTS 都已在真实 8970 + 远端后端上跑通；
- 唯一未做的是**真实 Android 手机安装 APK 并扫码**（没有可用设备/模拟器）。

