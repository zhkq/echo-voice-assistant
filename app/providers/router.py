# -*- coding: utf-8 -*-
"""把"多上游派发路由"作为 LLM provider 暴露出去（P5 / D25）

D25 的原话：**多上游派发路由属于主包**（`dsh-failover/` 是 ECHO 自己的代码，约 70 KB），
重构为 **LLM provider 的一种实现**（`openai-compat + failover`）；"注册进 DSH 配置"那一半
改为**仅在装了 `agent-dsh` 时可选执行**。

本文件是那个"一种实现"的**调用面**：路由本体（OpenAI 兼容入口 + 健康探测 + 熔断）仍在
`dsh-failover/proxy.py`，由 `app/failover_proxy.py` 负责拉起与探活。这样：
  * 纪要可以直接走它 → **不装 agent 也能出纪要**（P5 的验收点）；
  * P6 的 agent 后端同样可以把它当模型源（S4 已验证：`model=echo-auto` + 路由令牌可用）。

凭据处理（写死一条纪律）
----------------------
路由令牌从 `app.llm_router.router_token()` 取（它按"实际存在的 DSH 家目录"逐份找
ECHO_ROUTER_TOKEN —— 桌面版或标准版，只装其中一个也行；两个都没装时路由对本机
不校验令牌）。**令牌永远不进清单**（`ProviderSpec.details` 里只有端口与模型名），
也不写进任何日志。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from app.providers.base import LlmProvider

#: 路由对外的模型名（= dsh-failover/config.json 里的组名 / DSH 里看到的模型）
ROUTE_MODEL = "echo-auto"


class EchoAutoLlmProvider(LlmProvider):
    """走 ECHO 自己的多上游派发路由（OpenAI 兼容）。**数据会出网到配置的上游**。"""

    id = "echo-auto"

    # ---- 就绪 ----------------------------------------------------------

    def ready(self):
        """路由进程在监听就算就绪（上游可达性由路由自己的健康探测负责）。"""
        try:
            from app import failover_proxy
            return bool(failover_proxy.proxy_online(timeout=1.0))
        except Exception:
            return None

    # ---- 端点与凭据 -----------------------------------------------------

    def base_url(self):
        """路由的 OpenAI 兼容入口，例如 ``http://127.0.0.1:8899/v1``。"""
        port = 8899
        try:
            from app import failover_proxy
            port = int(failover_proxy.proxy_port())
        except Exception:
            pass
        return "http://127.0.0.1:%d/v1" % port

    def api_key(self):
        """路由令牌（取不到就返回空串，由路由自己决定是否拒绝）。"""
        try:
            from app import llm_router
            return str(llm_router.router_token() or "")
        except Exception:
            return ""

    # ---- 调用 ----------------------------------------------------------

    def chat(self, messages, timeout=60, base_url=None, api_key=None, model=None, **kw):
        """发一次 chat/completions，返回回复文本。

        失败**抛异常**（消息里带 provider 名与原因）—— 1.x 的教训是把"没拿到回复"
        静默变成空串，调用方无从判断（PROGRESS §19 发现③）。
        """
        if not messages:
            raise ValueError("echo-auto: messages 为空")
        url = (base_url or self.base_url()).rstrip("/") + "/chat/completions"
        payload = {"model": model or ROUTE_MODEL, "messages": list(messages),
                   "stream": False}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = api_key if api_key is not None else self.api_key()
        if token:
            headers["Authorization"] = "Bearer %s" % token
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise RuntimeError("echo-auto: 路由返回 HTTP %s %s" % (e.code, body)) from None
        except Exception as e:
            raise RuntimeError("echo-auto: 路由不可达（%s）——检查模型路由进程是否在运行"
                               % e) from None
        try:
            obj = json.loads(raw)
            content = obj["choices"][0]["message"]["content"]
        except Exception as e:
            raise RuntimeError("echo-auto: 回复格式不认识（%s）：%s" % (e, raw[:200])) from None
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("echo-auto: 上游返回了空回复（路由可能全成员失败）")
        return content


def register_builtin():
    """登记为 LLM provider（D25：路由是主包的一部分，默认 LLM 实现）。"""
    from app import providers as P

    P.register(P.ProviderSpec(
        id=EchoAutoLlmProvider.id, kind="llm", name="ECHO AUTO（多上游派发）",
        source="online", egress=True, default=True,
        egress_note="请求内容会发到你在「模型路由」里配置的上游（内网网关 / DeepSeek 官方 / "
                    "任意 OpenAI 兼容服务），按顺序派发并自动熔断",
        purpose="一个 OpenAI 兼容入口，背后是多个上游组成的模型组；纪要/命令都用它",
        details={"model": ROUTE_MODEL, "setting": "routerAutoRegister"}),
        EchoAutoLlmProvider)
