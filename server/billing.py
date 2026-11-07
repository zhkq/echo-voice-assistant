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

import datetime
import hashlib
import hmac
import json
import logging
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

log = logging.getLogger("echo.server")

router = APIRouter(prefix="/v1/billing")

#: 出厂套餐（任务书 T3 / 设计 §0、§4.5、§6）。**管理面可改价/下架**；
#: 这里只是"第一次启动时补缺"（`Store.seed_plans` 是 `INSERT OR IGNORE`）。
DEFAULT_PLANS = (
    {"code": "monthly", "name": "基础版：Basic", "price_cents": 1000, "days": 30,
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

#: 续费/退款/换套餐的**唯一口径**（2026-11-06，issue #13）。
#: 文案由服务端下发，页面不自己拼——这样页面、管理面、文档不会各说一套。
RENEWAL_RULE = ("期内续费从当前到期日顺延；过期或退款后从支付成功时间起算；"
                "试用不叠加；退款按剩余已支付订单重算。")
PLAN_CHANGE_RULE = ("暂不支持升级/降级：有有效付费权益时只能续费同一套餐；"
                    "等当前套餐到期后，或联系管理员处理。")
REFUND_RULE = "退款按剩余已支付订单重算到期时间；已消费的额度不退。"
#: 自动扣款本轮不做；以后若做，必须是显式勾选 + 可取消 + 到期前提醒。
AUTO_RENEW_ENABLED = False

#: `renewalBasis` 的取值（客户端只做展示，不参与计算）。
RENEWAL_BASIS_PAID_UNTIL = "paid_until"
RENEWAL_BASIS_PAYMENT_TIME = "payment_time"
RENEWAL_BASIS_NEVER = "never_expires"
RENEWAL_BASIS_NOT_APPLICABLE = "not_applicable"


def day_start(cfg, ts: Optional[float] = None) -> float:
    """`billing.timezone` 的**自然日 0 点**（epoch 秒）。"""
    now_ts = time.time() if ts is None else float(ts)
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(str(cfg.get("billing.timezone", "Asia/Shanghai")
                            or "Asia/Shanghai"))
        dt = datetime.datetime.fromtimestamp(now_ts, zone)
        return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    except Exception:                                           # pragma: no cover - 兜底
        import datetime as _dt
        dt = _dt.datetime.fromtimestamp(now_ts)
        return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


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
        # 官网据此决定是否显示「立即购买」：试用/内部账号是邀请制，不下单。
        "purchasable": bool(row.get("active")
                            and int(row.get("price_cents") or 0) > 0),
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
    name = str(payload.get("name") or code).strip()
    if not name or len(name) > 64 or any(ord(ch) < 32 for ch in name):
        raise errors.bad_request("套餐名字不能为空、不能超过 64 字或含控制字符")
    out: Dict[str, Any] = {"code": code, "name": name}

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


def merge_plan_payload(payload: Dict[str, Any],
                       existing: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把 PUT/POST 的**部分字段**合并到现有套餐，再走 `parse_plan` 校验。

    不合并的话，`PUT {"priceCents": 2800}` 会把天数/额度/设备数全部重置成出厂
    默认值 —— 对 `trial` / `internal` 尤其危险（点一下「改 → 保存」就变成月卡）。
    """
    payload = dict(payload or {})
    if not existing:
        return parse_plan(payload)
    base = plan_view(existing)
    base["sort"] = int(existing.get("sort") or 0)
    base.update(payload)
    base["code"] = str(payload.get("code") or existing.get("code") or "")
    return parse_plan(base)


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
        #: `user_id → (paid 订单重放结果, 取到的时刻)`。订单详情/列表要按订单
        #: 展示“这笔订单带来的到期时间”，重放一次比每行都查一遍便宜。
        #: 支付/退款/管理面调整都会 `forget_entitlement()`，一并失效。
        self._replay_cache: Dict[str, tuple] = {}
        self._replay_ttl_s = 5.0
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

    @property
    def effective_gating(self) -> bool:
        """收费闸门**是否真的会生效**：光开 billing 不够，还必须开客户端鉴权。

        不开鉴权时所有请求都是 `anonymous`，无法映射到 user → 全部走免费老路。
        管理面 overview 会把这个值显示成"计费闸门未生效"，不要让它静默空转。
        """
        return bool(self.enabled and self.cfg.get("auth.enabled", False))

    @property
    def companion_required(self) -> bool:
        """分离/声纹是否必须跟随**同一次 ASR**（2026-11-07，issue #20）。

        **只看鉴权**（`auth.enabled`），**不依赖 `billing.enabled`** —— 否则
        "有鉴权、未开收费"的自建部署会变成完全不校验（评审结论 4）。
        归属由独立的 `client_id → user_id` 解析决定（见 `State.companion_user_id`）：
        绑定账号按 user 共享，未绑定按 client 隔离。
        匿名自托管（auth 关）保持老行为，直接调 diarize 的脚本/冒烟不会被打断。
        """
        return bool(self.cfg.get("auth.enabled", False))

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
        uid = str(user_id or "")
        self._ent_cache.pop(uid, None)
        self._replay_cache.pop(uid, None)

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

    #: 配置键与数据库列名不一致的那两个（历史名字）：`billing.daily_tts_chars`
    #: 才是配置里的键，权益列叫 `tts_chars`。不映射的话 TTS 兜底配置永远不生效。
    _CFG_KEYS = {
        "daily_audio_minutes": "billing.daily_audio_minutes",
        "tts_chars": "billing.daily_tts_chars",
        "qps": "billing.qps",
        "max_devices": "billing.max_devices",
        "max_concurrent": "billing.max_concurrent",
    }

    def _limits(self, ent: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        def pick(key: str, default):
            if ent and ent.get(key) not in (None, ""):
                return ent.get(key)
            return self.cfg.get(self._CFG_KEYS.get(key, "billing.%s" % key), default)

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
        rules = {
            "renewalRule": RENEWAL_RULE,
            "planChangeRule": PLAN_CHANGE_RULE,
            "refundRule": REFUND_RULE,
            "autoRenew": AUTO_RENEW_ENABLED,
        }
        if row is None:
            return {"status": "none", "plan": "", "planName": "", "paidUntil": 0,
                    "dailyMinutes": 0.0, "dailyMinutesLeft": 0.0,
                    "ttsCharsLeft": 0, "qps": 0,
                    "maxDevices": 0, "maxConcurrent": 0, "devices": int(devices),
                    "expiringSoon": False, "renewalDays": 0, **rules}
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
        plan_row = self.store.plan(str(row.get("plan_code") or "")) or {}
        return {
            "status": status,
            "plan": str(row.get("plan_code") or ""),
            "planName": str(plan_row.get("name") or ""),
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
            "renewalDays": int(plan_row.get("days") or 0),
            **rules,
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
        # **fail-closed**：绑定行还在、但账号被停用/删除时**不要**回落免费老路。
        # 那会让"停用账号"看起来生效了（管理面显示停用），实际这台设备继续免费用。
        if user is None:
            raise errors.forbidden("绑定的账号不存在，请联系管理员")
        if str(user.get("status") or "active") != "active":
            raise errors.forbidden("绑定的账号已停用，请联系管理员")
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

        `kind="companion"`（分离/声纹）**跳过日额度，也跳过用户 1 QPS**：
        它们不占每日音频分钟；而且 ASR → diarize 是**紧挨着**发生的合法配套调用，
        ASR 刚吃掉唯一一个令牌，再让 companion 吃同一个桶会把正常流程误杀成 429
        （2026-11-07 评审阻塞点）。防滥用已由"每次 ASR 1+1"、ASR 自身的 1 QPS、
        并发与设备/权益闸门兜住。权益、设备与并发检查仍保留。
        """
        if not self.enabled:
            return None
        user = self.user_for_client(client_id)
        if user is None:
            return None
        uid = str(user["user_id"])
        ent = self.require_entitlement(uid)
        limits = self._limits(ent)
        if kind != "companion":
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

    # ---------------------------------------------------------------- 配套授权

    @staticmethod
    def _companion_owner(user_id: str, client_id: str):
        """授权归属：账号优先，未绑定才用 client（与 `Store._companion_key` 同一口径）。"""
        uid = str(user_id or "")
        return (uid, "" if uid else str(client_id or ""))

    def record_companion(self, user_id: str, client_id: str, digest: str,
                         now: Optional[float] = None) -> int:
        """ASR 成功后发配套授权。返回写入行数；失败**不抛**（不能拖垮已成功的 ASR）。

        写失败时配套调用会因找不到授权而被拒（fail-closed），这比"ASR 返回 500"
        更安全；日志里留一条 error 供排障。
        """
        if not self.companion_required or not digest:
            return 0
        uid, cid = self._companion_owner(user_id, client_id)
        if not (uid or cid):
            return 0
        per = int(self.cfg.get("billing.companion_per_asr", 1) or 0)
        ttl = float(self.cfg.get("billing.companion_ttl_s", 3600) or 0.0)
        try:
            return self.store.put_companion_grant(
                uid, cid, digest, per_asr=per, ttl_s=ttl, now=now)
        except Exception:                                       # pragma: no cover - 兜底
            log.exception("配套授权写库失败（user_id=%s client_id=%s）；"
                          "后面的 diarize/embed 会被拒", uid, cid)
            return 0

    def require_companion_hint(self, user_id: str, client_id: str,
                               now: Optional[float] = None) -> None:
        """读音频前的轻预检：这个归属**有没有任何**未过期的配套授权。

        只回答"要不要先把 body 收下来解码"；具体是不是这段音频、还剩几次，
        由 `claim_companion` 在占通道后原子判定。
        """
        if not self.companion_required:
            return
        uid, cid = self._companion_owner(user_id, client_id)
        if not (uid or cid):
            return
        if not self.store.has_companion_grant(uid, cid, now=now):
            raise errors.companion_not_allowed(
                "这段音频没有可用的 ASR 配套授权：请先对同一段音频做一次 ASR"
                "（或授权已过期）")

    @staticmethod
    def _raise_companion_verdict(verdict: str) -> None:
        """把 `ok` / `missing` / `expired` / `exhausted` 翻成 403 的具体文案。"""
        if verdict == "ok":
            return
        if verdict == "exhausted":
            raise errors.companion_not_allowed(
                "本次 ASR 的配套次数已用完：再对同一段音频做一次 ASR 才能继续")
        if verdict == "expired":
            raise errors.companion_not_allowed(
                "ASR 配套授权已过期：请重新对同一段音频做一次 ASR")
        raise errors.companion_not_allowed(
            "音频与最近一次 ASR 不一致：分离/声纹只认同一次 ASR 的那段音频")

    def check_companion(self, user_id: str, client_id: str, kind: str,
                        digest: str, now: Optional[float] = None) -> None:
        """读音频后的**只读**判定：摘要/次数不匹配先拒，**不扣次数、不记账**。

        这样"音频不一致 / 已用完"会在占通道与旧 client 记账之前被拒；真正扣减仍是
        `claim_companion` 在通道里的原子 UPDATE（防并发）。
        """
        if not self.companion_required:
            return
        uid, cid = self._companion_owner(user_id, client_id)
        if not (uid or cid):
            return
        self._raise_companion_verdict(
            self.store.companion_status(uid, cid, kind, digest, now=now))

    def claim_companion(self, user_id: str, client_id: str, kind: str,
                        digest: str, now: Optional[float] = None) -> None:
        """原子扣一次配套次数；不匹配/过期/用完 → 403 `companion_not_allowed`。

        必须在**拿到推理通道之后**调用：被 409/503 顶回去的请求不消费授权
        （与 `State.billing()` 的"先占通道，再记账"同一个顺序）。
        """
        if not self.companion_required:
            return
        uid, cid = self._companion_owner(user_id, client_id)
        if not (uid or cid):
            return
        self._raise_companion_verdict(self.store.claim_companion_grant(
            uid, cid, kind, digest, now=now))

    def refund_companion(self, user_id: str, client_id: str, kind: str,
                         digest: str, now: Optional[float] = None) -> bool:
        """推理 5xx / 超时 / 进程异常 → 把刚扣掉的配套次数退回。

        4xx 客户端错误**不退**（请求本身不合法，重试也不该成功）；这里只管
        "服务端自己没跑成"的那一档。失败只记日志 —— 退不回比把响应吞掉更好。
        """
        if not self.companion_required:
            return False
        uid, cid = self._companion_owner(user_id, client_id)
        if not (uid or cid) or not digest:
            return False
        per = int(self.cfg.get("billing.companion_per_asr", 1) or 0)
        try:
            return self.store.refund_companion_grant(
                uid, cid, kind, digest, per_asr=per, now=now)
        except Exception:                                       # pragma: no cover - 兜底
            log.exception("配套授权退回失败（user_id=%s client_id=%s kind=%s）",
                          uid, cid, kind)
            return False

    # ---------------------------------------------------------------- 续费计算

    def _renewal_base(self, existing: Optional[Dict[str, Any]],
                      plan: Dict[str, Any], now: float):
        """这次支付从哪一刻起算，以及原因为何（预览与入账共用这一处）。"""
        days = int(plan.get("days") or 0)
        if days <= 0:
            return now, RENEWAL_BASIS_NEVER
        if (existing is not None
                and str(existing.get("status") or "") == "active"
                and str(existing.get("plan_code") or "") != "trial"
                and float(existing.get("paid_until") or 0) > now):
            return float(existing["paid_until"]), RENEWAL_BASIS_PAID_UNTIL
        # 无权益 / 已过期 / 退款后 / 试用中：都从**支付成功时间**起算。
        return now, RENEWAL_BASIS_PAYMENT_TIME

    def _renewal_projection(self, existing: Optional[Dict[str, Any]],
                            plan: Dict[str, Any],
                            now: Optional[float] = None) -> Dict[str, Any]:
        """纯函数：`{current, projected, basis}`（不碰 DB，也不改时间）。

        支付页的“支付后预计到期”必须与真正入账的 `paid_until` 用同一套口径；
        两边都调这里，测试再钉一次“预览 == 实际”。
        """
        now = time.time() if now is None else float(now)
        current = float((existing or {}).get("paid_until") or 0)
        days = int(plan.get("days") or 0)
        if (existing is not None
                and str(existing.get("status") or "") == "active"
                and str(existing.get("plan_code") or "") == "internal"):
            return {"current": 0.0, "projected": 0.0,
                    "basis": RENEWAL_BASIS_NEVER}
        base, basis = self._renewal_base(existing, plan, now)
        projected = (base + days * 86400.0) if days > 0 else 0.0
        return {"current": current, "projected": projected, "basis": basis}

    def _paid_replay(self, user_id: str) -> Dict[str, Any]:
        """按支付时间重放所有 `paid` 订单；退款重算与订单详情共用。

        返回 `{paidUntil, lastPlan, lastOrder, perOrder}`，`perOrder[order_id]`
        是**这笔订单带来**的 `{current, projected, basis}`。没有剩余订单时
        `paidUntil=0`、`lastPlan=None`。
        """
        uid = str(user_id or "")
        now = time.time()
        hit = self._replay_cache.get(uid)
        if hit is not None and (now - hit[1]) < self._replay_ttl_s:
            return hit[0]
        rows = [o for o in self.store.orders(user_id=uid, limit=1000)
                if str(o.get("status") or "") == "paid"]
        rows.sort(key=lambda o: (float(o.get("paid_at") or o.get("created_at") or 0),
                                 str(o.get("order_id") or "")))
        paid_until = 0.0
        last_plan = None
        last_order = ""
        per_order: Dict[str, Dict[str, Any]] = {}
        for order in rows:
            plan = self.store.plan(str(order.get("plan_code") or ""))
            if plan is None:
                continue
            at = float(order.get("paid_at") or order.get("created_at") or 0)
            days = int(plan.get("days") or 0)
            before = paid_until
            if days <= 0:
                after = 0.0
                basis = RENEWAL_BASIS_NEVER
            else:
                after = max(at, before) + days * 86400.0
                basis = (RENEWAL_BASIS_PAID_UNTIL if before > at
                         else RENEWAL_BASIS_PAYMENT_TIME)
            per_order[str(order.get("order_id") or "")] = {
                "current": before, "projected": after, "basis": basis}
            paid_until = after
            last_plan = plan
            last_order = str(order.get("order_id") or "")
        out = {"paidUntil": paid_until, "lastPlan": last_plan,
               "lastOrder": last_order, "perOrder": per_order}
        self._replay_cache[uid] = (out, now)
        return out

    def _order_renewal_view(self, order: Dict[str, Any],
                            now: Optional[float] = None) -> Dict[str, Any]:
        """订单详情里的“当前到期 / 这笔订单之后预计到期 / 起算依据”。"""
        now = time.time() if now is None else float(now)
        status = str(order.get("status") or "")
        uid = str(order.get("user_id") or "")
        plan = self.store.plan(str(order.get("plan_code") or "")) or {}
        current = self.entitlement(uid) or {}
        if status == "paid":
            hit = self._paid_replay(uid)["perOrder"].get(
                str(order.get("order_id") or ""))
            if hit is not None:
                return hit
            # 套餐被删了/老数据：回落到当前权益，不给一个编出来的日期。
            paid_until = float(current.get("paid_until") or 0)
            return {"current": paid_until, "projected": paid_until,
                    "basis": (RENEWAL_BASIS_NEVER if paid_until <= 0
                              else RENEWAL_BASIS_PAID_UNTIL)}
        if status in ("refunded", "closed"):
            return {"current": float(current.get("paid_until") or 0),
                    "projected": 0.0, "basis": RENEWAL_BASIS_NOT_APPLICABLE}
        return self._renewal_projection(current, plan, now)

    def _check_plan_change(self, user_id: str, plan: Dict[str, Any],
                           now: float) -> None:
        """有有效付费权益时只允许同套餐续费；换套餐明确 409，不做 proration。"""
        existing = self.entitlement(user_id)
        if existing is None or str(existing.get("status") or "") != "active":
            return
        code = str(existing.get("plan_code") or "")
        if code == "internal":
            raise errors.plan_change_unsupported(
                "内部账号不参与付费套餐",
                message="当前是内部账号，无需购买套餐")
        if code == "trial":
            return                      # 试用不是付费权益，买月卡不算“换套餐”
        if float(existing.get("paid_until") or 0) <= now:
            return                      # 已经过期：按新购处理，允许换任意套餐
        wanted = str(plan.get("code") or "")
        if code == wanted:
            return
        current_name = str((self.store.plan(code) or {}).get("name") or code or "当前套餐")
        raise errors.plan_change_unsupported(
            "当前套餐是「%s」，暂不支持升级/降级；请等它到期后再换，或联系管理员"
            % current_name)

    # ---------------------------------------------------------------- 订单

    def _order_pay_view(self, order: Dict[str, Any], *, create: bool = False,
                        with_pair_url: bool = True) -> Dict[str, Any]:
        meta = _unjson(order.get("meta"))
        plan = self.store.plan(str(order.get("plan_code") or "")) or {}
        view = {
            "orderId": str(order.get("order_id") or ""),
            "planCode": str(order.get("plan_code") or ""),
            # 支付页只读服务端：套餐名与天数也由这里给，不在前端写死。
            "planName": str(plan.get("name") or ""),
            "days": int(plan.get("days") or 0),
            "amountCents": int(order.get("amount_cents") or 0),
            "status": str(order.get("status") or ""),
            "createdAt": float(order.get("created_at") or 0),
            "expiresAt": float(order.get("expires_at") or 0),
            "paidAt": float(order.get("paid_at") or 0),
            "payUrl": str(meta.get("payUrl") or ""),
            "qrCode": str(meta.get("qrCode") or ""),
            # 只在支付宝沙箱下为 True；支付页据此显示"测试环境"标注。
            # 切生产（`alipay.sandbox=false`）后标注自动消失，页面文案不用改。
            "sandbox": bool(self.provider == "alipay"
                            and getattr(self.gateway, "sandbox", False)),
            # 支付前就能看见“这次付完什么时候到期”。预览与入账共用同一套口径。
            "renewalDays": int(plan.get("days") or 0),
            "renewalRule": RENEWAL_RULE,
            "planChangeRule": PLAN_CHANGE_RULE,
            "refundRule": REFUND_RULE,
            "autoRenew": AUTO_RENEW_ENABLED,
        }
        renewal = self._order_renewal_view(order)
        view.update({
            "currentPaidUntil": float(renewal.get("current") or 0.0),
            "projectedPaidUntil": float(renewal.get("projected") or 0.0),
            "renewalBasis": str(renewal.get("basis") or ""),
        })
        if create:
            view["mock"] = bool(meta.get("mock"))
        # **管理面列表必须只读**：`pairUrl` 可能触发补发码（写库 + 审计），
        # 不能因为管理员刷新一下订单列表就写。客户端轮询路径才带 `pairUrl`。
        if view["status"] == "paid" and with_pair_url:
            view["pairUrl"] = self._pair_url_locked(order)
        return view

    def order_view(self, order: Dict[str, Any], *,
                   with_pair_url: bool = True) -> Dict[str, Any]:
        return self._order_pay_view(order, with_pair_url=with_pair_url)

    def create_order(self, user_id: str, plan_code: str, *, client_id: str = ""
                     ) -> Dict[str, Any]:
        if not self.enabled:
            raise errors.billing_disabled()
        plan = self.plan(plan_code)
        if int(plan.get("price_cents") or 0) <= 0:
            raise errors.bad_request("这个套餐不需要下单（试用由系统赠送）")
        # **换套餐在落单之前拒绝**：不做 proration，也不留一个永远 closed 的孤儿订单。
        self._check_plan_change(str(user_id), plan, time.time())
        if not getattr(self.gateway, "configured", False):
            # **先判支付配置，再落订单**：未配置时不要留一堆永远 `closed` 的孤儿订单。
            raise errors.payment_unconfigured()
        now = time.time()
        # `billing.order_ttl_s` 的执行者：先关掉过期的未支付订单。
        try:
            self.store.close_expired_orders(now)
        except Exception:                                       # pragma: no cover - 兜底
            pass
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

    def orders_for_user(self, user_id: str, *, limit: int = 20
                        ) -> List[Dict[str, Any]]:
        """当前用户的最近订单（「我的」页列表）。

        **不带 pairUrl**：列表是只读展示，不能因为刷新一下列表就补发码/写库。
        """
        rows = self.store.orders(user_id=str(user_id or ""), limit=limit)
        return [self._order_pay_view(row, with_pair_url=False) for row in rows]

    def cancel_order(self, order_id: str, user_id: str) -> Dict[str, Any]:
        order = self.order_for_user(order_id, user_id)
        status = str(order.get("status") or "")
        if status not in ("created", "paying"):
            raise errors.order_state_conflict(status, "created/paying")
        self.store.update_order(order["order_id"], status="closed")
        self.store.insert_order_event(order["order_id"], "cancelled", "")
        return {"ok": True, "orderId": order["order_id"], "status": "closed"}

    def sync_order(self, order_id: str, user_id: str, *,
                   with_pair_url: bool = True) -> Dict[str, Any]:
        """本地联调/回调丢失兜底：主动向支付宝查询并补记支付结果。

        只对 `created|paying` 的订单查询；终态订单直接回存库视图，不重复入账。
        `TRADE_CLOSED` 是唯一会把订单变 `closed` 的查询结果；`paid` 走与异步通知
        完全相同的 `apply_notify()`（幂等、金额校验、权益/发码都在那里）。

        `with_pair_url=False` 给管理面用：列表/详情刷新**不能**因为读一下
        就补发配对码（写库 + 审计）。用户侧那条路保持默认 True。
        """
        order = self.order_for_user(order_id, user_id)
        status = str(order.get("status") or "")
        if status in ("paid", "refunded", "closed"):
            return self.order_view(order, with_pair_url=with_pair_url)
        if not getattr(self.gateway, "configured", False):
            raise errors.payment_unconfigured()
        norm = self.gateway.query(order)
        got_order = str(norm.get("outTradeNo") or "")
        if got_order and got_order != str(order.get("order_id") or ""):
            # 查询是按 out_trade_no 发起的，回来却是别的单：宁可不入账。
            raise errors.payment_gateway_error("支付宝查询返回的订单号不匹配")
        result = str(norm.get("status") or "")
        if result == "paid":
            norm = dict(norm)
            norm["verified"] = True
            self.apply_notify(norm)
            order = self.store.order(str(order.get("order_id") or "")) or order
        elif result == "closed":
            # 查询结果可能比回调慢：期间已经支付/退款的订单**不许**被改成 closed。
            fresh = self.store.order(str(order.get("order_id") or "")) or order
            if str(fresh.get("status") or "") in ("created", "paying"):
                self.store.update_order(str(order.get("order_id") or ""),
                                        status="closed")
                self.store.insert_order_event(
                    str(order.get("order_id") or ""), "sync-closed",
                    _json({"rawStatus": str(norm.get("rawStatus") or "")}))
                fresh = self.store.order(str(order.get("order_id") or "")) or fresh
            order = fresh
        return self.order_view(order, with_pair_url=with_pair_url)

    # ---------------------------------------------------------------- 支付回调

    def _entitlement_row(self, existing: Optional[Dict[str, Any]],
                         plan: Dict[str, Any], order: Dict[str, Any],
                         now: float) -> Dict[str, Any]:
        """纯函数：由"现有权益 + 这次套餐"算新权益行（不碰 DB，事务内可安全调用）。"""
        uid = str(order.get("user_id") or "")
        if (existing is not None
                and str(existing.get("plan_code") or "") == "internal"
                and str(existing.get("status") or "") == "active"):
            return dict(existing)          # internal 不被付费套餐降级
        # **试用不叠加**；期内从 paid_until 顺延，其余从支付时间起算。
        base, _basis = self._renewal_base(existing, plan, now)
        days = int(plan.get("days") or 0)
        return {
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

    def _record_buyer(self, order: Dict[str, Any], norm: Dict[str, Any]) -> None:
        """支付身份回填（设计 §3.5.11）。只在该 user 还没有 identity 时填。"""
        buyer = str(norm.get("buyerId") or "")
        uid = str(order.get("user_id") or "")
        if not buyer or not uid:
            return
        user = self.store.user(uid)
        if user is not None and not str(user.get("alipay_open_id") or ""):
            try:
                self.store.update_user(uid, alipay_open_id=buyer)
            except Exception:                                   # pragma: no cover - 兜底
                pass

    def _duplicate_paid(self, order: Dict[str, Any], trade_no: str,
                        norm: Dict[str, Any], now: float) -> Dict[str, Any]:
        """已 `paid` 订单的重复回调：**自愈**没落完的权益/配对码，再返回 duplicate。"""
        if str(order.get("alipay_trade_no") or "") != str(trade_no):
            raise errors.order_state_conflict(str(order.get("status") or ""),
                                              "未支付订单")
        meta = _unjson(order.get("meta"))
        uid = str(order.get("user_id") or "")
        entitlement = self.store.entitlement(uid)
        # 本单是否已经落过权益：新代码看 `meta.fulfilled`；老数据没有这个标记，
        # 用 `entitlements.order_id == 本单` 或"后面还有已支付订单"推断（后者说明
        # 这笔已经叠进后续的 paid_until，不能重复加）。
        needs_repair = (not bool(meta.get("fulfilled"))) or entitlement is None
        if needs_repair and entitlement is not None:
            if str(entitlement.get("order_id") or "") == str(order.get("order_id") or ""):
                needs_repair = False
            else:
                paid_at = float(order.get("paid_at") or order.get("created_at") or 0)
                newer = [o for o in self.store.orders(user_id=uid, limit=1000)
                         if str(o.get("status") or "") == "paid"
                         and float(o.get("paid_at") or 0) > paid_at]
                if newer:
                    needs_repair = False
        if needs_repair:
            plan = self.store.plan(str(order.get("plan_code") or ""))
            if plan is None:
                raise errors.plan_not_found(str(order.get("plan_code") or ""))
            out = self.store.repair_paid_order(
                str(order.get("order_id") or ""), now, plan,
                lambda existing, plan: self._entitlement_row(existing, plan, order, now))
            order = out.get("order") or order
            self.forget_entitlement(str(order.get("user_id") or ""))
        self._record_buyer(order, norm)
        # 码是**尽力而为**的：发码失败不影响已经落好的权益；重复回调会再试。
        if self.store.count_devices(str(order.get("user_id") or "")) == 0:
            self._ensure_pair_code(order)
        return {"ok": True, "orderId": str(order.get("order_id") or ""),
                "status": "paid", "duplicate": True}

    def apply_notify(self, norm: Dict[str, Any]) -> Dict[str, Any]:
        """已验签/已归一化的支付结果 → 订单 + 权益。**幂等、原子、可自愈**。

        成功路径：`alipay_notify`（唯一键）+ 订单状态 + 权益 + 事件在**同一事务**；
        任何一步失败都回滚，订单保持 `paying`，支付宝重投可以再来一次。
        发码在事务之外（失败不影响权益；重复回调会补发）。
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
        raw = json.dumps(norm, ensure_ascii=False)
        verified = bool(norm.get("verified", True))
        if status != "paid":
            # 非成功状态只留痕（关闭/失败），不改权益。
            first = self.store.record_notify(trade_no, order_id, raw=raw,
                                             verified=verified, status=status)
            self.store.insert_order_event(order_id, "notify-" + (status or "unknown"), "")
            return {"ok": True, "orderId": order_id, "status": "ignored",
                    "duplicate": not first}

        now = time.time()
        current = str(order.get("status") or "")
        if current == "paid":
            return self._duplicate_paid(order, trade_no, norm, now)
        if current not in ("created", "paying"):
            # **迟到支付也要留痕**：订单已 closed/refunded 时回调不能入账，但必须让
            # 管理面看得到"钱可能收了、单已关"；否则只能靠支付宝后台人工对账。
            self.store.record_notify(trade_no, order_id, raw=raw, verified=verified,
                                     status="conflict", overwrite=True)
            raise errors.order_state_conflict(current, "created/paying")
        plan = self.store.plan(str(order.get("plan_code") or ""))
        if plan is None:
            # 在事务**之前**失败：不回滚出"paid 没权益"，订单保持 paying，可重试。
            raise errors.plan_not_found(str(order.get("plan_code") or ""))
        out = self.store.settle_paid_order(
            order_id, trade_no, raw, verified, now, plan,
            lambda existing, plan: self._entitlement_row(existing, plan, order, now))
        if out.get("duplicate"):
            fresh = self.store.order(order_id) or out.get("order") or order
            return self._duplicate_paid(fresh, trade_no, norm, now)
        if out.get("retry"):
            fresh = self.store.order(order_id) or out.get("order") or order
            if str(fresh.get("status") or "") == "paid":
                return self._duplicate_paid(fresh, trade_no, norm, now)
            raise errors.order_state_conflict(str(fresh.get("status") or ""),
                                              "created/paying")
        if not out.get("ok"):
            raise errors.order_state_conflict(str(out.get("conflict") or ""),
                                              "created/paying")
        order = out.get("order") or self.store.order(order_id) or order
        self.forget_entitlement(str(order.get("user_id") or ""))
        self._record_buyer(order, norm)
        # 第一次支付且还没有设备 → 发配对码；已有设备 = 续费，不发码（设计 §4.5）。
        if self.store.count_devices(str(order.get("user_id") or "")) == 0:
            self._ensure_pair_code(order)
        return {"ok": True, "orderId": order_id, "status": "paid", "duplicate": False}

    def extend_entitlement(self, order: Dict[str, Any], *, now: Optional[float] = None
                           ) -> Dict[str, Any]:
        """把这次支付的天数加到权益上（公开入口；内部支付路径走事务版）。"""
        now = time.time() if now is None else float(now)
        plan = self.store.plan(str(order.get("plan_code") or ""))
        if plan is None:
            raise errors.plan_not_found(str(order.get("plan_code") or ""))
        row = self._entitlement_row(self.entitlement(str(order.get("user_id") or "")),
                                    plan, order, now)
        self.store.upsert_entitlement(row)
        self.forget_entitlement(str(order.get("user_id") or ""))
        self.store.insert_order_event(str(order.get("order_id") or ""), "entitlement",
                                      _json({"plan": row.get("plan_code"),
                                             "paidUntil": row.get("paid_until")}))
        return row

    def recompute_entitlement(self, user_id: str, *, now: Optional[float] = None
                              ) -> Optional[Dict[str, Any]]:
        """按**未退款的已支付订单**重算权益（退款后不能让剩余天数一起失效）。

        叠加重算规则与支付路径一致：按 `paid_at` 顺序，每笔从
        `max(支付时刻, 当前 paid_until)` 起加套餐天数。没有剩余订单 → 状态 refunded。
        """
        uid = str(user_id or "")
        now = time.time() if now is None else float(now)
        existing = self.store.entitlement(uid)
        if (existing is not None
                and str(existing.get("plan_code") or "") == "internal"
                and str(existing.get("status") or "") == "active"):
            return existing
        replay = self._paid_replay(uid)
        paid_until = float(replay.get("paidUntil") or 0)
        last_plan = replay.get("lastPlan")
        last_order = str(replay.get("lastOrder") or "")
        if last_plan is None:
            # 没有剩余 paid 订单：状态退回 refunded，且**到期时间清零**。
            # 不保留一个“已退款但到期日在未来”的行，那会让权益卡自相矛盾。
            if existing is not None:
                row = dict(existing)
                row["paid_until"] = 0.0
                row["status"] = "refunded"
                row["updated_at"] = now
                self.store.upsert_entitlement(row)
            self.forget_entitlement(uid)
            return self.store.entitlement(uid)
        row = {
            "user_id": uid,
            "plan_code": str(last_plan.get("code") or ""),
            "paid_until": paid_until,
            "daily_audio_minutes": float(last_plan.get("daily_audio_minutes") or 0),
            "tts_chars": int(last_plan.get("tts_chars") or 0),
            "qps": int(last_plan.get("qps") or 1),
            "max_devices": int(last_plan.get("max_devices") or 3),
            "max_concurrent": int(last_plan.get("max_concurrent") or 1),
            "status": "active",
            "order_id": last_order,
            "updated_at": now,
        }
        self.store.upsert_entitlement(row)
        self.forget_entitlement(uid)
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

    def _prepare_pair_code(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """纯计算一张新配对码（不写库）。返回 code / hash / nonce / url。"""
        order_id = str(order.get("order_id") or "")
        user_id = str(order.get("user_id") or "")
        plan = self.store.plan(str(order.get("plan_code") or ""))
        name = str((plan or {}).get("name") or "ECHO 后端")
        ttl = float(self.cfg.get("billing.pair_code_ttl_s", 900) or 900)
        nonce = secrets.token_hex(8)
        code = self._derive_pair_code(order_id, nonce)
        code_id = hash_pairing_code(code)
        code_row = {
            "code_hash": code_id, "ttl_s": ttl,
            "created_by": "order:" + order_id, "name": name,
            "scopes": str(self.cfg.get("billing.client_scopes", "") or ""),
            "client_id": "", "user_id": user_id, "kind": "purchase",
        }
        return {"code": code, "code_id": code_id, "nonce": nonce,
                "code_row": code_row,
                "url": self._pair_url_of(order_id, nonce, code)}

    def _pair_url_of(self, order_id: str, nonce: str, code: str) -> str:
        ttl = float(self.cfg.get("billing.pair_code_ttl_s", 900) or 900)
        built = ops.pairing_string(self.cfg, code, ttl_s=ttl)
        return str(built.get("url") or "")

    def _pair_url_current(self, order: Dict[str, Any]) -> str:
        """**只读**：当前订单若有一张仍然有效的码，算出它的 URL；否则空串。"""
        if str(order.get("status") or "") != "paid":
            return ""
        if self.store.count_devices(str(order.get("user_id") or "")) > 0:
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
        return ""

    def _ensure_pair_code(self, order: Dict[str, Any]) -> str:
        """确保订单有一张可用码；**CAS** 保证并发轮询只装一张。失败返回空串。"""
        order_id = str(order.get("order_id") or "")
        for _ in range(3):
            current = self.store.order(order_id) or order
            # 续费/已有设备：**不发码**（`_pair_url_current` 也会返回空，但这里先退出
            # 才不会误进"补发"分支然后写库）。
            if self.store.count_devices(str(current.get("user_id") or "")) > 0:
                return ""
            url = self._pair_url_current(current)
            if url:
                return url
            try:
                prepared = self._prepare_pair_code(current)
            except Exception as exc:                            # 发码失败不影响权益
                try:
                    self.store.insert_order_event(
                        order_id, "pair-code-failed",
                        _json({"reason": type(exc).__name__}))
                except Exception:
                    pass
                return ""
            out = self.store.install_pair_code(
                order_id, str(current.get("pairing_code_id") or ""),
                prepared["code_row"], {"pairNonce": prepared["nonce"]})
            if out.get("ok"):
                return str(prepared["url"])
            if out.get("valid"):
                return self._pair_url_current(out.get("order") or current)
            if out.get("raced"):
                continue
            return ""
        return ""

    def issue_purchase_code(self, order: Dict[str, Any]) -> Optional[str]:
        """兼容入口：确保订单有一张配对码（幂等 + CAS）。"""
        return self._ensure_pair_code(order) or None

    def _pair_url_locked(self, order: Dict[str, Any]) -> str:
        """已支付订单的 `pairUrl`（客户端轮询路径；必要时补发，CAS 不会出两张）。"""
        return self._ensure_pair_code(order)

    def refund_order(self, order_id: str, reason: str = "") -> Dict[str, Any]:
        """退款：调网关退款 + 订单变 `refunded` + **按剩余订单重算权益**。

        多笔叠加时退其中一笔不能再把剩余天数一起停掉；只剩退款单时才失效。
        """
        order = self.store.order(str(order_id or ""))
        if order is None:
            raise errors.order_not_found(order_id)
        if str(order.get("status") or "") != "paid":
            raise errors.order_state_conflict(str(order.get("status") or ""), "paid")
        out = self.gateway.refund(order)
        self.store.update_order(order_id, status="refunded")
        uid = str(order.get("user_id") or "")
        # 先清重放缓存再重算：否则 5 秒缓存里还可能留着“这单仍是 paid”的旧结果。
        self.forget_entitlement(uid)
        self.recompute_entitlement(uid)
        self.forget_entitlement(uid)
        self.store.insert_order_event(
            order_id, "refunded",
            _json({"gateway": out, "reason": str(reason or "")}))
        return {"ok": True, "orderId": order_id, "status": "refunded",
                "entitlement": self.entitlement(uid)}

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
        # 设计 §5.4：统计口径是 `billing.timezone` 的**自然日**，不是滚动 24h。
        day_start_ts = day_start(self.cfg, now)
        orders = self.store.orders(limit=1000)
        paid = [o for o in orders if str(o.get("status") or "") == "paid"]
        revenue_today = sum(int(o.get("amount_cents") or 0) for o in paid
                            if float(o.get("paid_at") or 0) >= day_start_ts)
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
    # 站点据此显示"测试环境"标注 / 支付是否可用；**不含任何密钥或路径**。
    payment = {
        "provider": svc.provider,
        "configured": bool(getattr(svc.gateway, "configured", False)),
        "sandbox": bool(svc.provider == "alipay"
                        and getattr(svc.gateway, "sandbox", False)),
    }
    return {"plans": plans, "trial": trial, "payment": payment,
            "serverTime": time.time()}


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


@router.get("/orders")
def list_orders(request: Request, limit: int = 20):
    """当前账号的最近订单（「我的」页只读列表；不含 pairUrl）。"""
    svc = _billing(request)
    if not svc.enabled:
        raise errors.billing_disabled()
    user = _user(request)
    return {"orders": svc.orders_for_user(str(user["user_id"]), limit=limit)}


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


@router.post("/orders/{order_id}/sync")
def sync_order(request: Request, order_id: str):
    """主动查询支付宝并同步订单状态（本地联调、回调丢失兜底）。"""
    svc = _billing(request)
    user = _user(request)
    return svc.sync_order(order_id, str(user["user_id"]))


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
