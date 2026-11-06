# Vibe Pay 签约材料 · 站点截图证据（首页 / 商品页 / 支付页 / 下载页）

> **Phase 3 已取代本目录的支付页证据**：`phase3/` 是账号、真实订单驱动支付页与
> Before/After 截图；本目录保留为 issue #7 / #9 的历史截图（支付页仍是当时内部预览版）。
> 当前支付页不再使用静态二维码占位，也不再显示「预览版 / 沙箱 / 尚未接入」。

配套 issue #7（站点）与 issue #9（下载页），**不是** PR #6 的服务端收费链路。
本目录记录首页 `/`、商品页 `/pricing`、支付页 `/pay`、下载页 `/download` 的实际渲染
截图、采集条件与验收状态。**页面代码已交付，不代表下载页或下载文件已经部署到公网。**

> ⚠️ 首页/商品页/支付页的 `after-*.png` 仍是**内部预览版**（页面上有「签约材料预览版」
> 「沙箱示例」「尚未接入」等标注），**不能直接作为 Vibe Pay 签约截图提交**。最终提交版
> 必须按下面「提交前必须做」重做。下载页 `after-download.png` 按 issue #9 要求没有
> 「预览版 / 沙箱 / 尚未接入」字样。

## 采集条件

| 项 | 值 |
|---|---|
| Baseline（Before，下载页加入前） | `origin/integration/billing-vibepay` @ `747d64ca`；首页/页脚还没有「下载」入口，`/download` 是真实 404 |
| Baseline（更早，站点加入前） | `origin/main` @ `1b55158` 时期没有站点；`before-no-site.png` 用空目录静态服务复现真实 404，不是从旧站点渲染的 |
| After | 本分支 `agent/download-page-8a9058788ad4` 工作树（2026-11-05） |
| 桌面视口 | 1440 × 900，deviceScaleFactor 1 |
| 移动视口 | 375 × 812，deviceScaleFactor 2（商品页与下载页各额外截一张） |
| 平板检查 | 768 × 1024，deviceScaleFactor 1（只检查横向溢出，不另存图） |
| 浏览器 | 系统 Google Chrome 156.0.8078.79（headless，Playwright 驱动） |
| 主题 / 语言 / 时区 | 浅色；`zh-CN`；`Asia/Shanghai`；`reduced_motion=reduce` |
| 地址 | 本地干净 URL：`/`、`/pricing`、`/pay?plan=monthly`、`/download`，端口由脚本随机分配（见 `capture.json`） |
| 数据 | 站内静态 `site/assets/plans.json`；没有真实用户、订单或支付数据 |
| 采集脚本 | `scripts/capture-vibepay-evidence.py` |

## 文件

| 文件 | 说明 |
|---|---|
| `before-no-site.png` | 更早的 Baseline：base revision 没有站点，空目录静态服务的真实 404 页面。**不是**用另一个页面冒充旧 UI。 |
| `before-home.png` | 下载页 Baseline：`integration/billing-vibepay` 的首页，导航/页脚还没有「下载」入口。 |
| `before-download-404.png` | 下载页 Baseline：同一 revision 请求 `/download` 的真实 404。 |
| `after-home.png` | After：首页 `/`，全页（1440 宽），导航与页脚已加入「下载」。 |
| `after-pricing.png` | After：商品页 `/pricing`，全页（1440 宽）。 |
| `after-pay.png` | After：支付页 `/pay?plan=monthly`，全页（1440 宽），停在“等待支付”的沙箱示例状态。 |
| `after-download.png` | After：下载页 `/download`，全页（1440 宽），Windows / macOS / Android 三张卡片。 |
| `after-pricing-mobile.png` | After：商品页 375 宽移动端，全页（750 px 物理宽 @2x）。 |
| `after-download-mobile.png` | After：下载页 375 宽移动端，全页（750 px 物理宽 @2x）。 |
| `capture.json` | 采集清单：时间、分支/base、浏览器、视口、每个文件的 SHA-256 |

## 页面与截图对应

- **首页**：产品名/logo、一句话介绍、语音指令 / 会议转写 / 说话人分离 / TTS /
  能力后端套餐 / 隐私边界，以及查看套餐入口。
- **商品页**：月卡 ¥10 / 30 天，每日 ASR 120 分钟、TTS 5 万字符、1 QPS、
  3 台设备、并发 1；“立即购买”进入支付页；7 天试用标注为邀请制，不在本页下单。
- **支付页**：订单摘要（套餐、金额、有效期、示例订单号）+ 支付宝付款方式 +
  沙箱二维码占位 + “等待支付”状态；页面不自动跳转、不判定支付结果。
