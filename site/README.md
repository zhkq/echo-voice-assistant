# ECHO 官网（首页 / 套餐 / 登录与账户 / 支付 / 下载）

这是 ECHO 的独立静态站，提供产品介绍、套餐购买、支付宝收银台跳转、订单/权益查看与客户端下载。
页面本身**没有构建链**，只引用站内 `assets/`；金额、套餐名、订单号、订单状态与权益全部来自能力后端
`/v1/billing/*` 与 `/v1/account/*`。

> 本文描述代码与部署约束，不代表公网已经上线。实际域名、证书与支付宝应用状态以部署环境为准。

## 目录

| 路径 | 内容 |
|---|---|
| `index.html` | 首页 `/`：产品名/logo、功能点、隐私边界、套餐与下载入口 |
| `pricing.html` | 套餐页 `/pricing`：从 `GET /v1/billing/plans` 读套餐；「立即购买」先校验登录，再 `POST /v1/billing/orders` |
| `account.html` | 登录 / 注册与我的账户 `/account`：邮箱注册（邮件验证码 + 密码 + 邀请码）、邮箱登录、忘记密码、权益、最近订单、退出登录 |
| `pay.html` | 支付页 `/pay?order=<订单号>`：订单摘要、真实 `payUrl`、轮询 + 主动 sync、成功/关闭/超时/退款/错误状态 |
| `download.html` | 下载页 `/download`：Windows / macOS / Android 安装包、版本/大小/构建日期与 SHA-256 |
| `assets/plans.json` | 静态预览时的套餐回退数据；线上以 `/v1/billing/plans` 为准 |
| `assets/downloads.json` | 下载页与 `downloads/README.md` 的唯一事实源 |
| `assets/site.css` | 全站样式（浅色、桌面 + 移动自适应，无第三方字体/CDN） |
| `assets/site.js` | 账号、套餐下单、支付轮询/sync、订单与权益渲染 |
| `assets/echo-logo.png` | ECHO 现有图标（未改品牌） |
| `downloads/` | 安装包托管目录（部署时放入三个安装包，见下） |

`/`、`/pricing`、`/account`、`/pay`、`/download` 是干净 URL。静态托管需要把 `/pricing` 映射到
`pricing.html`、`/account` 映射到 `account.html`、`/pay` 映射到 `pay.html`、`/download` 映射到
`download.html`；本地预览和截图脚本已经这样做。

## 页面流程

1. **注册 / 登录 / 忘记密码**：注册先调 `POST /v1/account/email/send-code`
   （`purpose=register`）收邮件验证码，再调 `POST /v1/account/register`（邮箱 + 验证码 +
   密码 + 邀请码）；登录调 `POST /v1/account/login`（邮箱 + 密码）；忘记密码先发
   `purpose=reset` 的码，再调 `POST /v1/account/password/reset`。
   `userToken` 存在浏览器 `localStorage`；退出登录调 `POST /v1/account/logout` 并清本地状态。
   令牌失效（401）时清掉并回登录页。邮箱账号没有「工号 + 邀请码」入口。
2. **选套餐 → 下单**：`/pricing` 先 `GET /v1/billing/plans`；点「立即购买」时若未登录先跳
   `/account?next=…`，已登录则 `POST /v1/billing/orders {planCode}`，成功后跳到
   `/pay?order=<orderId>`。
3. **支付**：`/pay` 用 `GET /v1/billing/orders/{id}` 读订单摘要（套餐名/天数/金额/订单号/状态/
   当前到期/支付后预计到期都来自服务端），用服务端返回的 `payUrl` 打开支付宝收银台，
   **不使用静态二维码/假二维码**。页面每 3 秒轮询订单，从支付宝回跳（`return_url` 自动带
   `?order=`）或点「刷新订单状态」时调 `POST /v1/billing/orders/{id}/sync` 主动查询并补记支付。
   预计到期与起算口径来自 `projectedPaidUntil / currentPaidUntil / renewalBasis / renewalRule`，
   页面**不自己写“支付成功后起算”**。
4. **结果**：`paid` → 成功页 + 权益摘要；`closed`/超时 → 重新购买；`refunded` → 已退款；
   读不到/网络失败 → 明确错误与重试。支付页不会把「等待支付」当成功。
