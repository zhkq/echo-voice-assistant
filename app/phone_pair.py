# -*- coding: utf-8 -*-
"""phone_pair.py — 手机 / 手表触点的**配对码**：一次性、有寿命、错多了就锁。

为什么不是"把令牌直接塞进二维码"（原设计 §3 的写法，2026-10-09 改）
------------------------------------------------------------------
原设计让 `POST /api/pair/phone` 直接回一把明文令牌，二维码里也带它。两个问题：

  ① **二维码 / 截图 = 凭据**：谁拍到那张图，谁就拿到一把**没有寿命**的令牌；
  ② **手表没有摄像头**（HUAWEI WATCH 4 Pro，见 docs/手表触点-可行性分析-Watch4Pro.md §4-1），
     "扫码"这条唯一入口直接不存在。

改成"**短码 → 兑换**"两步，两个问题一起解决：面板显示 6 位码（人能读、能用表冠/键盘输入），
设备拿码去 `POST /api/pair/phone/claim` 换令牌。码只有 5 分钟寿命、**用掉即删**、
错多了锁一段时间。令牌那一刻才生成，且**只回给兑换的那个人**。

码存哪：**只在内存里**。它是个"5 分钟内有效的引导凭据"，落库等于多一个泄露面
（库文件是用户会备份/同步的东西）。代价如实说：**ECHO 重启后未兑换的码失效**，
用户重新点一次即可。

防爆破（6 位数字 = 10⁶ 个可能，必须有闸）
----------------------------------------
* 寿命 5 分钟（`TTL_S`）；
* 单张码**用掉即删**（不是"标记已用"）；
* 兑换失败累计 `MAX_FAILURES` 次 → 锁 `LOCK_S` 秒（全局锁，不是按下发者各算一份：
  我们只有一张码在有效期内，锁全局就是这个场景下的正确粒度）；
* 5 分钟 × 20 次尝试 → 命中概率约 2e-5。这条闸**不能省** ——
  没有它，6 位码在局域网上是可以暴力试穿的。
"""
from __future__ import annotations

import secrets
import threading
import time

#: 配对码寿命（秒）。用户要在手机上从"看到码"到"输完"走完，5 分钟够用；
#: 越短越安全，但不能短到"还没输完就过期"。
TTL_S = 300

#: 允许的连续失败次数 / 触发锁的时长（秒）
MAX_FAILURES = 20
LOCK_S = 300

#: 位数（6 位数字；不要改小 —— 空间每差一位安全差 10 倍）
DIGITS = 6

_lock = threading.Lock()
_state = {"code": "", "expires": 0.0, "failures": []}


def _cleanup(now: float) -> None:
    """清掉过期码与过窗的失败记录（调用方必须持锁）。"""
    if _state["code"] and now >= _state["expires"]:
        _state["code"] = ""
        _state["expires"] = 0.0
    _state["failures"] = [t for t in _state["failures"] if now - t < LOCK_S]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def locked_for() -> int:
    """还要锁多少秒（0 = 没锁）。"""
    with _lock:
        now = time.time()
        _cleanup(now)
        if len(_state["failures"]) < MAX_FAILURES:
            return 0
        return int(LOCK_S - (now - min(_state["failures"]))) + 1


def issue(ttl: int = TTL_S) -> dict:
    """生成一张新码（**覆盖**尚未兑换的旧码 —— 用户点两次"生成"就该只有最新那张有效）。"""
    with _lock:
        now = time.time()
        _cleanup(now)
        code = "%0*d" % (DIGITS, secrets.randbelow(10 ** DIGITS))
        _state["code"] = code
        _state["expires"] = now + float(ttl)
        return {"code": code, "expiresAt": _iso(_state["expires"]), "ttlSeconds": int(ttl)}


def pending() -> dict:
    """当前待兑换的码（面板要显示它）。没有就返回空 dict。"""
    with _lock:
        now = time.time()
        _cleanup(now)
        if not _state["code"]:
            return {}
        return {"code": _state["code"],
                "expiresAt": _iso(_state["expires"]),
                "secondsLeft": max(0, int(_state["expires"] - now))}


def claim(code) -> tuple:
    """兑换一张码：返回 ``(ok, 原因)``。**成功即删码**（兑换一次就作废）。

    原因串是给人看的 —— 面板/App 直接显示它，所以要说清"是过期、是错、还是被锁了"。
    """
    given = "".join(ch for ch in str(code or "") if ch.isdigit())
    with _lock:
        now = time.time()
        _cleanup(now)
        if len(_state["failures"]) >= MAX_FAILURES:
            return False, "尝试过于频繁，请等几分钟再试（或回面板重新生成配对码）"
        if not _state["code"]:
            return False, "配对码已过期或不存在，请在 ECHO 面板上重新生成"
        if len(given) != DIGITS:
            _state["failures"].append(now)
            return False, "配对码格式不对（应为 %d 位数字）" % DIGITS
        if not secrets.compare_digest(given, _state["code"]):
            _state["failures"].append(now)
            return False, "配对码不对"
        _state["code"] = ""                 # 用掉即删
        _state["expires"] = 0.0
        return True, ""


def reset() -> None:
    """清空全部状态（**只给测试与"用户主动取消配对"用**）。"""
    with _lock:
        _state["code"] = ""
        _state["expires"] = 0.0
        _state["failures"] = []
