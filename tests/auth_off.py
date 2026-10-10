# -*- coding: utf-8 -*-
"""测试用的"关掉鉴权"小工具（2026-10-11）。

**为什么必须有它**：门禁在**开发者自己的机器**上跑，而那台机器一旦配过手机，
`apiAuthEnabled=true`（且 `serverBindMode=lan` 不让关）。测试里的裸 `TestClient` 对端是
`"testclient"`，`netguard.is_loopback_peer()` 判不出 IP → **fail closed → 不算回环** →
于是 `/api/*` 一律 401，而用例期望 200。表现是"一片红，但坏的地方不是红的地方"。

**做法**：在**内存里**把这一项遮成 False —— **不写库**。写库（`settings.update({...})`）
会改**用户的真实设置**，万一崩在中间就把手机那条路（LAN + 鉴权）弄断了。
（`tests/test_api_paths.py` 与 `tests/test_install_state.py` 里是同一套做法，这里是共享版。）

**只在需要"以本机面板身份调用 API"的用例模块里用**：有些用例断的正是"网上来的调用被拒"
（`_NETWORK_DENIED`），那些**不能**用 —— 遮掉鉴权会让它们失去意义。
"""
import contextlib

from unittest.mock import patch


@contextlib.contextmanager
def api_auth_off():
    """把 `apiAuthEnabled` 读成 False（其余键原样透传；退出时自动还原）。"""
    from app.config import settings
    real = settings.get

    def _get(key, *a, **kw):
        return False if key == "apiAuthEnabled" else real(key, *a, **kw)

    with patch.object(settings, "get", side_effect=_get):
        yield


def install_for_module(module_globals):
    """给一个测试模块装/卸这对钩子（`setUpModule` / `tearDownModule` 里各调一次）。

    用法（放在模块末尾、`if __name__` 之前或之后都行）：

        from tests.auth_off import install_for_module
        def setUpModule():    install_for_module(globals())
        def tearDownModule(): install_for_module(globals())
    """
    ctx = module_globals.get("_api_auth_off_ctx")
    if ctx is None:
        ctx = api_auth_off()
        ctx.__enter__()
        module_globals["_api_auth_off_ctx"] = ctx
    else:
        ctx.__exit__(None, None, None)
        module_globals["_api_auth_off_ctx"] = None
