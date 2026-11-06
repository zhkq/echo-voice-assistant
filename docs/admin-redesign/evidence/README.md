# 管理面重设计 · Before / After 证据

配套：[设计说明](../01-设计说明.md)、[静态设计稿](../mockup.html)、issue #16。

## 采集条件

| 项 | 值 |
|---|---|
| Baseline | `integration/billing-vibepay` @ `16d61a3`（旧深色管理面） |
| After | 本 ticket 分支（新浅色管理面），实现 commit 见 PR |
| 视口 | 1280×900、768×1024、375×812；deviceScaleFactor 1 |
| 浏览器 | 系统 Google Chrome headless（Playwright 驱动） |
| 数据 | 临时 SQLite（`/tmp/echo-admin-evidence-*`）+ 假引擎 + `payment.provider=mock`；**不是真实库** |
| 管理员 | 临时账号 `ops`，口令仅用于本地采集 |
| 页面 | `http://127.0.0.1:18901/admin/`（temp ports 18900/18901，避免占用真实后端 8900/8901） |
| 邮箱 | 采集脚本在临时库补 `users.email` 并写入示例邮箱（模拟 issue #15 落地后的字段）；真实 `email` 列由 #15 提供 |

Before 与 After 使用**同一份演示数据、同一组视口、同一浏览器**；仓库当前状态里没有真实用户/订单，
截图中所有姓名、邮箱、订单号、tradeNo、设备名都是示例假数据。

## 文件

| 文件 | 说明 |
|---|---|
| `before-login-1280.png` | baseline 登录页（深色） |
| `before-overview-1280.png` | baseline 概览（只有并发/调用，无运营数据） |
| `before-billing-1280.png` | baseline「收费 / 账号」一页塞 7 张卡（套餐/订单/回调/权益/账号/邀请码） |
| `before-overview-768.png` | baseline 768 宽 |
| `before-overview-375.png` | baseline 375 宽 |
| `after-login-1280.png` | 新登录页（与官网一致的浅色/蓝主色） |
| `after-overview-1280.png` | 新概览（收入/订单/活跃用户/到期/异常/最近订单/注册） |
| `after-orders-1280.png` | 订单列表（搜索/筛选/分页/复制订单号/退款入口） |
| `after-order-detail-1280.png` | 订单详情抽屉（用户、权益影响、回调、流水、`sync`/退款） |
| `after-refund-1280.png` | 退款二次确认（展示订单号/金额/用户 + 退款原因） |
| `after-users-1280.png` | 用户列表（邮箱/账号/权益/到期/设备/注册） |
| `after-user-detail-1280.png` | 用户详情抽屉（邮箱、权益、设备、订单、邀请码、冻结/解绑） |
| `after-notifies-1280.png` | 支付回调（验签/conflict/原文展开） |
| `after-overview-768.png` | 新概览 768 宽 |
| `after-orders-375.png` | 新订单列表 375 宽（卡片式，无横向溢出） |
| `after-user-detail-375.png` | 用户详情 375 宽 |

## 自检

- 实机页面（非静态 mockup）在 1280/768/375 截图时检查
  `document.documentElement.scrollWidth <= clientWidth`：**无横向溢出**。
- 采集时监听 `pageerror`：**无 JS 运行时错误**。
- 页面仍不引用外部 CDN / 框架 / 字体；图标内联。
- 旧能力（模型、客户端/发授权、调用、性能、存储）未删；Before 的旧入口在 After 中归入「运行」分组。

## 复现

采集脚本是临时的（不随仓库分发）：启动一个临时配置的 `server.main.create_app`，
用 fake engine + mock payment 起 18900/18901，种子演示数据后用 Playwright 登录截图。
生产验证仍以 `tests/test_admin_console.py`、`tests/test_admin_redesign.py` 与真实部署为准。
