# -*- coding: utf-8 -*-
"""套餐 / 订单 / 权益（设计 `docs/后端收费-交付给服务器dsh/01-后端收费与自动配对-设计.md` §4.1–§4.6）。

## 三条铁律

1. **价格与额度只在服务端**：客户端只传 `planCode`；金额、天数、额度全部从
   `plans` 表读（代码里不写死）。
2. **支付成功才发配对码**：码由订单驱动（`orders.pairing_code_id`），未支付订单
   不产生可用码。
3. **码仍是一次性 + 短 TTL**：复用既有的 `pairing_codes` 表与 `/v1/pair` 语义，
   不新造第二套配对协议。

## 权益归人，设备只是登录端

`entitlements` 的主键是 `user_id`。`user_devices` 只回答"这台设备是谁的"。
所以续费/换机不会重复收费，日额度/QPS/并发也按人算（见 `routes.State` 的闸门）。

## 状态机只许前进

`created → paying → paid → (refunded | closed)`。唯一的后退例外是
`paid → refunded`（设计 §4.6 明说）。任何"从终态再变回 paid"的路径都必须被拒 ——
那会把退款后的权益又送回来。

## 与 Mock 支付的关系

本模块不 import 任何网关实现：`payment.build_gateway(cfg)` 给出一个
`create_pay / verify_notify / query / refund` 的对象。Mock 与真实支付宝
走**同一条** `apply_notify()`，所以切网关不用改这里一行。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from server import errors, ops, payment as payment_mod
from server import ratelimit
from server.auth import (CODE_ALPHABET, hash_pairing_code, pairing_code_expired)

router = APIRouter(prefix="/v1/billing")

#: 出厂套餐（任务书 T3 / 设计 §0、§4.5、§6）。**管理面可改价/下架**；
#: 这里只是"第一次启动时补缺"（`Store.seed_plans` 是 `INSERT OR IGNORE`）。
DEFAULT_PLANS = (
    {"code": "monthly", "name": "月卡", "price_cents": 1000, "days": 30,
     "daily_audio_minutes": 120.0, "tts_chars": 50000, "qps": 1,
     "max_devices": 3, "max_concurrent": 1, "active": 1, "sort": 10},
    {"code": "trial", "name": "7 天试用", "price_cents": 0, "days": 7,
     "daily_audio_minutes": 120.0, "tts_chars": 50000, "qps": 1,
     "max_devices": 3, "max_concurrent": 1, "active": 1, "sort": 20},
    {"code": "internal", "name": "内部账号", "price_cents": 0, "days": 0,
     "daily_audio_minutes": 9999.0, "tts_chars": 5000000, "qps": 1,
     "max_devices": 3, "max_concurrent": 1, "active": 1, "sort": 90},
)

#: 计划行的列顺序（`store.seed_plans` / `store.upsert_plan` 共用）。
PLAN_COLUMNS = ("code", "name", "price_cents", "days", "daily_audio_minutes",
                "tts_chars", "qps", "max_devices", "max_concurrent", "active", "sort")

#: 允许的订单状态（**名字即契约**：客户端与对账脚本都按它判）。
ORDER_STATES = ("created", "paying", "paid", "refunded", "closed")


def new_order_id() -> str:
    """不可猜的订单号（设计 §4.6：`secrets.token_urlsafe`）。"""
    return "ord-" + secrets.token_urlsafe(18)


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def _unjson(text: Any) -> Dict[str, Any]:
    try:
        got = json.loads(str(text or "") or "{}")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def plan_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """套餐 → 可出网的 camelCase 视图。**金额只在服务端算，客户端只读**。"""
    return {
        "code": str(row.get("code") or ""),
        "name": str(row.get("name") or ""),
        "priceCents": int(row.get("price_cents") or 0),
        "days": int(row.get("days") or 0),
        "dailyAudioMinutes": float(row.get("daily_audio_minutes") or 0.0),
        "ttsChars": int(row.get("tts_chars") or 0),
        "qps": int(row.get("qps") or 1),
        "maxDevices": int(row.get("max_devices") or 3),
        "maxConcurrent": int(row.get("max_concurrent") or 1),
        "active": bool(row.get("active")),
    }


def parse_plan(payload: Dict[str, Any]) -> Dict[str, Any]:
    """管理面表单 → 一行套餐。**越界/非数 → 400 + 中文原因，不静默夹紧**。

    与配对码 TTL 那条同一个口径：夹紧会让"我明明填了 30 天"变成"其实 7 天"，
    而管理员以为上架的是 30 天。
    """
    payload = payload or {}
    code = str(payload.get("code") or "").strip().lower()
    if not code or not all(c.isalnum() or c in "-_" for c in code):
        raise errors.bad_request("套餐 code 只能包含字母/数字/下划线/短横线")
    out: Dict[str, Any] = {"code": code,
                           "name": str(payload.get("name") or code).strip()}

    def _int(field: str, default: int, lo: int, hi: int) -> int:
        raw = payload.get(field, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise errors.bad_request("%s 要是整数" % field)
        if value < lo or value > hi:
            raise errors.bad_request("%s 要在 %d ~ %d 之间" % (field, lo, hi))
        return value

    def _float(field: str, default: float, lo: float, hi: float) -> float:
        raw = payload.get(field, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise errors.bad_request("%s 要是数字" % field)
        if value < lo or value > hi:
            raise errors.bad_request("%s 要在 %.1f ~ %.1f 之间" % (field, lo, hi))
        return value

    out["price_cents"] = _int("priceCents", 1000, 0, 10_000_000)
    out["days"] = _int("days", 30, 0, 3650)
    out["daily_audio_minutes"] = _float("dailyAudioMinutes", 120.0, 0.0, 1_000_000)
    out["tts_chars"] = _int("ttsChars", 50000, 0, 1_000_000_000)
    out["qps"] = _int("qps", 1, 1, 1000)
    out["max_devices"] = _int("maxDevices", 3, 1, 100)
    out["max_concurrent"] = _int("maxConcurrent", 1, 1, 100)
    out["active"] = 1 if payload.get("active", True) else 0
    out["sort"] = _int("sort", 50, 0, 100000)
    return out


class BillingService:
    """收费的全部业务判断（路由只负责"怎么问、怎么回 JSON"）。"""

    def __init__(self, cfg, store, auth):
        self.cfg = cfg
        self.store = store
        self.auth = auth
        self.gateway = payment_mod.build_gateway(cfg)
        #: 1 QPS 按 **user**（令牌桶，突发 1）。多台设备共用同一个桶。
        self.qps = ratelimit.TokenBucket(rate=float(cfg.get("billing.qps", 1) or 1),
                                         burst=1.0)
        #: 按 user 的并发（默认 1）。与既有的"每客户端 1 条通道"并存。
        self.concurrency = ratelimit.UserConcurrency()
        #: `client_id → (user_id, 取到的时刻)`。设备绑定不每请求查库（设计 §4.4）。
        self._device_cache: Dict[str, tuple] = {}
        self._device_lock = threading.Lock()
        self._device_ttl_s = 30.0
        #: `user_id → (权益行, 取到的时刻)`。权益几乎不变，5 秒缓存足够；
        #: 管理面调整/退款会主动失效（否则"已退款还能用"最多滞后 5 秒）。
        self._ent_cache: Dict[str, tuple] = {}
        self._ent_ttl_s = 5.0
        #: 日额度账本由 `routes.State` 造好之后挂上来（它们要用同一个时钟）。
        #: 计时/记账在 `State.billing()/timed_billing()`，这里只做"用完没有"的判定。
        self.user_quota = None
        self.tts_ledger = None

    def attach_ledgers(self, user_quota, tts_ledger) -> None:
        self.user_quota = user_quota
        self.tts_ledger = tts_ledger

    # ---------------------------------------------------------------- 开关

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("billing.enabled", False))

    @property
    def provider(self) -> str:
        return str(self.cfg.get("payment.provider", "disabled") or "disabled").lower()

    # ---------------------------------------------------------------- 套餐

    def seed(self) -> int:
        return self.store.seed_plans(DEFAULT_PLANS)

    def plans(self, *, active_only: bool = True) -> List[Dict[str, Any]]:
        return self.store.plans(active_only=active_only)

    def plan(self, code: str) -> Dict[str, Any]:
        row = self.store.plan(str(code or ""))
        if row is None or not int(row.get("active") or 0):
            raise errors.plan_not_found(code)
        return row

    # ---------------------------------------------------------------- 权益

    def entitlement(self, user_id: str) -> Optional[Dict[str, Any]]:
        uid = str(user_id or "")
        now = time.time()
        hit = self._ent_cache.get(uid)
        if hit is not None and (now - hit[1]) < self._ent_ttl_s:
            return hit[0]
        row = self.store.entitlement(uid)
        self._ent_cache[uid] = (row, now)
        return row

    def forget_entitlement(self, user_id: str) -> None:
        self._ent_cache.pop(str(user_id or ""), None)

    def require_entitlement(self, user_id: str, now: Optional[float] = None
                            ) -> Dict[str, Any]:
        """有权益吗？没有/到期/被停用 → 402（三个不同的 code，见 `errors.py`）。"""
        now = time.time() if now is None else float(now)
        uid = str(user_id or "")
        # **到期/退款必须立刻生效**：这条判定直接读库，不走 5 秒缓存
        # （管理面改完、甚至测试直接改库，下一个请求就该看到）。
        row = self.store.entitlement(uid)
        self._ent_cache[uid] = (row, time.time())
        if row is None:
            raise errors.entitlement_missing()
        status = str(row.get("status") or "")
        if status != "active":
            raise errors.entitlement_inactive(status)
        paid_until = float(row.get("paid_until") or 0)
        # `paid_until <= 0` = 不过期（internal）。设计 §4.5。
        if paid_until > 0 and paid_until < now:
            raise errors.entitlement_expired(paid_until)
        return row

    def _limits(self, ent: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        def pick(key: str, default):
            if ent and ent.get(key) not in (None, ""):
                return ent.get(key)
            return self.cfg.get("billing.%s" % key, default)

        return {
            "daily_audio_minutes": float(pick("daily_audio_minutes", 120.0) or 0.0),
            "tts_chars": int(pick("tts_chars", 50000) or 0),
            "qps": max(1, int(pick("qps", 1) or 1)),
            "max_devices": max(1, int(pick("max_devices", 3) or 3)),
            "max_concurrent": max(1, int(pick("max_concurrent", 1) or 1)),
        }

    def limits(self, user_id: str) -> Dict[str, Any]:
        """这个账号此刻生效的额度（没有权益 → 配置兜底值，**但不代表能放行**）。"""
        return self._limits(self.entitlement(user_id))

    def device_limit(self, user_id: str) -> int:
        return int(self.limits(user_id)["max_devices"])

    def grant_trial(self, user_id: str) -> Optional[Dict[str, Any]]:
        """注册时送一次 7 天试用（默认开、管理面可关、**一次性**）。

        "一次性"是靠"已有 `entitlements` 行就不再送"实现的：试用过期后那一行
        仍在（`status=active`、`paid_until` 已过），所以重新登录不会再送第二份。
        """
        if not bool(self.cfg.get("trial.enabled", True)):
            return None
        existing = self.entitlement(user_id)
        if existing is not None:
            return existing
        plan = self.store.plan("trial")
        if plan is None:
            return None
        days = int(self.cfg.get("trial.days", 7) or 7)
        now = time.time()
        row = {
            "user_id": str(user_id),
            "plan_code": "trial",
            "paid_until": now + max(1, days) * 86400.0,
            "daily_audio_minutes": float(plan.get("daily_audio_minutes") or 0),
            "tts_chars": int(plan.get("tts_chars") or 0),
            "qps": int(plan.get("qps") or 1),
            "max_devices": int(plan.get("max_devices") or 3),
            "max_concurrent": int(plan.get("max_concurrent") or 1),
            "status": "active",
            "order_id": "",
            "updated_at": now,
        }
        self.store.upsert_entitlement(row)
        self.forget_entitlement(user_id)
        self.store.insert_order_event("", "trial", _json({"userId": user_id}))
        return row

    def entitlement_summary(self, user_id: str, *, devices: int = 0) -> Dict[str, Any]:
        """`/v1/capabilities` 与「我的」页读的那份权益摘要（设计 §4.4 末两条）。"""
        now = time.time()
        row = self.entitlement(user_id)
        if row is None:
            return {"status": "none", "plan": "", "paidUntil": 0,
                    "dailyMinutes": 0.0, "dailyMinutesLeft": 0.0,
                    "ttsCharsLeft": 0, "qps": 0,
                    "maxDevices": 0, "maxConcurrent": 0, "devices": int(devices),
                    "expiringSoon": False}
        limits = self._limits(row)
        paid_until = float(row.get("paid_until") or 0)
        status = str(row.get("status") or "")
        if status == "active" and paid_until > 0 and paid_until < now:
            status = "expired"
        left_days = ((paid_until - now) / 86400.0) if paid_until > 0 else None
        uid = str(user_id or "")
        # 今日剩余：上限 - 进程内账本里今天已用的量（与 `routes.State` 同一个账本）。
        daily_left = limits["daily_audio_minutes"]
        if self.user_quota is not None:
            daily_left = max(0.0, daily_left - float(self.user_quota.used_minutes(uid)))
        tts_left = limits["tts_chars"]
        if self.tts_ledger is not None:
            rem = self.tts_ledger.remaining(uid)
            if rem is not None:
                tts_left = rem
        return {
            "status": status,
            "plan": str(row.get("plan_code") or ""),
            "paidUntil": paid_until,
            "dailyMinutes": limits["daily_audio_minutes"],
            "dailyMinutesLeft": round(daily_left, 2),
            "ttsCharsLeft": int(tts_left),
            "qps": limits["qps"],
            "maxDevices": limits["max_devices"],
            "maxConcurrent": limits["max_concurrent"],
            "devices": int(devices),
            # 到期前 7 天内为 True（7 / 3 / 1 天那几档都在里面；设计 §4.4）。
            "expiringSoon": bool(left_days is not None and 0 < left_days <= 7.0),
        }

    # ---------------------------------------------------------------- 设备映射

    def user_for_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        """`client_id → user`（带 30 秒缓存）。没有绑定 → None（走老路）。

        解绑后的设备（`status=revoked`）**明确 403 `device_revoked`** ——
        不能静默回落成"没有绑定、按免费额度放行"，那等于解绑没生效。
        """
        cid = str(client_id or "")
        if not cid:
            return None
        now = time.time()
        with self._device_lock:
            hit = self._device_cache.get(cid)
        if hit is not None and (now - hit[1]) < self._device_ttl_s:
            uid = hit[0]
        else:
            row = self.store.device_for_client(cid)
            uid = str((row or {}).get("user_id") or "")
            with self._device_lock:
                self._device_cache[cid] = (uid, now)
            if row is not None and str(row.get("status") or "") != "active":
                raise errors.device_revoked()
        if not uid:
            return None
        user = self.store.user(uid)
        if user is None or str(user.get("status") or "active") != "active":
            return None
        return user

    def forget_device(self, client_id: str) -> None:
        with self._device_lock:
            self._device_cache.pop(str(client_id or ""), None)

    # ---------------------------------------------------------------- 准入闸门

    def check_access(self, client_id: str, *, kind: str = "audio") -> Optional[Dict[str, Any]]:
        """能力请求的收费闸门：`client → user → 权益 → 1 QPS → 日额度`。

        返回 `None` = 这条客户端没有绑定账号（**老路**，继续用 `clients` 的
        免费额度，设计 §5.0.0 的"两条路并存"）。返回 dict = 绑定账号的上下文，
        调用方把它传给 `timed_billing()`（`user_id` 用来记账/占并发槽）。

        **顺序是有意的**（任务书 T5）：鉴权 → user → 1 QPS → 准入/并发 → 计费。
        这里只做到"日额度检查"；并发槽由 `user_slot()` 在推理期间持有。
        """
        if not self.enabled:
            return None
        user = self.user_for_client(client_id)
        if user is None:
            return None
        uid = str(user["user_id"])
        ent = self.require_entitlement(uid)
        limits = self._limits(ent)
        try:
            self.qps.check(uid, rate=float(limits["qps"]))
        except errors.EchoError:
            # 顺序仍是 1 QPS 先判（任务书 T5），但如果今天的额度**已经用完**，
            # "今天别再试"比"慢一点"更准确 —— 换那个错误告诉客户端，
            # 否则客户端会等一秒再撞一次同样的墙。
            if kind == "audio":
                self.check_audio_quota(uid)
            elif kind == "tts":
                self.check_tts_quota(uid, 0)
            raise
        if kind == "audio":
            self.check_audio_quota(uid)
        elif kind == "tts":
            self.check_tts_quota(uid, 0)
        return {"user_id": uid, "entitlement": ent, "limits": limits}

    def check_audio_quota(self, user_id: str) -> None:
        """当日音频额度（按 user 累计）。上限来自 `entitlements`。

        真正的计数在 `routes.State.user_quota`（进程内日报账，与既有 `quota.py`
        同一个取舍）；这里只做"今天是不是已经用完了"的判定。
        """
        if self.user_quota is not None:
            self.user_quota.check(str(user_id))

    def check_tts_quota(self, user_id: str, chars: int = 0) -> None:
        """TTS 日字符额度。`/v1/tts`（edge-tts 通道）在读 body 后按
        `len(text)` 预判，合成成功才记账（见 `server/routes.py::tts`）。"""
        if self.tts_ledger is not None:
            self.tts_ledger.check(str(user_id), add_chars=int(chars or 0))

    @contextmanager
    def user_slot(self, user_id: Optional[str]):
        """按 user 的并发槽（默认 1）。同一账号第二台设备 → 409 `client_busy`。

        TTS 不占这个槽（不占 GPU，只受 1 QPS，设计 §4.4）—— 调用方按能力选。
        """
        if not self.enabled or not user_id:
            yield
            return
        limit = int(self.limits(str(user_id))["max_concurrent"])
        with self.concurrency.hold(str(user_id), limit):
            yield

    # ---------------------------------------------------------------- 订单

    def _order_pay_view(self, order: Dict[str, Any], *, create: bool = False
                        ) -> Dict[str, Any]:
        meta = _unjson(order.get("meta"))
        view = {
            "orderId": str(order.get("order_id") or ""),
            "planCode": str(order.get("plan_code") or ""),
            "amountCents": int(order.get("amount_cents") or 0),
            "status": str(order.get("status") or ""),
            "createdAt": float(order.get("created_at") or 0),
            "expiresAt": float(order.get("expires_at") or 0),
            "paidAt": float(order.get("paid_at") or 0),
            "payUrl": str(meta.get("payUrl") or ""),
            "qrCode": str(meta.get("qrCode") or ""),
        }
        if create:
            view["mock"] = bool(meta.get("mock"))
        if view["status"] == "paid":
            view["pairUrl"] = self._pair_url_locked(order)
        return view

    def order_view(self, order: Dict[str, Any]) -> Dict[str, Any]:
        return self._order_pay_view(order)

    def create_order(self, user_id: str, plan_code: str, *, client_id: str = ""
                     ) -> Dict[str, Any]:
        if not self.enabled:
            raise errors.billing_disabled()
        plan = self.plan(plan_code)
        if int(plan.get("price_cents") or 0) <= 0:
            raise errors.bad_request("这个套餐不需要下单（试用由系统赠送）")
        if not getattr(self.gateway, "configured", False):
            # **先判支付配置，再落订单**：未配置时不要留一堆永远 `closed` 的孤儿订单。
            raise errors.payment_unconfigured()
        now = time.time()
        hourly = int(self.cfg.get("billing.order_rate_per_hour", 10) or 10)
        if hourly > 0 and self.store.orders_created_since(now - 3600, user_id) >= hourly:
            raise errors.rate_limited(3600, "同一账号一小时内建单太多")
        order = {
            "order_id": new_order_id(),
            "plan_code": str(plan["code"]),
            "amount_cents": int(plan.get("price_cents") or 0),
            "status": "created",
            "user_id": str(user_id),
            "client_id": str(client_id or ""),
            "pairing_code_id": "",
            "alipay_trade_no": "",
            "created_at": now,
            "paid_at": 0.0,
            "expires_at": now + float(self.cfg.get("billing.order_ttl_s", 900) or 900),
            "meta": "",
        }
        self.store.create_order(order)
        self.store.insert_order_event(order["order_id"], "created", "")
        try:
            pay = self.gateway.create_pay(order)
        except Exception:
            # 支付没配好/网关失败：订单留成 closed（**不留一个永远 paying 的孤儿**），
            # 再把原始错误抛给客户端。
            self.store.update_order(order["order_id"], status="closed")
            self.store.insert_order_event(order["order_id"], "pay-failed", "")
            raise
        order["status"] = "paying"
        order["meta"] = _json({
            "payUrl": str(pay.get("payUrl") or ""),
            "qrCode": str(pay.get("qrCode") or ""),
            "mock": bool(pay.get("mock")),
        })
        self.store.update_order(order["order_id"], status="paying", meta=order["meta"])
        self.store.insert_order_event(order["order_id"], "paying", "")
        return self._order_pay_view(order, create=True)

    def order_for_user(self, order_id: str, user_id: str) -> Dict[str, Any]:
        order = self.store.order(str(order_id or ""))
        if order is None or str(order.get("user_id") or "") != str(user_id or ""):
            # **不区分"不存在"与"不是你的"**：那会送一个枚举订单号的口子。
            raise errors.order_not_found(order_id)
        return order

    def cancel_order(self, order_id: str, user_id: str) -> Dict[str, Any]:
        order = self.order_for_user(order_id, user_id)
        status = str(order.get("status") or "")
        if status not in ("created", "paying"):
            raise errors.order_state_conflict(status, "created/paying")
        self.store.update_order(order["order_id"], status="closed")
        self.store.insert_order_event(order["order_id"], "cancelled", "")
        return {"ok": True, "orderId": order["order_id"], "status": "closed"}

    # ---------------------------------------------------------------- 支付回调

    def apply_notify(self, norm: Dict[str, Any]) -> Dict[str, Any]:
        """已验签/已归一化的支付结果 → 订单 + 权益。**幂等**。

        `norm` 至少含 `tradeNo` / `outTradeNo` / `amountCents` / `status`。
        Mock 与真实支付宝都走这一条（`payment` 只负责验签与字段归一）。
        """
        trade_no = str(norm.get("tradeNo") or "")
        order_id = str(norm.get("outTradeNo") or "")
        if not trade_no or not order_id:
            raise errors.bad_request("支付回调缺 tradeNo / outTradeNo")
        order = self.store.order(order_id)
        if order is None:
            raise errors.order_not_found(order_id)
        amount = int(norm.get("amountCents") or 0)
        if amount != int(order.get("amount_cents") or 0):
            raise errors.bad_request("支付金额与订单金额不一致")
        status = str(norm.get("status") or "")
        first = self.store.record_notify(trade_no, order_id,
                                         raw=json.dumps(norm, ensure_ascii=False),
                                         verified=bool(norm.get("verified", True)))
        if status != "paid":
            # 非成功状态只留痕（关闭/失败），不改权益。
            self.store.insert_order_event(order_id, "notify-" + (status or "unknown"), "")
            return {"ok": True, "orderId": order_id, "status": "ignored",
                    "duplicate": not first}
        current = str(order.get("status") or "")
        if current == "paid":
            # 同一笔重复通知 → 幂等成功；另一笔 trade_no 打到已支付订单 → 拒绝。
            if str(order.get("alipay_trade_no") or "") == trade_no:
                return {"ok": True, "orderId": order_id, "status": "paid",
                        "duplicate": True}
            raise errors.order_state_conflict(current, "未支付订单")
        if current == "refunded":
            raise errors.order_state_conflict(current, "未支付订单")
        if current not in ("created", "paying"):
            raise errors.order_state_conflict(current, "created/paying")

        now = time.time()
        meta = _unjson(order.get("meta"))
        meta["tradeNo"] = trade_no
        if norm.get("buyerId"):
            meta["buyerId"] = str(norm["buyerId"])
        self.store.update_order(order_id, status="paid", paid_at=now,
                                alipay_trade_no=trade_no, meta=_json(meta))
        self.store.insert_order_event(order_id, "paid", _json({"tradeNo": trade_no}))
        # 支付身份回填（设计 §3.5.11：支付只做"把 payment identity 绑到当前 user"）。
        # **只在该 user 还没有 identity 时填**，绝不去抢别人的 open_id。
        buyer = str(norm.get("buyerId") or "")
        if buyer and str(order.get("user_id") or ""):
            user = self.store.user(str(order["user_id"]))
            if user is not None and not str(user.get("alipay_open_id") or ""):
                try:
                    self.store.update_user(str(order["user_id"]), alipay_open_id=buyer)
                except Exception:                               # pragma: no cover - 兜底
                    pass
        order = self.store.order(order_id) or order
        self.extend_entitlement(order, now=now)
        # 第一次支付且还没有设备 → 发配对码；已有设备 = 续费，不发码（设计 §4.5）。
        if self.store.count_devices(str(order.get("user_id") or "")) == 0:
            self.issue_purchase_code(order)
        return {"ok": True, "orderId": order_id, "status": "paid", "duplicate": False}

    def extend_entitlement(self, order: Dict[str, Any], *, now: Optional[float] = None
                           ) -> Dict[str, Any]:
        """把这次支付的天数加到权益上（提前续费从 `max(now, paid_until)` 起算）。

        **internal 不被覆盖**：内部账号不过期、不收费，买了月卡也不该被降级。
        """
        now = time.time() if now is None else float(now)
        uid = str(order.get("user_id") or "")
        plan = self.store.plan(str(order.get("plan_code") or ""))
        if plan is None:
            raise errors.plan_not_found(str(order.get("plan_code") or ""))
        existing = self.entitlement(uid)
        if (existing is not None
                and str(existing.get("plan_code") or "") == "internal"
                and str(existing.get("status") or "") == "active"):
            return existing
        base = now
        # **试用不叠加**：设计 §4.5「试用到期后…续费买月卡即从当天起算」。
        # 试用期内就买也按"从这次支付起算"（否则新用户第一单会变成 37 天，
        # 与任务书 §7 第 5 条"paid_until = 支付时刻 + 30 天"对不上）。
        if (existing is not None and str(existing.get("status") or "") == "active"
                and str(existing.get("plan_code") or "") != "trial"
                and float(existing.get("paid_until") or 0) > now):
            base = float(existing["paid_until"])
        days = int(plan.get("days") or 0)
        row = {
            "user_id": uid,
            "plan_code": str(plan.get("code") or ""),
            "paid_until": (base + days * 86400.0) if days > 0 else 0.0,
            "daily_audio_minutes": float(plan.get("daily_audio_minutes") or 0),
            "tts_chars": int(plan.get("tts_chars") or 0),
            "qps": int(plan.get("qps") or 1),
            "max_devices": int(plan.get("max_devices") or 3),
            "max_concurrent": int(plan.get("max_concurrent") or 1),
            "status": "active",
            "order_id": str(order.get("order_id") or ""),
            "updated_at": now,
        }
        self.store.upsert_entitlement(row)
        self.forget_entitlement(uid)
        self.store.insert_order_event(str(order.get("order_id") or ""), "entitlement",
                                      _json({"plan": row["plan_code"],
                                             "paidUntil": row["paid_until"]}))
        return row

    # ---------------------------------------------------------------- 发码 / 配对

    def _derive_pair_code(self, order_id: str, nonce: str) -> str:
        """从"订单 id + 随机 nonce + 服务端密钥"派生配对码。

        为什么不随机生成后把明文存进 `orders.meta`：那会让"配对码只存哈希"
        这条既有约束在收费这条路上破一个口子。派生方案让服务端在**不存明文**的
        前提下重复算出同一张码（nonce 可以存，它单独没有用 —— 没有服务端密钥
        算不回去），所以"同一订单重复轮询返回同一张码"成立。
        """
        key = str(self.auth.key).encode("utf-8")
        digest = hmac.new(key, ("pair:%s:%s" % (order_id, nonce)).encode("utf-8"),
                          hashlib.sha256).digest()
        return "".join(CODE_ALPHABET[b % len(CODE_ALPHABET)]
                       for b in digest[:8])

    def issue_purchase_code(self, order: Dict[str, Any]) -> Optional[str]:
        """给这张订单发一张配对码（**同一订单只发一张；重发时旧码作废**）。

        返回 `echo://pair?...`。库里**只有哈希**（`pairing_codes.code_hash`）与
        一个随机 `nonce`；码本身由 `_derive_pair_code()` 现算，所以重复轮询能拿到
        同一张，而 DB 里没有明文。
        """
        order_id = str(order.get("order_id") or "")
        user_id = str(order.get("user_id") or "")
        old = str(order.get("pairing_code_id") or "")
        if old:
            self.store.delete_pairing_code(old)
        plan = self.store.plan(str(order.get("plan_code") or ""))
        name = str((plan or {}).get("name") or "ECHO 后端")
        ttl = float(self.cfg.get("billing.pair_code_ttl_s", 900) or 900)
        nonce = secrets.token_hex(8)
        code = self._derive_pair_code(order_id, nonce)
        self.auth.create_pairing_code(
            created_by="order:" + order_id, name=name,
            scopes=str(self.cfg.get("billing.client_scopes", "") or ""),
            ttl_s=ttl, user_id=user_id, kind="purchase", code=code)
        code_id = hash_pairing_code(code)
        meta = _unjson(order.get("meta"))
        meta.pop("pairCode", None)                 # 旧版本可能留下的明文，清掉
        meta.pop("pairUrl", None)
        meta["pairNonce"] = nonce
        self.store.update_order(order_id, pairing_code_id=str(code_id or ""),
                                meta=_json(meta))
        self.store.insert_order_event(order_id, "pair-code", _json({"codeId": code_id}))
        return self._pair_url_of(order_id, nonce, code)

    def _pair_url_of(self, order_id: str, nonce: str, code: str) -> str:
        ttl = float(self.cfg.get("billing.pair_code_ttl_s", 900) or 900)
        built = ops.pairing_string(self.cfg, code, ttl_s=ttl)
        return str(built.get("url") or "")

    def _pair_url_locked(self, order: Dict[str, Any]) -> str:
        """已支付订单的 `pairUrl`（**只读路径**）。

        * 已经有活跃设备 → 没有 `pairUrl`（续费不发码）；
        * 码还在 → 用 nonce 重算出当初那张（订单轮询重复拿到同一张）；
        * 码过期/没了且仍无设备 → 重新发一张（旧码本来就已失效）。
        """
        if str(order.get("status") or "") != "paid":
            return ""
        user_id = str(order.get("user_id") or "")
        if self.store.count_devices(user_id) > 0:
            return ""
        order_id = str(order.get("order_id") or "")
        meta = _unjson(order.get("meta"))
        nonce = str(meta.get("pairNonce") or "")
        code_id = str(order.get("pairing_code_id") or "")
        row = self.store.peek_pairing_code(code_id) if code_id else None
        if row is not None and not pairing_code_expired(row) and nonce:
            code = self._derive_pair_code(order_id, nonce)
            if hash_pairing_code(code) == code_id:
                return self._pair_url_of(order_id, nonce, code)
        # 码过期（或已被清掉）且仍没有设备 → 重发一张；旧码本来就用不了了。
        fresh = self.issue_purchase_code(order)
        return str(fresh or "")

    def refund_order(self, order_id: str) -> Dict[str, Any]:
        """退款：调网关退款 + 订单变 `refunded` + **权益立即失效**（设计 §4.4）。"""
        order = self.store.order(str(order_id or ""))
        if order is None:
            raise errors.order_not_found(order_id)
        if str(order.get("status") or "") != "paid":
            raise errors.order_state_conflict(str(order.get("status") or ""), "paid")
        out = self.gateway.refund(order)
        self.store.update_order(order_id, status="refunded")
        self.store.set_entitlement_status(str(order.get("user_id") or ""), "refunded")
        self.forget_entitlement(str(order.get("user_id") or ""))
        self.store.insert_order_event(order_id, "refunded", _json({"gateway": out}))
        return {"ok": True, "orderId": order_id, "status": "refunded"}

    def precheck_pair(self, row: Optional[Dict[str, Any]]) -> None:
        """`/v1/pair` 兑换前的准入预检（设备上限 / 权益）。"""

        if not row or not row.get("user_id"):
            return
        user = self.store.user(str(row["user_id"]))
        if user is None or str(user.get("status") or "active") != "active":
            raise errors.forbidden("这个账号已停用")
        self.require_entitlement(str(row["user_id"]))
        self._check_device_room(str(row["user_id"]))

    def after_pair(self, row: Optional[Dict[str, Any]], client_id: str,
                   device_name: str = "") -> None:
        """兑换成功后把设备记到账号名下（`purchase` 与 `device-bind` 都走这里）。"""
        if not row or not row.get("user_id"):
            return
        uid = str(row["user_id"])
        self.store.upsert_device(uid, str(client_id), device_name)
        self.forget_device(client_id)
        self.store.insert_order_event(
            "", "paired", _json({"userId": uid, "clientId": str(client_id),
                                 "kind": str(row.get("kind") or "")}))
        # 并发下可能刚好被另一台抢了最后一个名额：**超出的这台要撤掉**。
        if self.store.count_devices(uid) > self.device_limit(uid):
            self.store.revoke_device(uid, str(client_id))
            try:
                self.auth.set_disabled(str(client_id), True)
            except Exception:                                   # pragma: no cover - 兜底
                pass
            raise errors.device_limit_reached(self.device_limit(uid))

    def _check_device_room(self, user_id: str) -> None:
        limit = self.device_limit(user_id)
        if self.store.count_devices(user_id) >= limit:
            raise errors.device_limit_reached(limit)

    # ---------------------------------------------------------------- 管理面

    def adjust_entitlement(self, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """管理面手工调整权益：延长 / 冻结 / 恢复 / 设 internal / 直接改字段。

        与退款是**两条路**：退款动的是"钱"，这里动的是"权限"。两者都要求审计
        （`admin.py` 的 `as_write` 负责），because 10 元小额服务里"手滑送一年"
        是最常见的事故。
        """
        if self.store.user(str(user_id)) is None:
            raise errors.order_not_found(str(user_id))
        existing = self.store.entitlement(str(user_id)) or {}
        row = {
            "user_id": str(user_id),
            "plan_code": str(existing.get("plan_code") or
                             self.cfg.get("billing.default_plan", "monthly")),
            "paid_until": float(existing.get("paid_until") or 0),
            "daily_audio_minutes": float(existing.get("daily_audio_minutes") or
                                         self.cfg.get("billing.daily_audio_minutes", 120)),
            "tts_chars": int(existing.get("tts_chars") or
                             self.cfg.get("billing.daily_tts_chars", 50000)),
            "qps": int(existing.get("qps") or self.cfg.get("billing.qps", 1)),
            "max_devices": int(existing.get("max_devices") or
                               self.cfg.get("billing.max_devices", 3)),
            "max_concurrent": int(existing.get("max_concurrent") or
                                  self.cfg.get("billing.max_concurrent", 1)),
            "status": str(existing.get("status") or "active"),
            "order_id": str(existing.get("order_id") or ""),
            "updated_at": time.time(),
        }
        action = str((payload or {}).get("action") or "").strip().lower()
        now = time.time()
        if action == "extend":
            try:
                days = int(payload.get("days", 30))
            except (TypeError, ValueError):
                raise errors.bad_request("days 要是天数")
            if days <= 0 or days > 3650:
                raise errors.bad_request("days 要在 1 ~ 3650 之间")
            base = max(now, row["paid_until"])
            row["paid_until"] = base + days * 86400.0
            row["status"] = "active"
        elif action in ("freeze", "disable"):
            row["status"] = "frozen"
        elif action in ("activate", "resume"):
            row["status"] = "active"
        elif action == "internal":
            plan = self.store.plan("internal")
            row.update({
                "plan_code": "internal", "paid_until": 0.0, "status": "active",
                "daily_audio_minutes": float((plan or {}).get("daily_audio_minutes")
                                             or 9999),
                "tts_chars": int((plan or {}).get("tts_chars") or 5000000),
                "qps": int((plan or {}).get("qps") or 1),
                "max_devices": int((plan or {}).get("max_devices") or 3),
                "max_concurrent": int((plan or {}).get("max_concurrent") or 1),
            })
        elif action == "set":
            if payload.get("planCode"):
                plan = self.store.plan(str(payload["planCode"]))
                if plan is None:
                    raise errors.plan_not_found(str(payload["planCode"]))
                row["plan_code"] = str(plan["code"])
            if payload.get("paidUntil") is not None:
                try:
                    row["paid_until"] = float(payload["paidUntil"])
                except (TypeError, ValueError):
                    raise errors.bad_request("paidUntil 要是 unix 秒")
            if payload.get("status"):
                status = str(payload["status"])
                if status not in ("active", "frozen", "refunded", "inactive"):
                    raise errors.bad_request("status 只能是 active/frozen/refunded/inactive")
                row["status"] = status
            for field, kind in (("dailyAudioMinutes", "daily_audio_minutes"),
                                ("ttsChars", "tts_chars"), ("qps", "qps"),
                                ("maxDevices", "max_devices"),
                                ("maxConcurrent", "max_concurrent")):
                if payload.get(field) is None:
                    continue
                try:
                    row[kind] = int(payload[field]) if kind != "daily_audio_minutes" \
                        else float(payload[field])
                except (TypeError, ValueError):
                    raise errors.bad_request("%s 要是数字" % field)
        else:
            raise errors.bad_request("action 只能是 extend/freeze/activate/internal/set")
        self.store.upsert_entitlement(row)
        self.forget_entitlement(str(user_id))
        self.store.insert_order_event(
            str(row.get("order_id") or ""), "entitlement-adjust",
            _json({"action": action, "paidUntil": row["paid_until"],
                   "status": row["status"]}))
        return row

    def overview(self) -> Dict[str, Any]:
        """管理面「收费」页签的概览（收入 / 订单 / 快到期）。**只报数字**。"""
        now = time.time()
        day_start = now - 24 * 3600
        orders = self.store.orders(limit=1000)
        paid = [o for o in orders if str(o.get("status") or "") == "paid"]
        revenue_today = sum(int(o.get("amount_cents") or 0) for o in paid
                            if float(o.get("paid_at") or 0) >= day_start)
        counts: Dict[str, int] = {}
        for o in orders:
            key = str(o.get("status") or "")
            counts[key] = counts.get(key, 0) + 1
        expiring = 0
        for ent in self.store.entitlements():
            if str(ent.get("status") or "") != "active":
                continue
            paid_until = float(ent.get("paid_until") or 0)
            if 0 < paid_until - now <= 7 * 86400:
                expiring += 1
        return {
            "orders": len(orders), "byStatus": counts,
            "revenueCentsToday": revenue_today,
            "revenueCentsTotal": sum(int(o.get("amount_cents") or 0) for o in paid),
            "expiringIn7Days": expiring,
            "plans": len(self.store.plans()),
            "users": len(self.store.users()),
            "generatedAt": now,
        }


# ---------------------------------------------------------------- 路由（/v1/billing）

def _state(request: Request):
    return request.app.state.echo


def _user(request: Request) -> Dict[str, Any]:
    """`/v1/billing/*` 的身份：**只认 userToken**（不能拿 client JWT 下单）。"""
    account = getattr(_state(request), "account", None)
    if account is None:
        raise errors.auth_misconfigured("服务端没有可用的账号服务")
    return account.authenticate(request.headers.get("authorization") or "")


def _billing(request: Request) -> BillingService:
    svc = getattr(_state(request), "billing_svc", None)
    if svc is None:
        raise errors.billing_disabled()
    return svc


@router.get("/plans")
def list_plans(request: Request):
    """公开套餐表（免登录）。**金额只从 `plans` 来**。"""
    svc = _billing(request)
    if not svc.enabled:
        raise errors.billing_disabled()
    plans = [plan_view(p) for p in svc.plans(active_only=True)]
    trial = {"enabled": bool(svc.cfg.get("trial.enabled", True)),
             "days": int(svc.cfg.get("trial.days", 7) or 7)}
    return {"plans": plans, "trial": trial}


@router.post("/orders")
async def create_order(request: Request):
    """建单（必须带 userToken）。客户端只传 `planCode`，**不传金额**。"""
    svc = _billing(request)
    if not svc.enabled:
        raise errors.billing_disabled()
    user = _user(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    plan_code = str(payload.get("planCode") or "")
    if not plan_code:
        raise errors.bad_request("要带 planCode")
    return svc.create_order(str(user["user_id"]), plan_code)


@router.get("/orders/{order_id}")
def get_order(request: Request, order_id: str):
    """订单状态 +（**仅 paid 后**）`pairUrl`。只有下单的那个人能看。"""
    svc = _billing(request)
    user = _user(request)
    order = svc.order_for_user(order_id, str(user["user_id"]))
    return svc.order_view(order)


@router.post("/orders/{order_id}/cancel")
def cancel_order(request: Request, order_id: str):
    svc = _billing(request)
    user = _user(request)
    return svc.cancel_order(order_id, str(user["user_id"]))


@router.get("/entitlement")
def get_entitlement(request: Request):
    """当前账号的权益摘要（没有权益也回 200 + `status=none`，面板据此显示入口）。"""
    svc = _billing(request)
    if not svc.enabled:
        raise errors.billing_disabled()
    user = _user(request)
    uid = str(user["user_id"])
    return svc.entitlement_summary(uid, devices=svc.store.count_devices(uid))


@router.post("/alipay/notify")
async def alipay_notify(request: Request):
    """支付宝异步通知：**验签 → 幂等 → 金额/订单校验 → 权益**。

    成功回纯文本 `success`（支付宝的约定）；失败回 4xx 让它重试。
    响应里**不回任何秘密**。
    """
    svc = _billing(request)
    try:
        form = dict(await request.form())
        norm = svc.gateway.verify_notify(form)
        svc.apply_notify(norm)
    except errors.EchoError:
        raise
    except Exception:
        # 验签失败/字段缺失一律 400：支付宝会按自己的策略重试；
        # 这里**不回 200**，否则失败的支付会被当成到账。
        raise errors.bad_request("支付回调不合法")
    return PlainTextResponse("success")
