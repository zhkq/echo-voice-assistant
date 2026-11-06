# -*- coding: utf-8 -*-
"""支付网关抽象 + **Mock 实现** + 真实支付宝骨架（设计 §4.2 / §4.6 / §6）。

## 为什么要有抽象层

`billing.py` 只认这四个动作：

    create_pay(order) -> {payUrl, qrCode?}
    verify_notify(form) -> {tradeNo, outTradeNo, amountCents, status, buyerId?}
    query(order)
    refund(order)

Mock 与真实支付宝**都产出同形的 notify**，所以 T3/T5 那条链路（下单 → 回调 →
`paid` → 发码 → 权益）不用为"用哪个网关"改一行。切网关只改
`payment.provider`，不碰订单/权益代码。

## Mock 的边界（任务书 T4 / 负面清单第 5、6 条）

* 只有 `payment.provider=mock` 时才把 `/v1/billing/mock/*` 注册进 app
  （`mock_router` 由 `main.create_app` 条件 include）。`provider=alipay` 下
  **这些端点不存在**，不是"存在但拒绝"。
* Mock 的支付结果**必须显式触发**（调用模拟支付端点），服务端不会自己把订单标成已付。
* 响应里带 `"mock": true` —— 一眼能看出这次不是真钱。
* 未配置支付（`provider=disabled` 或 alipay 缺密钥）→ `503 payment_unconfigured`，
  **绝不静默降级成 Mock**。

## 真实支付宝的边界

`create_pay` / `verify_notify` / `query` / `refund` 都按支付宝网页收款的 RSA2 规则
实现。**请求签名包含 `sign_type`，异步通知验签排除 `sign_type`**（2026-11-05 沙箱
实测口径）；同步响应（query/refund）用支付宝公钥校验响应节点的原始 JSON 串。
私钥只从**服务端文件路径**读，不进仓库、不进日志。

`return_url` 由服务端配置成站点支付页，下单时自动带上 `?order=<orderId>`，
支付宝回跳后支付页据此查询/补记订单。`notify_url` 只从配置读，公网部署后填。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen

from fastapi import APIRouter, Request

from server import errors

#: 支付宝交易状态里表示"已收到钱"的那两个（设计 §4.6 的白名单）。
PAID_TRADE_STATUSES = ("TRADE_SUCCESS", "TRADE_FINISHED")

#: 支付宝生产/沙箱网关（`alipay.sandbox=true` 且没显式改 gateway 时自动切）。
ALIPAY_PROD_GATEWAY = "https://openapi.alipay.com/gateway.do"
ALIPAY_SANDBOX_GATEWAY = "https://openapi-sandbox.dl.alipaydev.com/gateway.do"

#: Mock 端点允许的本机/测试来源。TestClient 的 `client.host` 是 `testclient`；
#: 真实服务是回环地址。远程来源默认拒绝，除非显式 `payment.mock_allow_remote`。
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient", "testserver"}


def _canonical(form: Dict[str, Any], *, include_sign_type: bool = False) -> str:
    """待签名串：按 key 字典序，`k=v` 用 `&` 连接。

    * 请求签名（下单/查询）：只排除 `sign`，**包含 sign_type**（支付宝 RSA2 实测）。
    * 异步通知验签：排除 `sign` 和 `sign_type`（支付宝官方 SDK 口径）。
    """
    excluded = {"sign"} if include_sign_type else {"sign", "sign_type"}
    items = [(str(k), str(v)) for k, v in (form or {}).items()
             if k not in excluded and v is not None and str(v) != ""]
    return "&".join("%s=%s" % (k, v) for k, v in sorted(items))


def _amount_cents(value: Any) -> int:
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        raise errors.bad_request("支付金额格式不对")


def _normalize(form: Dict[str, Any], *, verified: bool) -> Dict[str, Any]:
    """网关字段 → `billing.apply_notify()` 的输入（两种网关共用）。"""
    status_raw = str((form or {}).get("trade_status") or "").upper()
    if status_raw in PAID_TRADE_STATUSES:
        status = "paid"
    elif status_raw in ("TRADE_CLOSED", "TRADE_FAILED"):
        status = "closed"
    elif status_raw in ("WAIT_BUYER_PAY", ""):
        status = "pending"
    else:
        status = status_raw.lower()
    return {
        "tradeNo": str((form or {}).get("trade_no") or ""),
        "outTradeNo": str((form or {}).get("out_trade_no") or ""),
        "amountCents": _amount_cents((form or {}).get("total_amount")),
        "status": status,
        "buyerId": str((form or {}).get("buyer_open_id")
                       or (form or {}).get("buyer_user_id") or ""),
        "verified": bool(verified),
        "rawStatus": status_raw,
    }


class DisabledGateway:
    """没配支付。**所有动作都明确报 `payment_unconfigured`**。"""

    name = "disabled"
    configured = False

    def create_pay(self, order: Dict[str, Any]) -> Dict[str, Any]:
        raise errors.payment_unconfigured()

    def verify_notify(self, form: Dict[str, Any]) -> Dict[str, Any]:
        raise errors.payment_unconfigured()

    def query(self, order: Dict[str, Any]) -> Dict[str, Any]:
        raise errors.payment_unconfigured()

    def refund(self, order: Dict[str, Any]) -> Dict[str, Any]:
        raise errors.payment_unconfigured()


class MockGateway:
    """Mock 网关：能产出**与真网关同形**的 notify（`build_form` → `verify_notify`）。

    `build_form()` 是给 `mock_router` 用的；它签出来的表单与支付宝回调的字段名
    一致，所以 `verify_notify()` 与真实网关走的是同一条归一化路径。
    """

    name = "mock"
    configured = True

    def __init__(self, secret: str = ""):
        # **没配就随机生成**（每次启动不同），不把仓库里的默认值当生产密钥。
        # Mock 的签名只在同一进程内 build_form → verify_notify，所以随机完全够用。
        self.secret = str(secret or secrets.token_hex(16))
        self.generated_secret = not bool(secret)

    def _sign(self, form: Dict[str, Any]) -> str:
        return hmac.new(self.secret.encode("utf-8"),
                        _canonical(form, include_sign_type=True).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def build_form(self, order: Dict[str, Any], trade_no: str = "",
                   trade_status: str = "TRADE_SUCCESS",
                   amount_cents: Optional[int] = None,
                   buyer_id: str = "") -> Dict[str, str]:
        cents = int(order.get("amount_cents") if amount_cents is None else amount_cents)
        form = {
            "notify_id": "mock-" + secrets.token_hex(8),
            "notify_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "out_trade_no": str(order.get("order_id") or ""),
            "trade_no": str(trade_no or ("mock-" + secrets.token_hex(10))),
            "trade_status": str(trade_status),
            "total_amount": "%.2f" % (max(0, cents) / 100.0),
            "buyer_open_id": str(buyer_id or "mock-buyer"),
            "sign_type": "HMAC-SHA256",
        }
        form["sign"] = self._sign(form)
        return form

    def create_pay(self, order: Dict[str, Any]) -> Dict[str, Any]:
        order_id = str(order.get("order_id") or "")
        return {"payUrl": "mock://pay/%s" % order_id,
                "qrCode": "mock://pay/%s" % order_id,
                "mock": True}

    def verify_notify(self, form: Dict[str, Any]) -> Dict[str, Any]:
        got = dict(form or {})
        sign = str(got.pop("sign", "") or "")
        if not sign or not hmac.compare_digest(sign, self._sign(got)):
            raise errors.bad_request("Mock 支付回调签名不对")
        return _normalize(got, verified=True)

    def query(self, order: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": str(order.get("status") or ""), "mock": True}

    def refund(self, order: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "mock": True,
                "tradeNo": str(order.get("alipay_trade_no") or "")}


class AlipayGateway:
    """真实支付宝（网页收款 `alipay.trade.page.pay` + query/refund）。

    * `create_pay`：生成带 RSA2 签名的收银台 URL（可选 `return_url` / `notify_url`）；
    * `verify_notify`：异步通知验签（排除 `sign_type`，与支付宝 SDK 口径一致）；
    * `query` / `refund`：同步接口，**验签响应**并把结果归一化成与回调同形的 dict。
    """

    name = "alipay"

    def __init__(self, cfg):
        self.app_id = str(cfg.get("alipay.app_id", "") or "")
        self.seller_id = str(cfg.get("alipay.seller_id", "") or "")
        self.private_key_path = str(cfg.get("alipay.private_key_path", "") or "")
        self.public_key_path = str(cfg.get("alipay.public_key_path", "") or "")
        self.sandbox = bool(cfg.get("alipay.sandbox", False))
        configured = str(cfg.get("alipay.gateway", "") or "")
        if self.sandbox and configured in ("", ALIPAY_PROD_GATEWAY):
            configured = str(cfg.get("alipay.sandbox_gateway", "") or
                             ALIPAY_SANDBOX_GATEWAY)
        self.gateway = configured or ALIPAY_PROD_GATEWAY
        self.notify_url = str(cfg.get("payment.notify_url", "")
                              or cfg.get("alipay.notify_url", "") or "")
        #: 支付完成后回跳的站点页面（例如 `https://…/pay`）。下单时自动追加
        #: `?order=<orderId>`，支付页据此刷新订单；空 = 不回跳（支付宝收银台
        #: 只显示"返回商家"的默认页）。
        self.return_url = str(cfg.get("alipay.return_url", "")
                              or cfg.get("payment.return_url", "") or "")
        try:
            self.timeout_s = max(3.0, float(cfg.get("alipay.timeout_s", 20) or 20))
        except (TypeError, ValueError):
            self.timeout_s = 20.0

    @property
    def configured(self) -> bool:
        import os
        return bool(self.app_id and self.private_key_path and self.public_key_path
                    and os.path.isfile(self.private_key_path)
                    and os.path.isfile(self.public_key_path))

    # ---- RSA2 ------------------------------------------------------------

    def _private_key(self):
        try:
            from cryptography.hazmat.primitives import serialization
        except ImportError:
            raise errors.payment_unconfigured()
        with open(self.private_key_path, "rb") as fh:
            return serialization.load_pem_private_key(fh.read(), password=None)

    def _public_key(self):
        try:
            from cryptography.hazmat.primitives import serialization
        except ImportError:
            raise errors.payment_unconfigured()
        with open(self.public_key_path, "rb") as fh:
            return serialization.load_pem_public_key(fh.read())

    def _sign(self, form: Dict[str, Any]) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        signature = self._private_key().sign(
            _canonical(form, include_sign_type=True).encode("utf-8"),
            padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode("ascii")

    def _verify(self, form: Dict[str, Any], sign: str) -> bool:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        try:
            self._public_key().verify(
                base64.b64decode(sign), _canonical(form).encode("utf-8"),
                padding.PKCS1v15(), hashes.SHA256())
            return True
        except Exception:
            return False

    def _verify_raw(self, data: bytes, sign: str) -> bool:
        """校验支付宝同步响应：签名对象是响应节点的**原始 JSON 子串**。"""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        try:
            self._public_key().verify(
                base64.b64decode(sign), data,
                padding.PKCS1v15(), hashes.SHA256())
            return True
        except Exception:
            return False

    def _request(self, method: str, biz: Dict[str, Any]) -> Dict[str, Any]:
        """调一个支付宝同步接口（query / refund），并**验签响应节点**。

        请求：表单 POST 到 `self.gateway`，RSA2 签名（与 `create_pay` 同一口径）；
        响应：支付宝返回 `{"<method>_response":{…},"sign":"…"}`，签名对象是
        响应节点的**原始 JSON 子串**（不能只拿 dict 重新序列化，键序/转义会变）。
        """
        if not self.configured:
            raise errors.payment_unconfigured()
        params = {
            "app_id": self.app_id,
            "method": method,
            "format": "JSON",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0",
            "biz_content": json.dumps(biz, ensure_ascii=False,
                                      separators=(",", ":")),
        }
        params["sign"] = self._sign(params)
        body = urlencode(params).encode("utf-8")
        request = UrlRequest(self.gateway, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Accept": "application/json",
        })
        try:
            with urlopen(request, timeout=self.timeout_s) as resp:
                raw = resp.read()
                charset = (resp.headers.get_content_charset() or "").lower()
        except Exception as exc:                                # 网络 / HTTP 层
            raise errors.payment_gateway_error("支付宝网关请求失败：%s" % exc)

        # 支付宝同步响应可能是 UTF-8 或 GBK（`charset` 由请求带过去，但
        # 网关偶尔不声明 Content-Type 的 charset）；按顺序试，失败才报错。
        text = ""
        used_charset = ""
        for encoding in ([charset] if charset else []) + ["utf-8", "gbk"]:
            if not encoding:
                continue
            try:
                text = raw.decode(encoding)
                used_charset = encoding
                break
            except (LookupError, UnicodeDecodeError):
                continue
        if not text:
            raise errors.payment_gateway_error("支付宝同步响应无法解码")

        try:
            outer = json.loads(text)
        except Exception:
            raise errors.payment_gateway_error("支付宝同步响应不是合法 JSON")
        if not isinstance(outer, dict):
            raise errors.payment_gateway_error("支付宝同步响应结构异常")
        key = '"%s_response"' % method.replace(".", "_")
        idx = text.find(key)
        if idx < 0:
            raise errors.payment_gateway_error("支付宝同步响应缺少 %s" % key)
        colon = text.find(":", idx + len(key))
        start = text.find("{", max(colon, idx + len(key)))
        if start < 0:
            raise errors.payment_gateway_error("支付宝同步响应结构异常")
        try:
            node, end = json.JSONDecoder().raw_decode(text[start:])
        except Exception:
            raise errors.payment_gateway_error("支付宝同步响应结构异常")
        if not isinstance(node, dict):
            raise errors.payment_gateway_error("支付宝同步响应结构异常")
        raw_node = text[start:start + end].encode(used_charset, "replace")
        sign = str(outer.get("sign") or "")
        if not sign or not self._verify_raw(raw_node, sign):
            raise errors.payment_gateway_error("支付宝同步响应验签失败")
        return node

    @staticmethod
    def _node_error(node: Dict[str, Any]) -> str:
        return str(node.get("sub_msg") or node.get("msg")
                   or node.get("sub_code") or node.get("code") or "未知错误")

    # ---- 网关接口 --------------------------------------------------------

    def create_pay(self, order: Dict[str, Any]) -> Dict[str, Any]:
        if not self.configured:
            raise errors.payment_unconfigured()
        # 支付宝侧也按订单的 `expires_at` 超时，避免用户在订单关闭后从收银台付款；
        # 真正的兜底仍是回调冲突留痕 + 人工对账（README 8.9）。
        minutes = 15
        try:
            left = float(order.get("expires_at") or 0) - time.time()
            if left > 0:
                minutes = max(1, int(left / 60) + (1 if left % 60 else 0))
        except (TypeError, ValueError):
            pass
        biz = {
            "out_trade_no": str(order.get("order_id") or ""),
            "total_amount": "%.2f" % (int(order.get("amount_cents") or 0) / 100.0),
            "subject": "ECHO 后端套餐",
            "product_code": "FAST_INSTANT_TRADE_PAY",
            "timeout_express": "%dm" % minutes,
        }
        params = {
            "app_id": self.app_id,
            "method": "alipay.trade.page.pay",
            "format": "JSON",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0",
            "biz_content": json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
        }
        if self.notify_url:
            params["notify_url"] = self.notify_url
        if self.return_url:
            # 回跳地址里带订单号，支付页才能只凭 URL 找回订单（支付宝也会再追加
            # 它自己的参数；已有 `?` 时用 `&`）。
            sep = "&" if "?" in self.return_url else "?"
            params["return_url"] = "%s%s%s" % (
                self.return_url, sep,
                urlencode({"order": str(order.get("order_id") or "")}))
        params["sign"] = self._sign(params)
        return {"payUrl": "%s?%s" % (self.gateway, urlencode(params)),
                "mock": False}

    def verify_notify(self, form: Dict[str, Any]) -> Dict[str, Any]:
        if not self.configured:
            raise errors.payment_unconfigured()
        got = dict(form or {})
        sign = str(got.pop("sign", "") or "")
        if not sign or not self._verify(got, sign):
            raise errors.bad_request("支付宝回调验签失败")
        # **app_id 必须存在且相等**（不是"有才比"）：缺了就是来源不明。
        if str(got.get("app_id") or "") != self.app_id:
            raise errors.bad_request("支付宝回调 app_id 缺失或不匹配")
        if self.seller_id and str(got.get("seller_id") or "") != self.seller_id:
            raise errors.bad_request("支付宝回调 seller_id 不匹配")
        return _normalize(got, verified=True)

    def query(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """主动查询（`alipay.trade.query`）。用于本地联调/回调丢失时的兜底。

        返回与异步通知同形的归一化结果（`status/paid/tradeNo/amountCents/…`）；
        **只把"交易不存在"当作未支付**，其余网关业务错误一律 502 —— 把系统错误
        当成 pending 会让支付页永远等下去，也掩盖了密钥/验签配置问题。
        """
        order_id = str(order.get("order_id") or "")
        node = self._request("alipay.trade.query", {"out_trade_no": order_id})
        code = str(node.get("code") or "")
        if code != "10000":
            if str(node.get("sub_code") or "") == "ACQ.TRADE_NOT_EXIST":
                return {"status": "pending", "tradeNo": "",
                        "outTradeNo": order_id, "amountCents": 0,
                        "verified": True, "rawStatus": "TRADE_NOT_EXIST"}
            raise errors.payment_gateway_error(
                "支付宝查询失败：%s" % self._node_error(node))
        got_order = str(node.get("out_trade_no") or "")
        if got_order and got_order != order_id:
            raise errors.payment_gateway_error(
                "支付宝查询返回的订单号不匹配：%s" % got_order)
        trade_status = str(node.get("trade_status") or "").upper()
        try:
            return _normalize({
                "trade_no": node.get("trade_no"),
                "out_trade_no": order_id,
                "total_amount": node.get("total_amount"),
                "trade_status": trade_status,
                "buyer_open_id": (node.get("buyer_open_id")
                                  or node.get("buyer_user_id")),
            }, verified=True)
        except errors.EchoError as exc:
            raise errors.payment_gateway_error(
                "支付宝查询响应字段不合法：%s" % (exc.detail or exc.message))

    def refund(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """全额退款（`alipay.trade.refund`），幂等键固定为这张订单。

        `out_request_no` 是支付宝的退款幂等键；同一订单重复调用不会重复退钱。
        `fund_change="N"` 表示这笔退款之前已经成功过（重试），仍按成功处理。
        """
        order_id = str(order.get("order_id") or "")
        node = self._request("alipay.trade.refund", {
            "out_trade_no": order_id,
            "refund_amount": "%.2f" % (int(order.get("amount_cents") or 0) / 100.0),
            "out_request_no": ("refund-" + order_id)[:64],
            "refund_reason": "ECHO 后端套餐退款",
        })
        if str(node.get("code") or "") != "10000":
            raise errors.payment_gateway_error(
                "支付宝退款失败：%s" % self._node_error(node))
        got_order = str(node.get("out_trade_no") or "")
        if got_order and got_order != order_id:
            raise errors.payment_gateway_error(
                "支付宝退款返回的订单号不匹配：%s" % got_order)
        return {"ok": True,
                "tradeNo": str(node.get("trade_no") or
                               order.get("alipay_trade_no") or ""),
                "refundFee": str(node.get("refund_fee") or ""),
                "fundChange": str(node.get("fund_change") or ""),
                "mock": False}


def build_gateway(cfg) -> Any:
    """按 `payment.provider` 造网关。**认不出来就 disabled，不猜**。"""
    provider = str(cfg.get("payment.provider", "disabled") or "disabled").strip().lower()
    if provider == "mock":
        return MockGateway(str(cfg.get("payment.mock_secret", "") or ""))
    if provider == "alipay":
        return AlipayGateway(cfg)
    return DisabledGateway()


# ---------------------------------------------------------------- Mock 端点
#
# **只有在 `payment.provider=mock` 时** `main.create_app` 才 include 这个 router。
# 生产（alipay）下这些路径根本不存在 —— 不是"存在但返回拒绝"。

mock_router = APIRouter(prefix="/v1/billing/mock")


def _svc(request: Request):
    from server import billing as billing_mod
    svc = getattr(request.app.state.echo, "billing_svc", None)
    if svc is None:
        raise errors.billing_disabled()
    return svc


def _check_mock_source(request: Request) -> None:
    """Mock 端点默认只允许本机/测试来源（生产用 alipay，不会注册这些路由）。

    这是防"provider 误配成 mock 后公网谁都能模拟付款"的第二道闸；显式
    `payment.mock_allow_remote=true` 可放开（只建议在隔离环境）。
    """
    cfg = request.app.state.echo.cfg
    if bool(cfg.get("payment.mock_allow_remote", False)):
        return
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "")
    if host not in _LOOPBACK_HOSTS:
        raise errors.forbidden("Mock 支付端点只允许本机/测试来源")


@mock_router.post("/pay/{order_id}")
async def mock_pay(request: Request, order_id: str):
    """**模拟付款**：构造一张同形 notify → 走与真回调完全相同的校验/入账路径。

    响应里 `"mock": true`。这个端点只存在于 `payment.provider=mock`。
    """
    _check_mock_source(request)
    svc = _svc(request)
    if not svc.enabled or not isinstance(svc.gateway, MockGateway):
        raise errors.payment_unconfigured()
    order = svc.store.order(str(order_id or ""))
    if order is None:
        raise errors.order_not_found(order_id)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    form = svc.gateway.build_form(order, trade_no=str(payload.get("tradeNo") or ""),
                                  buyer_id=str(payload.get("buyerId") or ""))
    out = svc.apply_notify(svc.gateway.verify_notify(form))
    out.update({"mock": True, "tradeNo": form["trade_no"], "orderId": str(order_id)})
    return out


@mock_router.post("/notify")
async def mock_notify(request: Request):
    """可编程回调（测试/联调用）：可以造**金额不对 / 失败状态**的 notify。

    `amountCents` 不传就用订单金额；传了别的值 → 服务端会按"金额不一致"拒掉
    （这正是要测的负向路径，不是后门）。
    """
    _check_mock_source(request)
    svc = _svc(request)
    if not svc.enabled or not isinstance(svc.gateway, MockGateway):
        raise errors.payment_unconfigured()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    order_id = str(payload.get("outTradeNo") or payload.get("orderId") or "")
    order = svc.store.order(order_id)
    if order is None:
        raise errors.order_not_found(order_id)
    amount = payload.get("amountCents")
    trade_status = "TRADE_SUCCESS"
    if str(payload.get("status") or "").lower() in ("closed", "failed", "fail"):
        trade_status = "TRADE_CLOSED"
    form = svc.gateway.build_form(
        order, trade_no=str(payload.get("tradeNo") or ""),
        trade_status=trade_status,
        amount_cents=(int(amount) if amount is not None else None),
        buyer_id=str(payload.get("buyerId") or ""))
    out = svc.apply_notify(svc.gateway.verify_notify(form))
    out.update({"mock": True, "tradeNo": form["trade_no"], "orderId": order_id})
    return out
