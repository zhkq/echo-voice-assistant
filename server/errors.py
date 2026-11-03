# -*- coding: utf-8 -*-
"""机器可读的错误模型。

**为什么每个失败都要有自己的 code**：客户端要按**原因**决定下一步 ——
"稍后重试"、"今天别再试"、"换下一个后端"、"改分段"是四件完全不同的事。
全部回一个 429/503 的话，客户端只能盲目重试；而其中有些重试**永远不会成功**
（例如自己上一个请求还没完 —— 见 `client_busy`）。

契约见 `docs/ECHO能力后端-服务端设计.md` §6.3；客户端侧的映射见同文档 §3.6。
"""
from __future__ import annotations

import time
from typing import Optional


class EchoError(Exception):
    """带 HTTP 状态 + 稳定 code + 可选 Retry-After 的错误。

    `message` 是**给人看的**（客户端面板会直接显示），`code` 是**给程序看的**。
    """

    def __init__(self, status: int, code: str, message: str,
                 retry_after: Optional[int] = None, detail: str = ""):
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.detail = detail

    def body(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.retry_after is not None:
            out["retryAfter"] = int(self.retry_after)
        if self.detail:
            out["detail"] = self.detail
        return out


# ---------------------------------------------------------------- 具体错误
#
# 名字即契约：这些 code 会被客户端**逐条映射**成不同的处理方式，
# 所以**不许合并、不许改名**（改名 = 客户端静默走错分支）。

def bad_request(detail: str = "") -> EchoError:
    return EchoError(400, "bad_request", "请求不合法", detail=detail)


def unauthorized(detail: str = "") -> EchoError:
    return EchoError(401, "unauthorized", "未授权或凭据无效", detail=detail)


def forbidden(detail: str = "") -> EchoError:
    return EchoError(403, "forbidden", "该凭据没有这项能力的权限", detail=detail)


def client_busy(detail: str = "") -> EchoError:
    """该客户端自己已有请求在跑。**重试永远不会成功** —— 客户端该等自己那条回来。

    `detail` 用来区分"这台设备自己"与"同一个账号的另一台设备"（收费后按 user 的
    并发闸门，见 `docs/后端收费-交付给服务器dsh/01-后端收费与自动配对-设计.md` §4.4）：code/status 仍然相同。
    """
    return EchoError(409, "client_busy",
                     "你已有一个请求正在处理，请等它结束",
                     detail=detail or "每客户端同时只允许 1 个")


def payload_too_large(limit: int) -> EchoError:
    return EchoError(413, "payload_too_large", "上传内容过大",
                     detail="上限 %d 字节" % int(limit))


def audio_too_long(limit: float) -> EchoError:
    return EchoError(413, "audio_too_long", "音频过长", detail="上限 %.0f 秒" % float(limit))


def unsupported_media(detail: str = "") -> EchoError:
    return EchoError(415, "unsupported_media", "不支持的音频格式", detail=detail)


def quota_exceeded(retry_after: int) -> EchoError:
    """当日额度用完。**今天别再试** —— 重试只是白烧额度。"""
    return EchoError(429, "quota_exceeded", "当日额度已用完", retry_after=retry_after)


def rate_limited(retry_after: int, detail: str = "") -> EchoError:
    """被限速（目前只用于 `/v1/pair` 的失败退避）。

    **与 `quota_exceeded` 分开**：那个是"额度用完了"，这个是"你刚试错太多次"。
    客户端对前者的正确反应是"今天别再试"，对后者是"等几秒再试一次" ——
    合成一个 code 就只能盲目退避。
    """
    return EchoError(429, "rate_limited", "尝试过于频繁，请稍后再试",
                     retry_after=retry_after, detail=detail)


def auth_misconfigured(detail: str = "") -> EchoError:
    """服务端自己的鉴权配置不对（例如 `mode=jwt` 却没配密钥）。

    **不是 401**：401 是"你的凭据不对"，这个是"我这边没配好"——
    客户端不该去翻自己的配置，运营该来看服务端日志。
    """
    return EchoError(503, "auth_misconfigured", "服务端鉴权未正确配置", detail=detail)


def server_busy(retry_after: int) -> EchoError:
    """服务端通道满。**系统忙，稍后再试** —— 这条才该退避重试。"""
    return EchoError(503, "server_busy", "系统忙，请稍后再试", retry_after=retry_after)


def model_loading(retry_after: int) -> EchoError:
    return EchoError(503, "model_loading", "模型正在加载，请稍后再试", retry_after=retry_after)


def model_not_found(model_id: str) -> EchoError:
    return EchoError(404, "model_not_found", "没有这个模型", detail=model_id)


def model_failed(detail: str = "") -> EchoError:
    """加载失败。**不静默回退 CPU** —— 那会拖垮所有客户端（设计 §3.4）。"""
    return EchoError(503, "model_failed", "模型不可用", detail=detail)


def gpu_oom(detail: str = "") -> EchoError:
    return EchoError(503, "gpu_oom", "显存不足", detail=detail)


def inference_timeout(seconds: float) -> EchoError:
    return EchoError(504, "inference_timeout", "处理超时",
                     detail="超过 %.0f 秒" % float(seconds))


# ---------------------------------------------------------------- 收费（2026-11-03）
#
# 同样"名字即契约"：客户端按 code 决定"去开通 / 去续费 / 等一会 / 先解绑"，
# 所以下面每个分支都必须有自己的 code，**不许合并成一个 402**。

def entitlement_missing() -> EchoError:
    """没开通（从来没有过权益）。客户端该给的按钮是「去开通」。"""
    return EchoError(402, "entitlement_missing", "这个账号还没有开通套餐",
                     detail="购买套餐后再用这台后端")


def entitlement_expired(paid_until: float = 0) -> EchoError:
    """套餐到期。**配对与数据都保留**，续费即恢复（设计 §4.4 的到期策略）。

    与 `entitlement_missing` 分开：客户端对"去续费"与"去开通"给的是不同的按钮。
    """
    return EchoError(402, "entitlement_expired", "套餐已到期，续费后恢复",
                     detail=("有效期至 %s" % time.strftime("%Y-%m-%d", time.localtime(paid_until))
                             if paid_until else "已过期"))


def entitlement_inactive(status: str = "") -> EchoError:
    """权益被冻结 / 已退款（管理面动作）。**立即失效**（设计 §4.4）。"""
    return EchoError(402, "entitlement_inactive", "套餐已停用，请联系管理员",
                     detail=str(status or "inactive"))


def billing_disabled() -> EchoError:
    """这台后端没开收费功能。客户端按"没有购买入口"静默降级（设计 §5.3）。

    404 而不是 403/503：旧客户端根本不该看到这个功能；新客户端按 404 隐藏入口。
    """
    return EchoError(404, "billing_disabled", "这台后端没有开通收费功能")


def plan_not_found(code: str = "") -> EchoError:
    return EchoError(404, "plan_not_found", "没有这个套餐", detail=str(code or ""))


def order_not_found(order_id: str = "") -> EchoError:
    return EchoError(404, "order_not_found", "没有这个订单", detail=str(order_id or ""))


def user_not_found(user_id: str = "") -> EchoError:
    return EchoError(404, "user_not_found", "没有这个账号", detail=str(user_id or ""))


def order_state_conflict(status: str = "", wanted: str = "") -> EchoError:
    """订单状态机只许前进（设计 §4.6）。当前状态不允许这个动作。"""
    return EchoError(409, "order_state_conflict", "订单当前状态不允许这个操作",
                     detail="当前 %s，需要 %s" % (status or "?", wanted or "?"))


def invite_invalid(reason: str = "") -> EchoError:
    """邀请码不存在 / 过期 / 次数用满。**统一一个 code**：别让人拿它枚举码。"""
    return EchoError(403, "invite_invalid", "邀请码无效或已用完",
                     detail=str(reason or ""))


def employee_no_taken() -> EchoError:
    return EchoError(409, "employee_no_taken", "这个工号已经注册过")


def device_limit_reached(limit: int) -> EchoError:
    """设备数到上限（设计 §3.5.10）。客户端该引导"先解绑旧设备"。"""
    return EchoError(403, "device_limit_reached",
                     "设备数已满（上限 %d 台），请先解绑旧设备" % int(limit),
                     detail="管理面 / 面板都可以解绑")


def device_revoked() -> EchoError:
    """这台设备已被解绑（换机/找回）。**403 而不是 401**：认得出它，但不许用。"""
    return EchoError(403, "device_revoked", "这台设备已被解绑，请重新绑定")


def payment_unconfigured() -> EchoError:
    """支付没配好（或本轮只有 Mock、真实网关未接入）。

    **503 而不是启动失败**（任务书 §5.2）：没配支付时服务必须能起来，
    只是下单接口明确说"支付未配置"；**绝不静默降级成 Mock**。
    """
    return EchoError(503, "payment_unconfigured", "支付未配置",
                     detail="请配置 payment.provider 与对应网关参数")
