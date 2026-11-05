# Phase 3 Before / After 截图

本次改动只动官网的账号、套餐购买与支付页（`site/` + 支付接口）。截图由
`scripts/capture-phase3-evidence.py` 生成；**After 使用本地 mock 能力后端**（订单、权益、
支付状态都是演示数据），不是真实扣款，也不冒充 Lead 的沙箱真机证据。

## 基线（Before）

- 基线：`origin/integration/billing-vibepay`（Phase 3 Lead WIP 之前的站点）
- 视口：1440×900，浅色，中文
- 页面：
  - `before-home.png`：首页（导航还没有账号入口）
  - `before-download.png`：下载页（导航/页脚还没有账号入口）
  - `before-pricing.png`：套餐页，购买入口还是 `/pay?plan=monthly`，页面含「预览版」字样
  - `before-pay.png`：支付页，静态示例订单 + 二维码占位，收银台按钮是 disabled
  - `before-account-404.png`：基线没有 `/account`，真实 404

## 改动后（After）

- After：本分支 + `scripts/capture-phase3-evidence.py` 的 mock API
- 视口：1440×900（另有 375×812 移动端）；浅色，中文
- 页面：
  - `after-home.png`：首页（导航新增「我的」/ 登录入口）
  - `after-download.png`：下载页（导航/页脚新增账号入口）
  - `after-pricing.png`：套餐来自 `GET /v1/billing/plans`；「立即购买」要求登录
  - `after-login.png`：`/account` 登录 / 注册
  - `after-account.png`：登录后的权益 + 最近订单
  - `after-pay.png`：真实订单驱动的支付页（订单号/金额来自服务端，`payUrl` 收银台按钮，
    测试环境标注由订单 `sandbox` 字段驱动）
  - `after-paid.png`：支付成功 + 权益摘要
  - `after-closed.png`：订单关闭（失败/取消）
  - `after-timeout.png`：支付超时（已过 `expiresAt`）
  - `after-account-mobile.png` / `after-pay-mobile.png`：375 宽移动端

## 真实后端 E2E（Mock 支付）

`scripts/e2e-vibepay-mock.py` 起一个**真的** `server.main`（临时 state/auth 库、mock 支付、
`invite.required=false`），把静态站与 `/v1/*` 放在同一个源，用 Chrome 走：

注册 → 账户页（7 天试用）→ 套餐页下单 → 支付页（真实订单号/金额）→ mock 支付端点
→ 刷新/sync → 支付成功 + 权益 → 我的账户（月卡 + 已支付订单）→ 退出 → 未登录购买被登录门挡住 → 登录回 next → 再下单并取消。

产物：`e2e-account.png`、`e2e-paid.png`、`e2e-closed.png`、`e2e.json`。这条链路证明
**站点 JS 与真实后端契约一致**；它仍是 mock 支付，不是支付宝沙箱扣款。

## 说明与边界

- mock 订单的 `payUrl` 指向 `sandbox.example`（仅用于截图，不会真的收款）；真实沙箱
  `page.pay + query + sync` 由 Lead 在 2026-11-05 用沙箱 app `9021000171628513` 实测 10.00 元，
  凭据不在仓库，本目录不复制那次证据。
- 生产 `app_id 2021007117680171` 审核中；公网 HTTPS 与带地址栏截图需部署完成后补采。
- `capture.json` 记录每个文件的 sha256、URL、视口与说明。