5. **我的账户**：`GET /v1/account/me` + `GET /v1/billing/orders` 展示当前权益（到期时间、今日剩余
   转写分钟、TTS 字符、QPS/并发、设备数）和最近订单；权益卡显示服务端下发的
   `renewalRule / planChangeRule / refundRule`，续费入口回 `/pricing`。订单列表显示全部状态
   （待支付/已支付/已退款/已关闭），不只显示 paid。
   **我的设备**（issue #23）：同一份 `me.devices` 渲染设备名/平台/绑定时间/最近活跃，
   并提供解绑（`POST /v1/account/devices/unbind`）；设备由客户端登录后自动绑定，
   账户页不再展示配对码。Before/After 截图见 `docs/account-device-evidence/`。

### 测试环境标注

支付页的「测试环境」提示由订单响应的 `sandbox` 字段驱动：后端 `alipay.sandbox=true` 时显示，
切到生产（`alipay.sandbox=false`）后**自动消失**，不需要改页面文案。正式页面（首页/套餐/下载）
不含「预览版 / 沙箱 / 尚未接入」字样。

## 本地预览

没有后端时可用静态预览（套餐会回退到 `assets/plans.json`，登录/下单/支付会明确报不可用）：

```bash
python scripts/capture-vibepay-evidence.py --serve --port 8090
# http://127.0.0.1:8090/  /pricing  /account  /pay?order=...  /download
```

要联调真实接口，在本机起能力后端并让静态站同源反代 `/v1/*`（见下面 Nginx 形状），或者用
`scripts/capture-phase3-evidence.py` 的 mock 后端采集截图。

Phase 3 截图（Before = `integration/billing-vibepay` 基线，After = 本分支 + mock 订单）：

```bash
python scripts/capture-phase3-evidence.py
# 产物在 docs/vibepay-evidence/phase3/

python scripts/e2e-vibepay-mock.py
# 起真实 server.main（mock 支付）+ 静态站，走注册→下单→支付→权益→退出的端到端冒烟；
# 产物 e2e-*.png / e2e.json（仍是 mock 支付，不是支付宝沙箱扣款）
```

## 下载页与安装包

版本按 issue #9 确认的**内测版 `0.9.0-beta`**；Windows/macOS 基于 ECHO 3.0.1 构建，
Android 为 debug 1.2。`site/assets/downloads.json` 是唯一事实源；`download.html`
仍是静态 HTML（不做运行时注入），发版时用脚本同步：

```bash
# 改 downloads.json 后，把版本/日期/文件名/大小/SHA 写回页面与 README
python scripts/update-download-page.py --write
# 校验三处是否一致（CI 也会跑）
python scripts/update-download-page.py --check
```

页面指向站内相对链接：

| 平台 | 站内路径 | 文件大小 | SHA-256 |
|---|---|---|---|
| Windows | `/downloads/ECHO-Windows-0.9.0-beta.zip` | 74,895,328 字节（74.9 MB / 71.4 MiB） | `CC6B5AE5850EAB786C1C98248DC76BAEBFB3E5BD4FB67B84218B5EF2B71FFAE8` |
| macOS | `/downloads/ECHO-macOS-0.9.0-beta.zip` | 8,784,030 字节（8.8 MB / 8.4 MiB） | `76BCD9690E9AAF1AB8FBB5147C62A4DB512605A725D1C4D95EB1BC1F11DD2B51` |
| Android | `/downloads/ECHO-Android-0.9.0-beta-debug.apk` | 22,346,187 字节（22.3 MB） | `5ADD81EBE1B86CD5D46968E36AFBB25C5FC2B61A38133C9BA7975202B7CE0C15` |

Windows/macOS 文件是 GitHub Release `v3.0.1` 资产的改名副本（字节不变）；Android 是
`v3.0.1` 交付包里同一份 APK。部署时把同名文件放进静态站的 `/downloads/` 即可；不要改名，
否则页面上的文件名与 SHA-256 会对不上。大文件不要提交进 git。

部署/发版前跑 `scripts/verify-downloads.ps1` 检查实际文件；页面注明 Android 是**内测
debug 包**，安装需允许「安装未知应用」，正式上架前再换 release 签名版。

## 套餐数据

