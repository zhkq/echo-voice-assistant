# -*- coding: utf-8 -*-
"""「对外公布地址」的**权威值**（发配对码时 `echo://pair?host=…` 里那个 host）。

## 为什么要单独一个模块

`server/ops.py` 那条路（`resolve_advertised` / `pairing_string`）必须能**只靠 `cfg`**
就把地址算出来 —— 它同时被命令行、管理面、本机自配对三处调用，而其中两处手里没有库。
所以这里的职责只有两件：

1. **把管理面配过的值写进 `cfg.raw["server"]["advertised_host"]`**（`apply_stored`）；
2. **把"当前值 + 来源"摊开给人看 / 校验请求体**（`view` / `parse` / `set_advertised`）。

地址本身的归一化规则**只有一份**，在 `ops.normalize_advertised_host()` 里 ——
这里一行都不重写（重写一遍就是"两条路各认一套格式"，迟早漂开）。

## 优先级：**管理面配置 > 环境变量 / 配置文件 > 自动探测**

| 顺序 | 来源 | 落在哪 | 页面上的 `source` |
|---|---|---|---|
| 1 | 管理面配置 | 鉴权库的 `server_advertised` 表（state 卷，**重启后还在**） | `admin` |
| 2 | 环境变量 / 配置文件 | `ECHO_ADVERTISED_HOST`（别名 `ECHO_PUBLIC_URL`）/ YAML | `env` 或 `config` |
| 3 | **自动探测** | `ops._probe_advertised()`（探测本机地址 + 那句如实提示） | `auto` |

**「没配」这一档刻意不猜一个局域网 IP**：后端就跑在客户端这台机器上时（本机后端），
那个猜法**必然猜错**（该给 `127.0.0.1`）。没配就如实说"这个地址是本机探测到的"，
把"换成他们访问得到的那台机器"这句话留在串的 `note` 里（一个字都没删）。

## 与 `server/limits.py` 的关系

两者是同一个形状（管理面配的值存在 state 卷、压过 env、重启还在、页面显示来源），
所以这里的口径**照抄 limits** —— 管理面那一侧也是同一套闸门（`admin.as_write`）。
**不合并成一张表**：`server_limits` 的值是整数（`value INTEGER`），而这里是一个地址串；
把地址塞进整数列只能靠编码，那是把简单的事弄复杂。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from server import errors
from server import ops as ops_mod

#: 这个配置项在哪（`cfg.get` 的点分路径 + 环境变量名清单，顺序 = 优先级）。
#: 环境变量**两个都收**：`ECHO_ADVERTISED_HOST` 是服务端自己的口径（与配置键同名同义），
#: `ECHO_PUBLIC_URL` 是内网部署里更常见的叫法。同时设时前者优先（`settings._env_overrides`
#: 里就是这么读的，这里必须与它一致 —— 否则页面显示的"来源"会与真正生效的那个变量相反）。
KEY = "server.advertised_host"
ENV_NAMES = ("ECHO_ADVERTISED_HOST", "ECHO_PUBLIC_URL")

#: 页面上那三种部署形态的一句话说明（**由后端下发**，页面不另抄一份 ——
#: 抄的那份迟早与真正的行为漂开，而这句话正是"填错了自己也看不出来"的解药）。
FORM_NOTE = (
    "这一项是**每次发码时想公布的地址**，不是一次性全局常量："
    "① 同事要连（容器 / 独立 GPU 机）→ 填**局域网/对外 IP**（如 10.100.0.24）；"
    "② 后端就跑在客户端这台机器上（本机后端）→ 填 **127.0.0.1**"
    "（本机客户端永远连得上，换网络、IP 变了都不受影响）；"
    "③ 同一台后端同时服务本机与远程 → **发码时各写各的**（本机 127.0.0.1、远程局域网 IP，"
    "配对串本身带 host，两张码互不影响），串里的 host 手改就能用。"
    "不填则沿用自动探测（容器里探测到的会是 Docker 网桥地址，同事连不上）。"
)


# ---------------------------------------------------------------- 小工具

def env_value() -> str:
    """环境变量里设的那个（认不出来 / 空 = 没设，与 `settings._env_overrides` 同一判据）。"""
    for name in ENV_NAMES:
        got = str(os.environ.get(name, "") or "").strip()
        if got:
            return got
    return ""


def stored(store) -> str:
    """管理面配过的值。**空 = 从没配过** → 走 env / 配置文件 / 自动探测。"""
    if store is None:
        return ""
    try:
        return str(store.advertised_override() or "")
    except Exception:                                          # pragma: no cover - 兜底
        return ""


def _newest(store) -> dict:
    if store is None:
        return {}
    try:
        return store.advertised_override_row() or {}
    except Exception:                                          # pragma: no cover - 兜底
        return {}


def normalize(raw: Any) -> str:
    """归一化（唯一一份实现在 `ops`）。非法值 → `ValueError`（中文原因）。"""
    return ops_mod.normalize_advertised_host(raw)


# ---------------------------------------------------------------- 应用 / 呈现

def apply_stored(cfg, store) -> Dict[str, str]:
    """把管理面配过的地址写进 `cfg.raw["server"]["advertised_host"]`（**覆盖** env 与 YAML）。

    进程启动时调一次（`main.create_app` 的 lifespan）。返回写进去的那一项
    （`{}` = 管理面没配过，什么都不动）。**与 `limits.apply_stored` 同一个形状。**
    """
    saved = stored(store)
    if not saved:
        return {}
    cfg.raw.setdefault("server", {})["advertised_host"] = saved
    return {"advertised_host": saved}


def effective(cfg) -> str:
    """此刻**真正生效**的那个值（`""` = 走自动探测）。`cfg` 已经是应用过管理面的那一份。"""
    return str(cfg.get(KEY, "") or "").strip()


def _source(cfg, store) -> str:
    """值从哪来：`admin` / `env` / `config` / `auto`（`auto` 时 `value` 是探测出来的）。

    ⚠️ **env 与 config 其实分不开**：`settings.load()` 把 YAML 与环境变量**合并进同一个
    `cfg`** 之后就看不出是谁写的了，而这两档**行为完全一样**（都用这个值）——
    差别只在页面上那句"来源"。所以这里的判据是：**环境里设了那个变量就报 `env`**
    （部署时最常改的一档），否则报 `config`（YAML 里写的）。文案因此写
    **"环境变量 / 配置文件"** —— 说一个分不出来的来源，比说错强。
    """
    if stored(store):
        return "admin"
    if env_value():
        return "env"
    if effective(cfg):
        return "config"
    return "auto"


def priority_notes(cfg, store=None) -> List[str]:
    """env 在场面、却被管理面盖住时的**如实提示**（一句话一条，没有就是空表）。

    与 `limits.priority_notes` 同一个理由：`compose.yaml` 里就摆着
    `ECHO_ADVERTISED_HOST`，管理员在页面上改成别的之后，现场的人会以为
    "我改了 env 怎么没变" —— 谁赢了必须说出口。
    """
    got_env, now, source = env_value(), effective(cfg), _source(cfg, store)
    if source == "admin" and got_env and got_env != now:
        return ["环境变量 %s 已设（%s），但当前以管理面配置的 %s 为准。"
                % ("/".join(ENV_NAMES), got_env, now)]
    return []


def probe_view(cfg) -> Dict[str, Any]:
    """没配时**实际会发出去的**地址（探测结果，给页面显示用）。

    页面显示"当前对外地址"时必须是**真的那条串里的那个地址** —— 所以这里直接问
    `ops.resolve_advertised()`（与发码那条路同一个函数），而不是另算一遍。
    """
    try:
        url, note = ops_mod.resolve_advertised(cfg)
        return {"url": url, "note": note}
    except errors.EchoError as exc:
        return {"url": "", "note": "%s" % (exc.detail or exc.message)}


def view(cfg, store=None) -> Dict[str, Any]:
    """`GET /admin/api/advertised-host` 的响应体：生效值 + 来源 + 三种形态的说明。"""
    source = _source(cfg, store)
    guessed = probe_view(cfg) if source == "auto" else {"url": "", "note": ""}
    newest = _newest(store)
    return {
        "value": effective(cfg),                 # "" = 走自动探测
        "source": source,                        # admin / env / config / auto
        "sourceNote": ("admin = 管理面配的（最优先）；env = 环境变量 ECHO_ADVERTISED_HOST / "
                       "ECHO_PUBLIC_URL；config = 配置文件里写的；auto = **没配**，"
                       "此刻那条串里的地址是本机探测出来的（容器里会是 Docker 网桥地址）。"),
        "url": guessed.get("url") or _configured_url(cfg, store),
        "note": guessed.get("note", ""),
        "envNames": list(ENV_NAMES),
        "envValues": {n: (os.environ.get(n) or None) for n in ENV_NAMES},
        "priority": "admin",
        "priorityNote": "管理面配置 > 环境变量 / 配置文件 > 自动探测：在管理面里配过的值"
                        "覆盖环境变量，重启后仍在（存在 state 卷的库里）。",
        "formNote": FORM_NOTE,
        "accepts": "主机名/IP、`IP:端口`、或 `http://IP:端口`（IPv6 要带方括号）；"
                   "不写端口就用 server.listen 的端口；scheme 由 TLS 决定，别在这里写 https。",
        "notes": priority_notes(cfg, store),
        "updatedAt": float(newest.get("updated_at") or 0.0),
        "updatedBy": str(newest.get("updated_by") or ""),
    }


def _configured_url(cfg, store) -> str:
    """配了值时，那条串里的地址（读不出来就空串 —— 页面显示"读不到"比显示错的强）。"""
    try:
        return ops_mod.resolve_advertised(cfg)[0]
    except errors.EchoError:
        return ""


# ---------------------------------------------------------------- 写入

def audit_target(payload: Optional[dict]) -> str:
    """审计里的"对象"那一栏（用请求里给的原始值，失败也留痕）。"""
    if not isinstance(payload, dict):
        return "(空请求)"
    if "value" not in payload:
        return "(空请求)"
    return "advertisedHost=%s" % (payload.get("value"),)


def parse(payload: Optional[dict]) -> str:
    """校验请求体，返回**归一化后**要写入的值（`""` = 清空 = 回到自动探测）。

    不合法一律 400 + 中文原因（**不静默忽略**）—— 静默忽略正是这次要修的 bug 的形态：
    配置里写着什么，实际却在探测，而两者看起来都没错。
    """
    if not isinstance(payload, dict) or "value" not in payload:
        raise errors.bad_request(
            "要带 value 字段（主机名/IP、`IP:端口` 或 `http://IP:端口`；空串 = 清空，"
            "回到自动探测）")
    try:
        return normalize(payload.get("value"))
    except ValueError as exc:
        raise errors.bad_request("%s" % exc)


def set_advertised(cfg, store, payload: Optional[dict], *, updated_by: str = "") -> str:
    """校验 → 落库 → **当场生效**（改 `cfg.raw`，下一次发码就现读到）。

    先落库再改 `cfg`：库写失败就不该让内存里出现一个"看起来生效了、重启就没了"的值
    （与 `limits.set_limits` 同一个顺序）。
    """
    value = parse(payload)
    if store is not None:
        store.set_advertised_override(value, str(updated_by or ""))
    cfg.raw.setdefault("server", {})["advertised_host"] = value
    return value
