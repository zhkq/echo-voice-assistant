# -*- coding: utf-8 -*-
"""真实支付宝网关的离线契约（RSA2 请求签名 + 同步响应验签 + query/refund）。

不连真网关：用本地生成的两对 RSA 密钥分别扮演「应用私钥」与「支付宝私钥」，
把支付宝同步响应按**原始 JSON 子串**签名，然后 patch `payment.urlopen`。
这样能在没有沙箱凭据的 CI/本机验证签名口径、字段校验与错误分类。

真实的沙箱 10.00 元付款由 Lead 在 2026-11-05 实测（page.pay + query + sync）；
本文件不声称跑过真实扣款，也不把 mock 结果当真实支付证据。
"""
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from email.message import Message
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives import hashes, serialization       # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa     # noqa: E402

from server import errors                                            # noqa: E402
from server import payment as payment_mod                             # noqa: E402
from server import settings as settings_mod                           # noqa: E402


class _FakeResponse:
    def __init__(self, body, charset=""):
        self._body = body
        self.headers = Message()
        if charset:
            self.headers["Content-Type"] = "application/json; charset=%s" % charset

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _gen_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_pem(key):
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo)


def _signed_response(method, node, alipay_key, charset="utf-8"):
    node_json = json.dumps(node, ensure_ascii=False, separators=(",", ":"))
    sign = base64.b64encode(alipay_key.sign(
        node_json.encode(charset), padding.PKCS1v15(), hashes.SHA256())).decode("ascii")
    body = '{"%s_response":%s,"sign":"%s"}' % (
        method.replace(".", "_"), node_json, sign)
    return body.encode(charset)


class AlipayGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-alipay-gateway-")
        self.app_key = _gen_key()
        self.alipay_key = _gen_key()
        self.private_path = os.path.join(self.tmp, "app-private.pem")
        self.public_path = os.path.join(self.tmp, "alipay-public.pem")
        with open(self.private_path, "wb") as fh:
            fh.write(self.app_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
        with open(self.public_path, "wb") as fh:
            fh.write(_public_pem(self.alipay_key))
        self.cfg = settings_mod.load()
        self.cfg.raw.setdefault("alipay", {}).update({
            "app_id": "2021000000000000",
            "private_key_path": self.private_path,
            "public_key_path": self.public_path,
            "gateway": "https://openapi-sandbox.example/gateway.do",
            "sandbox": True,
            "notify_url": "https://example.test/v1/billing/alipay/notify",
            "return_url": "https://example.test/pay",
            "timeout_s": 5,
        })
        self.gateway = payment_mod.AlipayGateway(self.cfg)
        self.captured = {}

    def _patch_response(self, method, node, *, charset="utf-8", mutate=None):
        body = _signed_response(method, node, self.alipay_key, charset=charset)
        if mutate is not None:
            body = mutate(body)
        response = _FakeResponse(body, charset=charset)

        def fake_urlopen(request, timeout=None):
            self.captured["url"] = request.full_url
            self.captured["timeout"] = timeout
            self.captured["body"] = request.data
            return response

        return patch.object(payment_mod, "urlopen", fake_urlopen)

    def _request_params(self):
        raw = self.captured.get("body") or b""
        parsed = parse_qs(raw.decode("utf-8"))
        return {key: values[0] for key, values in parsed.items()}

    def _assert_request_signed(self, params):
        sign = params.pop("sign")
        signed = payment_mod._canonical(params, include_sign_type=True).encode("utf-8")
        self.app_key.public_key().verify(
            base64.b64decode(sign), signed, padding.PKCS1v15(), hashes.SHA256())

    def test_create_pay_includes_return_url_notify_url_and_rsa2_signature(self):
        order = {"order_id": "ord-abc", "amount_cents": 1000,
                 "expires_at": time.time() + 900}
        pay = self.gateway.create_pay(order)
        self.assertFalse(pay["mock"])
        self.assertTrue(pay["payUrl"].startswith(
            "https://openapi-sandbox.example/gateway.do?"))
        params = {key: values[0] for key, values in
                  parse_qs(urlsplit(pay["payUrl"]).query).items()}
        self.assertEqual(params["method"], "alipay.trade.page.pay")
        self.assertEqual(params["app_id"], "2021000000000000")
        self.assertEqual(params["return_url"], "https://example.test/pay?order=ord-abc")
        self.assertEqual(params["notify_url"],
                         "https://example.test/v1/billing/alipay/notify")
        self.assertIn("biz_content", params)
        self.assertIn("ord-abc", params["biz_content"])
        self.assertIn("10.00", params["biz_content"])
        self._assert_request_signed(params)

    def test_query_paid_response_is_normalized_and_request_is_signed(self):
        node = {
            "code": "10000", "msg": "Success", "trade_no": "2026110522001",
            "out_trade_no": "ord-abc", "trade_status": "TRADE_SUCCESS",
            "total_amount": "10.00", "buyer_open_id": "buyer-open-1",
        }
        with self._patch_response("alipay.trade.query", node):
            norm = self.gateway.query({"order_id": "ord-abc", "amount_cents": 1000})
        self.assertEqual(norm["status"], "paid")
        self.assertEqual(norm["tradeNo"], "2026110522001")
        self.assertEqual(norm["outTradeNo"], "ord-abc")
        self.assertEqual(norm["amountCents"], 1000)
        self.assertEqual(norm["buyerId"], "buyer-open-1")
        self.assertTrue(norm["verified"])
        params = self._request_params()
        self.assertEqual(params["method"], "alipay.trade.query")
        self.assertIn("ord-abc", params["biz_content"])
        self._assert_request_signed(params)

    def test_query_trade_not_exist_is_pending_not_an_error(self):
        node = {"code": "40004", "msg": "Business Failed",
                "sub_code": "ACQ.TRADE_NOT_EXIST", "sub_msg": "交易不存在"}
        with self._patch_response("alipay.trade.query", node):
            norm = self.gateway.query({"order_id": "ord-missing"})
        self.assertEqual(norm["status"], "pending")
        self.assertEqual(norm["rawStatus"], "TRADE_NOT_EXIST")
        self.assertEqual(norm["outTradeNo"], "ord-missing")

    def test_query_wait_buyer_pay_is_pending_with_amount(self):
        node = {"code": "10000", "msg": "Success", "trade_no": "",
                "out_trade_no": "ord-abc", "trade_status": "WAIT_BUYER_PAY",
                "total_amount": "10.00"}
        with self._patch_response("alipay.trade.query", node):
            norm = self.gateway.query({"order_id": "ord-abc"})
        self.assertEqual(norm["status"], "pending")
        self.assertEqual(norm["amountCents"], 1000)

    def test_query_closed_maps_to_closed(self):
        node = {"code": "10000", "msg": "Success", "trade_no": "2026",
                "out_trade_no": "ord-abc", "trade_status": "TRADE_CLOSED",
                "total_amount": "10.00"}
        with self._patch_response("alipay.trade.query", node):
            norm = self.gateway.query({"order_id": "ord-abc"})
        self.assertEqual(norm["status"], "closed")

    def test_query_other_business_error_is_502(self):
        node = {"code": "20000", "msg": "Service Unavailable",
                "sub_code": "ACQ.SYSTEM_ERROR", "sub_msg": "系统错误"}
        with self._patch_response("alipay.trade.query", node):
            with self.assertRaises(errors.EchoError) as ctx:
                self.gateway.query({"order_id": "ord-abc"})
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.code, "payment_gateway_error")

    def test_query_bad_response_signature_is_502(self):
        node = {"code": "10000", "msg": "Success", "trade_no": "1",
                "out_trade_no": "ord-abc", "trade_status": "TRADE_SUCCESS",
                "total_amount": "10.00"}

        def corrupt(body):
            text = body.decode("utf-8")
            return text.replace('"trade_no":"1"', '"trade_no":"2"').encode("utf-8")

        with self._patch_response("alipay.trade.query", node, mutate=corrupt):
            with self.assertRaises(errors.EchoError) as ctx:
                self.gateway.query({"order_id": "ord-abc"})
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.code, "payment_gateway_error")

    def test_query_handles_gbk_responses(self):
        node = {"code": "40004", "msg": "业务失败",
                "sub_code": "ACQ.TRADE_NOT_EXIST", "sub_msg": "交易不存在"}
        with self._patch_response("alipay.trade.query", node, charset="gbk"):
            norm = self.gateway.query({"order_id": "ord-missing"})
        self.assertEqual(norm["status"], "pending")

    def test_refund_success_and_idempotent_request_no(self):
        node = {"code": "10000", "msg": "Success", "trade_no": "2026110522001",
                "out_trade_no": "ord-abc", "refund_fee": "10.00",
                "fund_change": "Y"}
        with self._patch_response("alipay.trade.refund", node):
            out = self.gateway.refund({"order_id": "ord-abc", "amount_cents": 1000,
                                       "alipay_trade_no": "2026110522001"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["refundFee"], "10.00")
        params = self._request_params()
        self.assertEqual(params["method"], "alipay.trade.refund")
        biz = json.loads(params["biz_content"])
        self.assertEqual(biz["out_trade_no"], "ord-abc")
        self.assertEqual(biz["refund_amount"], "10.00")
        self.assertEqual(biz["out_request_no"], "refund-ord-abc")
        self._assert_request_signed(params)

    def test_refund_error_is_502_and_does_not_pretend_success(self):
        node = {"code": "40004", "msg": "Business Failed",
                "sub_code": "ACQ.TRADE_NOT_EXIST", "sub_msg": "交易不存在"}
        with self._patch_response("alipay.trade.refund", node):
            with self.assertRaises(errors.EchoError) as ctx:
                self.gateway.refund({"order_id": "ord-abc", "amount_cents": 1000})
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.code, "payment_gateway_error")

    def test_unconfigured_gateway_still_raises_payment_unconfigured(self):
        self.cfg.raw["alipay"]["private_key_path"] = ""
        gateway = payment_mod.AlipayGateway(self.cfg)
        with self.assertRaises(errors.EchoError) as ctx:
            gateway.query({"order_id": "ord-abc"})
        self.assertEqual(ctx.exception.code, "payment_unconfigured")
        with self.assertRaises(errors.EchoError) as ctx:
            gateway.refund({"order_id": "ord-abc", "amount_cents": 1000})
        self.assertEqual(ctx.exception.code, "payment_unconfigured")


if __name__ == "__main__":
    unittest.main()
