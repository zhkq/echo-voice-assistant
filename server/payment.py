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

本轮只留骨架：`create_pay` / `verify_notify` 按支付宝网页收款的 RSA2 规则实现，
`query` / `refund` 明确报"尚未接入"（`payment_unconfigured`）。私钥只从
**服务端文件路径**读，不进仓库、不进日志。
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

from fastapi import APIRouter, Request

from server import errors

#: 支付宝交易状态里表示"已收到钱"的那两个（设计 §4.6 的白名单）。
PAID_TRADE_STATUSES = ("TRADE_SUCCESS", "TRADE_FINISHED")


def _canonical(form: Dict[str, Any]) -> str:
    """待签名串：按 key 字典序，`k=v` 用 `&` 连接，**排除 sign / sign_type**。"""
    items = [(str(k), str(v)) for k, v in (form or {}).items()
             if k not in ("sign", "sign_type") and v is not None and str(v) != ""]
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
        self.secret = str(secret or "echo-mock-payment-not-for-production")

    def _sign(self, form: Dict[str, Any]) -> str:
        return hmac.new(self.secret.encode("utf-8"),
                        _canonical(form).encode("utf-8"),
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
    """真实支付宝（网页收款 `alipay.trade.page.pay`）**骨架**。

    只实现两条能离线验证的：下单 URL 的 RSA2 签名、异步通知的 RSA2 验签。
    查询/退款**尚未接入**（要沙箱联调；本轮只跑 Mock）—— 明确报
    `payment_unconfigured`，不假装成功。
    """

    name = "alipay"

    def __init__(self, cfg):
        self.app_id = str(cfg.get("alipay.app_id", "") or "")
        self.private_key_path = str(cfg.get("alipay.private_key_path", "") or "")
        self.public_key_path = str(cfg.get("alipay.public_key_path", "") or "")
        self.gateway = str(cfg.get("alipay.gateway", "") or
                           "https://openapi.alipay.com/gateway.do")
        self.notify_url = str(cfg.get("payment.notify_url", "")
                              or cfg.get("alipay.notify_url", "") or "")

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
            _canonical(form).encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
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

    # ---- 网关接口 --------------------------------------------------------

    def create_pay(self, order: Dict[str, Any]) -> Dict[str, Any]:
        if not self.configured:
            raise errors.payment_unconfigured()
        biz = {
            "out_trade_no": str(order.get("order_id") or ""),
            "total_amount": "%.2f" % (int(order.get("amount_cents") or 0) / 100.0),
            "subject": "ECHO 后端套餐",
            "product_code": "FAST_INSTANT_TRADE_PAY",
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
        if str(got.get("app_id") or "") and str(got.get("app_id")) != self.app_id:
            raise errors.bad_request("支付宝回调 app_id 不匹配")
        return _normalize(got, verified=True)

    def query(self, order: Dict[str, Any]) -> Dict[str, Any]:
        # 主动查询是上线前的硬要求（设计 §7 第 3 条），但本轮没有沙箱环境；
        # 不假装成功 —— 让调用方明确知道"这条路还没接"。
        raise errors.payment_unconfigured()

    def refund(self, order: Dict[str, Any]) -> Dict[str, Any]:
        raise errors.payment_unconfigured()


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


@mock_router.post("/pay/{order_id}")
async def mock_pay(request: Request, order_id: str):
    """**模拟付款**：构造一张同形 notify → 走与真回调完全相同的校验/入账路径。

    响应里 `"mock": true`。这个端点只存在于 `payment.provider=mock`。
    """
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
