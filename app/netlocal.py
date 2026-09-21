# -*- coding: utf-8 -*-
"""netlocal.py — 本机（回环）HTTP 调用：**显式绕过代理**。

为什么要有这个模块（2026-09-22 同事实测反馈 B2）：公司机器上常设 `http_proxy` / `https_proxy`，
而 ECHO 与自己的本地服务（面板 API、模型路由、独立 harness、DSH Desktop）全靠 `127.0.0.1` 通信。
`urllib` 默认会读代理环境变量，于是**探活请求被代理劫走** → "服务明明在跑，ECHO 却认为它挂了"。
同事那台的表现是 harness 一直 `idle`、token 拿不到，而 `curl --noproxy 127.0.0.1` 返回 401。

两条一起用，缺一不可：

* :func:`urlopen` —— 按 URL 判断：**回环走无代理 opener**；外部地址仍走系统代理
  （企业网里访问 PyPI / ModelScope 往往正需要它，不能一刀切）。
  这是主要手段：Windows 上 `urllib` 的 `proxy_bypass` 读的是**注册表**里的
  ProxyOverride，光设环境变量并不保证绕过。
* :func:`ensure_no_proxy_env` —— 往 `NO_PROXY`/`no_proxy` 里补回环地址，让**子进程**
  与第三方库（requests / httpx / huggingface_hub 等自己读环境变量的）也一并免疫。
"""
from __future__ import annotations

import os
import urllib.parse
import urllib.request

#: 回环主机名（含 IPv6 写法；``urlsplit().hostname`` 会把方括号去掉）
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

#: 需要补进 NO_PROXY 的主机
_NO_PROXY_HOSTS = ("127.0.0.1", "localhost", "::1")

#: 一律不走代理的 opener（回环专用）
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
#: 默认 opener（外部地址用：尊重系统/环境代理）
_DEFAULT_OPENER = urllib.request.build_opener()


def is_loopback(url: str) -> bool:
    """这个 URL 是不是打到本机。端口、大小写、IPv6 方括号都兼容。"""
    try:
        host = urllib.parse.urlsplit(str(url)).hostname or ""
    except Exception:
        return False
    host = host.strip().lower()
    if not host:
        return False
    if host in LOOPBACK_HOSTS:
        return True
    return host.startswith("127.")          # 127.0.0.0/8 整个网段都是本机


def pick(url: str):
    """按 URL 选 opener：回环 → 无代理；其它 → 默认。测试直接断言这个选择。"""
    return _DIRECT_OPENER if is_loopback(url) else _DEFAULT_OPENER


def urlopen(url_or_request, data=None, timeout=5.0):
    """与 ``urllib.request.urlopen`` 同形，但回环地址**不经过代理**。

    接受 URL 字符串或 ``urllib.request.Request``（两种在仓库里都有用到）。
    """
    target = getattr(url_or_request, "full_url", url_or_request)
    return pick(target).open(url_or_request, data=data, timeout=timeout)


def ensure_no_proxy_env() -> bool:
    """把回环地址补进 ``NO_PROXY``/``no_proxy``（幂等）。返回是否有改动。

    给子进程与第三方库兜底 —— 它们不看我们上面那个 opener，只看环境变量。
    已有的值一律保留（用户自己配的域名不能丢）。
    """
    changed = False
    for key in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(key, "")
        have = {p.strip().lower() for p in current.split(",") if p.strip()}
        missing = [h for h in _NO_PROXY_HOSTS if h.lower() not in have]
        if not missing:
            continue
        os.environ[key] = (current + "," + ",".join(missing)).strip(",") if current else ",".join(missing)
        changed = True
    return changed


# ---------------------------------------------------------------- 全局开关
# 仓库里有十来处 urlopen 直接打 127.0.0.1（面板自查、模型路由探活、harness/DSH 适配器…）。
# 逐个改写既慢又**必然会漏**（以后新加的调用点又忘了），所以在进程启动时装一次全局转发：
# 回环 URL 交给无代理 opener，其余原样走系统代理。
# 这是应用改造自己的进程行为（ECHO 不是被别处 import 的库），启动时装一次即可。
_BYPASS_INSTALLED = False
_ORIGINAL_URLOPEN = urllib.request.urlopen


def install_loopback_bypass() -> bool:
    """装上"回环调用不走代理"的全局转发（幂等）。返回本次是否新装。"""
    global _BYPASS_INSTALLED
    if _BYPASS_INSTALLED:
        return False
    ensure_no_proxy_env()

    def _urlopen(url_or_request, *args, **kwargs):
        target = getattr(url_or_request, "full_url", url_or_request)
        if is_loopback(target):
            return _DIRECT_OPENER.open(url_or_request, *args, **kwargs)
        return _ORIGINAL_URLOPEN(url_or_request, *args, **kwargs)

    urllib.request.urlopen = _urlopen
    _BYPASS_INSTALLED = True
    return True


def uninstall_loopback_bypass() -> bool:
    """撤掉全局转发（只给测试用，保证用例之间不互相污染）。"""
    global _BYPASS_INSTALLED
    if not _BYPASS_INSTALLED:
        return False
    urllib.request.urlopen = _ORIGINAL_URLOPEN
    _BYPASS_INSTALLED = False
    return True