- **下载页**：Windows 10/11 x64、macOS（Intel / Apple Silicon 通用）与 Android（debug 内测包）
  三张下载卡，展示版本 `0.9.0-beta`、文件大小、安装包构建日期；安装步骤、系统要求、
  内测说明；每个安装包带可折叠的 SHA-256 校验值，并提供 GitHub Release 备用入口。

## 验收状态（issue #7）

| 验收项 | 状态 | 说明 |
|---|---|---|
| 三个 URL 公网 HTTPS 200（附 `curl`） | ⏳ 未完成 | 2026-11-05 实测 `/`、`/pricing`、`/pay?plan=monthly` 均返回 200；下载页 `/download` 仍 404。本次交付不自行部署，也不声称已上线 |
| 首页 / 商品页 / 支付页各 1 张截图 | ✅ 内容截图已入库 | 见上表；**带地址栏**的版本仍待公网 HTTPS 补采；当前是预览版，见「提交前必须做」 |
| 商品页价格/额度与后端 `plans` 一致 | ✅ | 月卡 ¥10 / 30 天 / 120 分钟 / 5 万字符 / 1 QPS / 3 设备 / 并发 1；数据源 `site/assets/plans.json` |
| 支付页处于“展示支付宝付款方式、等待支付”状态 | ✅（沙箱预览） | 明确标注“沙箱示例 · 不会产生真实扣款”；不是支付成功页。最终提交需真实沙箱收银台并去掉沙箱标注 |
| 移动端宽度可用 | ✅ | 脚本检查 375 / 768 / 1440 无横向溢出；375 下 `h1.left = 18`（2026-11-04 修复 `.page-hero` 的左右 padding 被简写清零） |
| 截图 + 采集说明入库 `docs/vibepay-evidence/` | ✅ | 本文件 + 图片 + `capture.json` |
| 没有把沙箱/占位页面表述成“生产可用” | ✅ | 页面与文档均写明预览/沙箱/未接真实收银台 |

## 验收状态（issue #9 下载页）

| 验收项 | 状态 | 说明 |
|---|---|---|
| `https://echoserver.zhkq.online:8443/download` 返回 200 | ⏳ 未完成 | 页面代码与截图已入库；部署与证书需另走发布流程。2026-11-05 实测 `/download` 仍是 404 |
| 三个平台下载链接可下、大小与页面一致 | ⏳ 待托管 | 页面指向站内 `/downloads/`，三个文件需按确认后的文件名放上去（由 Lead 侧上传）；GitHub Release 备用链接已给出。文件大小与 SHA-256 已核对 |
| 移动端 375px 布局正常 | ✅ | `after-download-mobile.png`；脚本检查无横向溢出、`h1.left >= 17px` |
| 导航/页脚在所有页面都有「下载」入口 | ✅ | 首页、套餐页、支付页、下载页各一个导航入口和一个页脚入口；脚本逐页断言 |
| 文件带 SHA-256，页面可核对 | ✅ | 三个安装包的 SHA-256 以可折叠 `<details>` 展示，值与实际文件一致 |
| 正式版页面无预览/沙箱字样 | ✅ | 下载页不含「预览版 / 沙箱 / 尚未接入」；首页/支付页仍保留 issue #7 的预览标注 |

## 下载资源（issue #9 三项已确认）

owner 已在 issue #9 的评论里确认：

1. **Android APK 要放**：`android/app/build/outputs/apk/debug/app-debug.apk`（debug 1.2，
   versionCode 3），发布名 `ECHO-Android-0.9.0-beta-debug.apk`；页面注明「内测 / debug 包 /
   需允许安装未知来源，正式上架前再换 release 签名版」。
2. **文件托管到服务器**：统一放 `https://echoserver.zhkq.online:8443/downloads/`，页面用
   相对链接 `/downloads/...`；GitHub Release 只作备用入口。上传由 Lead 侧执行。
3. **版本号先用 `0.9.0-beta`**：页面顶部展示「内测版 0.9.0-beta」；Windows/macOS 标注
   基于 ECHO 3.0.1 构建，Android 标注 debug 1.2。等收费功能全部上线后统一改 `1.0.0`。

页面链接的三个文件（放到 `site/downloads/` 或部署根目录的 `/downloads/`）：

| 平台 | 文件名 | 大小（字节） | SHA-256 |
|---|---|---|---|
| Windows | `ECHO-Windows-0.9.0-beta.zip` | 74,895,328（74.9 MB / 71.4 MiB） | `CC6B5AE5850EAB786C1C98248DC76BAEBFB3E5BD4FB67B84218B5EF2B71FFAE8` |
| macOS | `ECHO-macOS-0.9.0-beta.zip` | 8,784,030（8.8 MB / 8.4 MiB） | `76BCD9690E9AAF1AB8FBB5147C62A4DB512605A725D1C4D95EB1BC1F11DD2B51` |
| Android | `ECHO-Android-0.9.0-beta-debug.apk` | 22,346,187（22.3 MB） | `5ADD81EBE1B86CD5D46968E36AFBB25C5FC2B61A38133C9BA7975202B7CE0C15` |

