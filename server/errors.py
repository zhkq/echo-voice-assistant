# -*- coding: utf-8 -*-
"""机器可读的错误模型。

**为什么每个失败都要有自己的 code**：客户端要按**原因**决定下一步 ——
"稍后重试"、"今天别再试"、"换下一个后端"、"改分段"是四件完全不同的事。
全部回一个 429/503 的话，客户端只能盲目重试；而其中有些重试**永远不会成功**
（例如自己上一个请求还没完 —— 见 `client_busy`）。

契约见 `docs/ECHO能力后端-服务端设计.md` §6.3；客户端侧的映射见同文档 §3.6。
"""
from __future__ import annotations

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


def client_busy() -> EchoError:
    """该客户端自己已有请求在跑。**重试永远不会成功** —— 客户端该等自己那条回来。"""
    return EchoError(409, "client_busy",
                     "你已有一个请求正在处理，请等它结束", detail="每客户端同时只允许 1 个")


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
