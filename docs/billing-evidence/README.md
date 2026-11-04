# 管理面 UI 变更的 Before / After 证据

配套 `02-实现说明.md`。**管理面是本次唯一改到的 UI**（客户端 `web/` 未动）。

## 采集条件

| 项 | 值 |
|---|---|
| 视口 | 1440 × 900，deviceScaleFactor 1，浅色（管理面默认深色主题，两边一致） |
| 浏览器 | 系统 Google Chrome（headless，Playwright 驱动） |
| 数据 | 临时 SQLite（`/tmp/echo-evidence/*/state/echo-server-auth.db`），**不是真实库** |
| 管理员 | 临时账号 `ops`（口令仅用于本地采集，不是任何真实凭据） |
| 引擎 | 假引擎（只为起 app，不加载模型） |
| 页面 | `http://127.0.0.1:<admin>/admin/`，登录后停在「概览」 |
| Baseline | `origin/main` @ `1b55158`（release v3.0.1，本 PR 的 base） |
| After | 本 PR 分支 review 修复后（2026-11-04 重采；含 `effectiveGating`、套餐 `active` 勾选、退款/权益 confirm 文案；具体 head 见 PR 描述） |

Before 与 After 的 **overview 截图是同一场景**（同样的 server id / 空数据 / 视口 / 主题），
唯一差别是 After 的页签栏多了「收费 / 账号」。After 的 `billing` 截图展示新页签的实际内容
（套餐、订单、权益；示例 user/order/entitlement 由采集脚本写入临时库）。

| 文件 | 说明 |
|---|---|
| `before-admin-overview.png` | baseline：概览页（无「收费 / 账号」页签） |
| `after-admin-overview.png` | after：同一概览页（多出「收费 / 账号」页签） |
| `after-admin-billing.png` | after：「收费 / 账号」页签首屏 |
| `after-admin-billing-full.png` | after：同一页签完整长图（套餐/订单/权益/账号/邀请码） |
