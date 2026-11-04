# ECHO 签约用站点（首页 / 商品页 / 支付页）

这是 Vibe Pay 产品签约材料用的**独立静态站**，同时作为后续网页购买入口的基础。
它**不是已上线的收款系统**：本目录只包含页面与本地采集脚本，公网入口、证书、
真实支付宝收银台都还没接。

## 目录

| 路径 | 内容 |
|---|---|
| `index.html` | 首页 `/`：产品名/logo、一句话介绍、功能点、隐私边界、套餐入口 |
| `pricing.html` | 商品页 `/pricing`：月卡 ¥10 / 30 天与 7 天试用；「立即购买」→ `/pay?plan=monthly` |
| `pay.html` | 支付页 `/pay`：订单摘要 + 支付宝付款方式 + 沙箱二维码占位，停在「等待支付」 |
| `assets/plans.json` | 页面套餐的唯一来源；与能力后端 `server/billing.py` 的 `DEFAULT_PLANS` 对齐 |
| `assets/site.css` | 全站样式（浅色、桌面 + 移动自适应，无第三方字体/CDN） |
| `assets/site.js` | 套餐/订单渲染；支付页只展示等待支付，不做结果判定、不自动跳转 |
| `assets/echo-logo.png` | ECHO 现有图标（从 `web/icon-192.png` 复制，未改品牌） |

`/`、`/pricing`、`/pay` 是要求的干净 URL。静态托管需要把 `/pricing` 映射到
`pricing.html`、`/pay` 映射到 `pay.html`；本地预览和截图脚本已经这样做。

## 本地预览

```bash
python scripts/capture-vibepay-evidence.py --serve --port 8090
# http://127.0.0.1:8090/  /pricing  /pay?plan=monthly
```

截图（After）与基线（Before：base revision 没有站点时的 404）：

```bash
python scripts/capture-vibepay-evidence.py
# 产物在 docs/vibepay-evidence/
```

脚本会启动本地静态服务、用系统 Chrome 渲染、检查关键文案与 375/768/1440
宽度下无横向溢出，并把 URL、视口、浏览器版本、文件 sha256 写进
`docs/vibepay-evidence/capture.json`。依赖：Python + `playwright` + 本机 Chrome；
不需要下载 Playwright 自带浏览器。

## 套餐数据

`assets/plans.json` 当前是静态数据，字段与后端 `/v1/billing/plans` 的 camelCase
视图一致（金额单位是 **分**）。Phase 3 接真实收银台时，把 `site.js` 里的
`PLANS_URL` 改成同源 `"/v1/billing/plans"` 即可，不用改页面结构。

月卡与后端 `DEFAULT_PLANS` 对齐：`priceCents=1000`、`days=30`、
`dailyAudioMinutes=120`、`ttsChars=50000`、`qps=1`、`maxDevices=3`、
`maxConcurrent=1`。7 天试用同额度、`priceCents=0`，只在页面提示为邀请制，
不在本页下单。

## 部署（Phase 2 公网入口就绪后）

**现在没有部署，也不要在材料里写“已上线”。** 以下是部署时的约束，供 Phase 2
确定域名/端口/反代方案后使用：

1. 静态站点放独立目录（例如 `/srv/echo-site`），只读；不要把 `site/` 混进
   能力后端的数据目录。
2. HTTPS 必须是公信 CA 证书（支付宝不认自签）；从公网 `curl -v` 验证 200 且证书链通过。
3. `/v1/*` 仍走能力后端；静态站只占 `/`、`/pricing`、`/pay`、`/assets/*`。
4. 不要给静态站加登录墙、验证码、自动播放或第三方字体/图片外链；截图工具要能稳定加载。
5. 支付页在 Phase 3 前保持沙箱标注；接真实收银台后替换为支付宝返回的
   `payUrl` / `qrCode`，并补 `return_url` / `notify_url` 联调。

一个最小 Nginx 形状（域名/端口以 Phase 2 为准，不要直接照抄上线）：

```nginx
server {
    listen 443 ssl;
    server_name echoserver.zhkq.online;
    root /srv/echo-site;
    index index.html;

    location /v1/ {
        proxy_pass http://127.0.0.1:8900;
        # 保留现有后端反代配置；本文件不改变 /v1/* 语义
    }
    location / {
        try_files $uri $uri.html =404;
    }
}
```

## 已拍板的默认值（2026-11-04）

owner 已在 issue #7 的评论里选定「按默认值先开工」（A）。本次交付按这张表实现；
后续都可以改，若要改某一项，先在 issue #7 / PR 评论说明再改。

| 项 | 已拍板默认值 |
|---|---|
| APP 名称 | `ECHO 语音助手` |
| 最终域名 | 先按 `echoserver.zhkq.online` 设计；页面结构与文案不依赖域名 |
| 支付页 | 先用沙箱二维码占位，页面明确标注「测试 / 沙箱」，不冒充生产收银台 |
| 语言 | 先中文（Vibe Pay 审核中文即可） |
| 签约主体 | 个人创作者（Skill 只支持个人；企业主体走 b.alipay.com，另行处理） |
| 部署 / 截图 | 先做页面 + 截图脚本；公网 HTTPS 就绪后再正式部署和截图，**不得声称已上线** |

页面里的名称/域名/支付标注都按上表落地，不是本次交付自行拍板的最终结论。

## 不做的部分（明确留给后续）

- 不在本次交付接真实支付宝 `query/refund/notify`；支付页不判定支付结果。
- 不在本次交付做登录、支付、权益的真实闭环（M2 客户端）。
- 不在本次交付做 MCC 类目选择、开放平台签约动作（由用户本人扫码/确认）。
- 不在本次交付做域名备案；如平台要求，另行准备。
- 不在本次交付声称公网 HTTPS 200 或“已上线”。验收里的 `curl` 输出与带地址栏截图
  必须等 Phase 2 公网入口与证书就绪后补。