Windows 与 macOS 文件分别取自 GitHub Release `v3.0.1` 的
`ECHO-delivery-20261104-0018.zip` / `ECHO-delivery-macos-20261104-0017.zip`（只是改名，
字节不变）；Android 文件来自 `v3.0.1` 交付包内同一份 APK。三个 SHA-256 都已实测核对。
页面/README 的版本、日期、文件名、大小与 SHA-256 由 `site/assets/downloads.json` 驱动：
发版时跑 `python scripts/update-download-page.py --write`；部署前后用
`scripts/verify-downloads.ps1 -Dir <目录> -Full` 或 `-BaseUrl <地址>` 核对实际文件。

## 还没做的（必须等外部条件）

1. **公网 HTTPS 200 与 `curl` 输出**：下载页 `/download` 与 `/downloads/*` 要等下一轮
   部署（或发布流程）落地；本次交付不自行改线上静态目录。
2. **带地址栏的截图**：支付宝要求截图上能看到最终 HTTPS 地址。当前脚本只截页面内容；
   公网就绪后，用真实浏览器窗口对 `https://<最终域名>/`、`/pricing`、`/pay?plan=monthly`、
   `/download` 补采（Windows 可用 `Win+Shift+S` 截窗口，macOS 可用 `Cmd+Shift+4`），不要用
   `127.0.0.1` 的本地截图冒充公网证据。脚本也支持 `--base-url https://<最终域名>` 对真实
   地址重采内容图，但地址栏仍需人工补。
3. **真实支付宝收银台**：Phase 3 接 `alipay.trade.page.pay` 后再替换支付页的
   沙箱二维码/按钮，并补 `return_url`、`notify_url` 与补偿查询。
4. **备案信息**：如平台要求，另行准备；页面暂未放 ICP 号。

## 提交前必须做（Vibe Pay 签约）

**首页/商品页/支付页的 `after-*.png` 是内部预览版，不能直接作为签约截图提交。**
提交前逐条完成：

1. **换真实沙箱收银台**：Phase 3 接真实支付宝沙箱收银台后，用真实 `payUrl` /
   `qrCode` 重新截支付页（不是现在的占位二维码）。
2. **去掉预览标注**：最终提交截图里去掉「签约材料预览版」「尚未接入」「示例」这类标注；
   支付页保留真实的「等待支付」状态，而不是支付成功页。
3. **补带地址栏截图**：公网 HTTPS 就绪后，对**最终域名**用真实浏览器窗口补 4 张
   **带地址栏**截图（脚本不伪造地址栏），见上一节第 2 条。
4. **不声称已上线**：本地预览版与提交版分开保存；README / PR 不写「已上线」。
5. **再对官方定义**：支付页 = 展示支付宝付款方式、等待用户去支付的页面（不是支付成功页）。

## 已拍板的默认值（2026-11-04，issue #7 评论）

| 项 | 已拍板默认值 |
|---|---|
| APP 名称 | `ECHO 语音助手` |
| 最终域名 | 先按 `echoserver.zhkq.online` 设计 |
| 支付页 | 先用沙箱二维码占位，明确标注「测试 / 沙箱」 |
| 语言 | 先中文 |
| 签约主体 | 个人创作者 |
| 部署 / 截图 | 公网 HTTPS 就绪后再正式部署和截图，不声称已上线 |

这些是 owner 的决定（选 A：按默认值先开工）。本次交付按默认值实现，没有把
默认值写成“最终不可改”的结论；后续改任何一项，先在 issue #7 / 本 PR 评论说明。

## 复采

```bash
# 页面随时可本地重采（After + 两种 Before）
python scripts/capture-vibepay-evidence.py

# 公网 HTTPS 就绪后，对真实地址重采内容图（不伪造地址栏）
python scripts/capture-vibepay-evidence.py --base-url https://echoserver.zhkq.online
```

脚本会断言：四个页面的关键文案存在、下载页两个 SHA-256 与下载链接存在、每页导航/页脚
各有一个「下载」入口、支付页没有跳转、375/768/1440 无横向溢出，以及 375 宽下
`h1.left >= 17px`（防止 `.page-hero` 的左右 padding 被简写覆盖成 0），并把结果写进
`capture.json`。截图由真实浏览器渲染，不是拼图或 mockup。
