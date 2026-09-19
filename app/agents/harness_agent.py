# -*- coding: utf-8 -*-
"""agents/harness_agent.py — 独立 DeepSeek Harness 适配器

与 `dsh_agent.DshAgent` **共用同一套 RPC 与会话语义**（`/api` 接口面实测完全相同），
差别只有两点，所以这里**继承**而不是复制：

  1. **服务来自哪里**：独立 harness 是 npm 包 `@deepseek-ai/dsh`（`dsh web`），
     由 ECHO 自己拉起（见 `app/harness_proc.py`）——不要求装 DSH Desktop；
  2. **鉴权**：Desktop 要的是"逆向出来的签名 Cookie"（HMAC + `~/.dsh` 密钥），
     独立 harness 给的是**启动时打印的 token**：拿它访问一次 `/?token=…`
     就换到一枚 `dsh-auth-…` Cookie，之后照常调 `/api/*`。

2026-09-19 隔离实测（独立 DSH_HOME + 端口 43199）：
`session/list`、`session/create`、`workspace/create` 全部 200，请求体与 ECHO 现有实现一致。
"""
import time
import urllib.error
import urllib.request

from app import harness_proc
from app.agents.dsh_agent import DshAgent, DshError, COOKIE_PREFIX


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """登录那一步必须看到 303 本身（它带着我们唯一需要的 Set-Cookie）。"""

    def redirect_request(self, *args, **kwargs):   # noqa: D102
        return None


class HarnessAgent(DshAgent):
    name = "harness"
    display_name = "独立 DeepSeek Harness"
    vendor = "DeepSeek（npm @deepseek-ai/dsh）"
    description = ("独立 harness 的 web 服务（随 ECHO 启动，默认 127.0.0.1:43199）——"
                   "不装 DSH Desktop 也能用；/api 接口与 Desktop 完全一致")
    config_key = "agentHarnessEnabled"
    #: 它自己的配置项（面板在展开区里编辑）；token 是 secret，不会经接口回显
    settings_keys = ("harnessCommand", "harnessHome", "harnessPort", "harnessToken")
    capabilities = ("workspace", "session", "cancel", "history")

    def __init__(self, base_url=None):
        super().__init__(base_url or harness_proc.base_url())
        self._login_ts = 0.0

    # ------------------------------------------------------------- 鉴权

    def _login(self):
        """用 token 换 Cookie。

        harness 的登录是**一个 303**：
            GET /?token=<token>  →  303 See Other + Set-Cookie: dsh-auth-…  →  Location: /
        所以**绝不能跟着跳转**：urllib 默认会跟随，而它跟随时**不带**这一步拿到的
        Set-Cookie（除非用 cookiejar），跳到 `/` 就被围栏判 401 —— 表现为
        "harness 登录失败：HTTP 401（token 是否正确/过期？）"，让人误以为是 token 错
        （2026-09-19 实测踩过：同一个 token 用 PowerShell 200、用 urllib 401）。
        """
        tok = harness_proc.token()
        if not tok:
            raise DshError(
                "没有 harness 访问 token：ECHO 还没把它拉起来，且设置里也没填。"
                "选中本智能体后 ECHO 会自动启动并获取；若你是自己起的 harness，"
                "把启动时打印的 token 填到「harness 访问 token」里")
        req = urllib.request.Request(self.base_url + "/?token=" + tok)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(req, timeout=8)
            headers = resp.headers
            resp.close()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                headers = e.headers          # 303 就是**正常**的登录响应
            elif e.code in (401, 403):
                harness_proc.forget_token()      # 这个 token 不能用了，别再反复试
                raise DshError("harness 拒绝了 token（HTTP %s）：token 可能已过期 —— "
                               "重新选中本智能体让 ECHO 重拉一次，或更新"
                               "「harness 访问 token」" % e.code) from e
            else:
                raise DshError("harness 登录失败：HTTP %s %s" % (e.code, e.reason)) from e
        except Exception as e:
            raise DshError("harness 登录失败：%s（独立 harness 是否在 %s 上跑？）"
                           % (e, self.base_url)) from e
        cookies = headers.get_all("Set-Cookie") or []
        for c in cookies:
            if c.startswith(COOKIE_PREFIX):
                return c.split(";", 1)[0]
        raise DshError("harness 登录失败：响应里没有 %s Cookie（token 被拒？）" % COOKIE_PREFIX)

    def _cookie_header(self):
        """**不再铸造 HMAC Cookie**，改为 token 换 Cookie；5 分钟主动重登一次。"""
        if not self._cookie or time.time() - self._cookie_ts > 300:
            self._cookie = self._login()
            self._cookie_ts = time.time()
        return self._cookie

    def rpc(self, method, args=None, timeout=15):
        """401/403 时重登一次再试（cookie 过期不该让一次命令白跑）。"""
        try:
            return super().rpc(method, args, timeout=timeout)
        except DshError as e:
            msg = str(e)
            if "HTTP 401" not in msg and "HTTP 403" not in msg:
                raise
            self._cookie = self._login()
            self._cookie_ts = time.time()
            return super().rpc(method, args, timeout=timeout)

    # ------------------------------------------------------------- 可用性

    def available(self, probe=False):
        """探活 + 给出"怎么把它跑起来"的可操作原因。"""
        try:
            self.rpc("session/list", {"_request": {}}, timeout=3 if not probe else 6)
            return True, "API 可访问（%s）" % self.base_url
        except DshError as e:
            if not harness_proc.online(timeout=1.0):
                if not harness_proc.requested():
                    return False, ("独立 harness 没在运行：在面板把它选为当前智能体"
                                   "（或打开「启用独立 DeepSeek Harness」），"
                                   "ECHO 会自动拉起（需要本机 Node / npx）")
                return False, ("独立 harness 没在监听 %s：看 data/logs/harness.log —— "
                               "多数是 Node/npx 不在 PATH（把「harness 启动命令」改成 npx 全路径）"
                               % self.base_url)
            return False, "连不上独立 harness：%s" % e


def build():
    return HarnessAgent()


from app.agents import register        # noqa: E402  （在文件末尾登记，避免循环导入）

register(HarnessAgent, build)