线上套餐唯一来源是 `GET /v1/billing/plans`（金额单位是 **分**，camelCase）。月卡与后端
`DEFAULT_PLANS` 对齐：`priceCents=1000`、`days=30`、`dailyAudioMinutes=120`、
`ttsChars=50000`、`qps=1`、`maxDevices=3`、`maxConcurrent=1`。7 天试用同额度、
`priceCents=0`，只在页面提示为邀请制，不在本页下单。

`assets/plans.json` 只用于没有后端时的静态预览回退；改价先改管理面/服务端，不要改静态文件冒充线上价格。

## 部署（页面代码之外由发布流程执行）

**不要把“代码已合并”写成“支付已上线”。** 部署约束：

1. 静态站放独立目录（例如 `/srv/echo-site`），只读；不要把 `site/` 混进能力后端的数据目录。
2. HTTPS 必须是公信 CA 证书（支付宝不认自签）；从公网 `curl -v` 验证 200 且证书链通过。
3. `/v1/*` 反代到能力后端；静态站只占 `/`、`/pricing`、`/account`、`/pay`、`/download`、
   `/assets/*`、`/downloads/*`。`/v1/billing/alipay/notify` 必须公网可达。
4. 不要让反代把 `Authorization`、`Content-Type: application/json` 或大文件响应改写掉。
5. `/downloads/*.zip` 要返回 `Content-Type: application/zip` 与正确的 `Content-Length`。
6. 支付宝配置**只读文件路径**：`alipay.app_id`、`alipay.private_key_path`（应用私钥）、
   `alipay.public_key_path`（支付宝公钥）、`alipay.gateway`、`alipay.sandbox`、
   `alipay.notify_url`、`alipay.return_url`、`alipay.timeout_s`。密钥/沙箱凭据不进 git、
   不进日志、不进 PR；`.alipay-sandbox.json` / `data/backend-dev/` 均忽略。
7. `return_url` 配成站点的 `/pay`（服务端下单时自动追加 `?order=<orderId>`）；
   `notify_url` 配成公网 `https://<域名>/v1/billing/alipay/notify`。回调丢失时支付页的
   sync 是本地兜底，但上线前 notify 仍必须可达。

一个最小 Nginx 形状（域名/端口以实际反代为准，不要直接照抄上线）：

```nginx
server {
    listen 443 ssl;
    server_name echoserver.zhkq.online;
    root /srv/echo-site;
    index index.html;

    location /v1/ {
        proxy_pass http://127.0.0.1:8900;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location /downloads/ {
        # 安装包：静态文件；公开下载，不要鉴权
        try_files $uri =404;
    }
    location / {
        try_files $uri $uri.html =404;
    }
}
```

## 沙箱联调与验证边界

- 代码侧单测用 Mock 网关 + 本地生成的 RSA 密钥验证 `create_pay` / `query` / `refund` 的签名与验签、
  金额/订单号校验、状态机与权益重算；`POST /orders/{id}/sync` 另有接口用例。
- **真实支付宝沙箱付款**需要沙箱 `app_id`、应用私钥、支付宝公钥与可访问的网关。Lead 已在
  2026-11-05 用沙箱 app `9021000171628513` 完成 `page.pay + query + sync` 的 10.00 元实测；
  这些凭据不在仓库、也不在本机。代码侧**没有**在当前环境重跑真实扣款，验收时请以 Lead 的
  沙箱截图/日志 + 本分支的自动化测试为准，不要把本地 mock 截图当成真实付款证据。
- 生产 `app_id 2021007117680171` 审核中；切生产前把 `alipay.sandbox` 设为 `false`、
  换生产密钥路径与 `gateway`，并重新验证 `return_url`/`notify_url`。

## 不做的部分

- 不做第三方登录、短信验证码、扫码授权登录。
- 不做自动续费、优惠券、部分退款；退款是管理面全额退款 + 按剩余 `paid` 订单重算权益。
- 第一版不做升级/降级 proration：有有效付费权益时只能续费同一套餐，换套餐返回
  `409 plan_change_unsupported`；过期/退款/试用状态下可重新购买任意上架套餐。
- 不做域名备案；公网 HTTPS 与支付宝签约状态由发布流程确认。
- 不在本分支合并 `main`；目标分支是 `integration/billing-vibepay`。
