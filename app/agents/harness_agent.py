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
import os
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
    """**ECHO 唯一的 DSH 智能体**（一套 DSH）。

    2026-11-03 之前这里叫「标准版 harness」、与「DSH 桌面版」并列两套；用户拍板
    "不能接受用户用两套 dsh"。现在只有一个产品：
      * 桌面版（官方 DeepSeek Harness GUI）开着 → 直接用它的 43120（签名 Cookie）；
      * 没开 → ECHO 起无头 dsh（token），**家目录同样是 `~/.dsh`**。

    所以"会话/技能/工作区"只有一份，打开桌面版就能看到 ECHO 建的会议/指令/回顾空间。
    """
    name = "dsh"
    display_name = "DSH（DeepSeek Harness）"
    short_name = "DSH"
    vendor = "DeepSeek Harness"
    description = ("本机唯一的 DSH：桌面版开着就用桌面版，没开由 ECHO 起无头 dsh；"
                   "两者共用同一个 DSH 家目录，会话在工作区里可见")
    config_key = ""          # 恒启用：一套 DSH 没有"启用开关"
    #: 面板展开区里的配置项（dshAutoStart = 是否随 ECHO 启动）
    settings_keys = ("dshBaseUrl", "dshAutoStart", "harnessPort", "harnessCommand")
    capabilities = ("workspace", "session", "cancel", "history")
    web_ui = True          # 它有 Web 界面 → 仪表盘上给一个"用浏览器打开它"的小图标

    def __init__(self, base_url=None):
        super().__init__(base_url or harness_proc.base_url())
        self._login_ts = 0.0

    # ------------------------------------------------------------- 一套 DSH 的两种接入
    #
    # desktop=True：桌面版在跑 → 用 43120 + 签名 Cookie（父类 DshAgent 那套）
    # desktop=False：起/用无头 dsh → 用 43199 + token 换 Cookie（下面 _login 那套）

    def _sync_endpoint(self) -> bool:
        """按"谁在跑"同步 base_url，返回是否桌面版模式。

        发现桌面版起来时**顺手让出**同一个 DSH 家目录（停掉 ECHO 起的无头 dsh）——
        两个进程同时写 `~/.dsh` 会把会话/storages 写坏。只停 ECHO 自己起的那个。
        """
        desktop = harness_proc.desktop_mode()
        if desktop:
            try:
                harness_proc.yield_to_desktop()
            except Exception:
                pass
        want = harness_proc.desktop_url() if desktop else harness_proc.base_url()
        if want != self.base_url:
            self.base_url = want
            self._cookie = None
            self._cookie_ts = 0.0
        return desktop

    def _credentials_path(self):
        """凭据文件：与桌面版**同一份**（一套 DSH，共用同一个家目录）。"""
        return os.path.join(harness_proc.home(), ".credentials.yaml")


    def _workspace_registry_path(self):
        """独立 harness **自己家目录**里的工作区注册表。

        父类（DshAgent）缺省读 `~/.dsh/storages/workspace.json` —— 那是 DSH Desktop 的家，
        跟独立 harness 的 DSH_HOME（`harness_proc.home()`）是**两套**。读错注册表会把
        Desktop 的旧 workspaceId 当成 harness 的，`create_session(workspace_id=…)` 就报
        `workspace/not-found`，导致回退 cwd 方式建会话 → 侧栏全部落到「未分组」
        （2026-09-22 实测：指令会话 + 会议会话全未分组，根因就是这里）。
        """
        return os.path.join(harness_proc.home(), "storages", "workspace.json")

    def _secret_cookie(self):
        """退路：用 harness **自己家目录**里的 browser-session 密钥直接铸 Cookie。

        为什么要有这条路（2026-09-19 实测发现）：独立 harness 的 `.credentials.yaml` 与
        桌面版**结构完全同构**，都存着 `records['client-connection/browser-session'].payload.secret`。
        所以"用户自己起的实例 / ECHO 重启后 token 变了"这些情况下，根本不必去要 token ——
        照桌面版同一套算法铸 Cookie 就行。token 只有在**读不到那个文件**时才是必需的。
        """
        from app.agents.dsh_agent import _load_browser_secret
        secret = _load_browser_secret(self._credentials_path())
        if not secret:
            return ""
        from app.agents.dsh_agent import _make_cookie
        return _make_cookie(self.base_url, secret_b64=secret)

    def _login(self):
        """换 Cookie：**优先 token**（ECHO 自己拉起时抓到的），拿不到就用家目录里的密钥铸。

        harness 的 token 登录是**一个 303**：
            GET /?token=<token>  →  303 See Other + Set-Cookie: dsh-auth-…  →  Location: /
        所以**绝不能跟着跳转**：urllib 默认会跟随，而它跟随时**不带**这一步拿到的
        Set-Cookie（除非用 cookiejar），跳到 `/` 就被围栏判 401 —— 表现为
        "harness 登录失败：HTTP 401（token 是否正确/过期？）"，让人误以为是 token 错
        （2026-09-19 实测踩过：同一个 token 用 PowerShell 200、用 urllib 401）。
        """
        tok = harness_proc.token()
        if not tok:
            cookie = self._secret_cookie()
            if cookie:
                return cookie
            raise DshError(
                "没有 harness 访问 token，也读不到它的凭据文件（%s）：ECHO 还没把它拉起来，"
                "设置里也没填 token。选中本智能体后 ECHO 会自动启动并获取；"
                "若你是自己起的 harness，把启动时打印的 token 填到「harness 访问 token」里"
                % self._credentials_path())
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
                cookie = self._secret_cookie()   # 退路：拿家目录里的密钥铸一枚
                if cookie:
                    return cookie
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
        """Cookie 从哪来取决于**谁在跑**：

        * 桌面版在跑 → 用 `~/.dsh` 里的签名密钥自铸（父类 DshAgent 那套）；
        * 无头 dsh → token 换 Cookie（下面 `_login`），5 分钟主动重登一次。
        """
        if self._sync_endpoint():
            return DshAgent._cookie_header(self)
        if not self._cookie or time.time() - self._cookie_ts > 300:
            self._cookie = self._login()
            self._cookie_ts = time.time()
        return self._cookie

    def rpc(self, method, args=None, timeout=15):
        """一套 DSH 的两种接入：

        * 桌面版在跑 → 直接走父类（43120 + 签名 Cookie），**不起第二个进程**；
        * 否则走无头 dsh：401/403 重登一次，连接层失败时把无头 dsh 拉起来再试。
        """
        try:
            if self._sync_endpoint():
                return DshAgent.rpc(self, method, args, timeout=timeout)  # 桌面版：签名 Cookie
            return super().rpc(method, args, timeout=timeout)
        except DshError as e:
            msg = str(e)
            if "HTTP 401" in msg or "HTTP 403" in msg:
                self._cookie = self._login()
                self._cookie_ts = time.time()
                return super().rpc(method, args, timeout=timeout)
            if not self._looks_offline(e):
                raise
            ok, note = harness_proc.ensure_running()
            if not ok:
                raise DshError("%s（自动拉起 harness 没成功：%s）" % (e, note)) from e
            return super().rpc(method, args, timeout=timeout)

    @staticmethod
    def _looks_offline(e):
        """连接层失败？（**HTTP 错误要先排掉** —— `HTTPError` 也是 `URLError` 的子类，
        不排掉的话 401/403 会被当成"没起来"，转而去重启一个好好的 harness。）"""
        cause = getattr(e, "__cause__", None)
        if isinstance(cause, urllib.error.HTTPError):
            return False
        if isinstance(cause, (urllib.error.URLError, ConnectionError)):
            return True
        text = str(e)
        return ("Connection refused" in text or "10061" in text
                or "urlopen error" in text or "拒绝" in text)

    # ------------------------------------------------------------- 可用性

    def available(self, probe=False):
        """探活 + 给出"怎么把它跑起来"的可操作原因（一套 DSH 的两种接入分别说清）。"""
        if self._sync_endpoint():
            ok, why = DshAgent.available(self, probe=probe)
            return ok, ("桌面版在跑：" + why) if ok else (
                "DSH 桌面版开着但 ECHO 用不了它：" + why)
        # 探活**不走 rpc**：rpc 在"连不上"时会自动拉起引擎，那是业务路径的行为；
        # 探活（面板「检测」/启动页）只回答"在不在"，不许有副作用。
        if harness_proc.online(timeout=1.0 if not probe else 6.0):
            return True, "API 可访问（%s，无头 dsh）" % self.base_url
        try:
            from app.config import settings as _s
            auto = bool(_s.get("dshAutoStart", True))
        except Exception:
            auto = True
        if not auto:
            return False, ("DSH 没在跑：设置里关掉了「随 ECHO 启动」，"
                           "面板上点「启动」即可（桌面版打开后 ECHO 会自动改用它）")
        return False, ("DSH 没在监听 %s：ECHO 没能把它拉起来 —— 看 data/logs/harness.log；"
                       "启动程序来自官方桌面端自带的 dsh，"
                       "缺它就重装官方 DeepSeek Harness 桌面端" % self.base_url)


def build():
    return HarnessAgent()


from app.agents import register        # noqa: E402  （在文件末尾登记，避免循环导入）

register(HarnessAgent, build)
