#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
echo-llm-router — ECHO LLM 路由（本机 HTTP 服务）

DSH 侧把 provider 的 baseURL 指向本服务（默认 http://127.0.0.1:8899），
本服务按「模型组」把请求派发给组内成员：

    echo-auto  →  [1] 大EP（联通内网，DeepSeek-V4-Flash）
                  [2] DeepSeek-V41-Flash（官方公网，deepseek-flash）

派发规则
  1. 组成员按 priority 升序尝试；停用（enabled=false）的成员不参与；
     健康表里「熔断中」或「最近探测不可达」的成员先跳过
     （全都不可用时不再跳过，照样按序试一遍——避免健康表把唯一活路也挡住）。
  2. 只有「连不上 / 首字节迟迟不来 / 401 / 402 / 403 / 404 / 429 / 5xx」才切下一个成员；
     4xx 参数类错误（含上下文超长）原样透传，尊重上游裁定，不掩盖真实错误。
  3. 首个数据块之前都可以切换；一旦向 DSH 出流即提交，之后失败只能中断
     （DSH 会按 provider 的 retryPolicy 重试，此时坏成员已被熔断，重试自然落到下一个）。
  4. 后台低频探测（TCP 连接 + GET {baseURL}/models）维护健康表；真实请求结果同样计入熔断。

模型组怎么配
  组成员、优先级、启停都在 ECHO 面板「模型路由」页里改（app/router_admin.py 写回
  config.json 并调用本服务的 /admin/reload 热重载）。config.json 里只放结构，
  密钥一律按成员的 credential 名去 DSH 凭据库取。

密钥来源（优先级从高到低）
  - 组成员配置里的 "token"
  - 环境变量 FAILOVER_<CREDENTIAL>
  - **每个存在的 DSH 家目录**里的 .credentials.yaml refs（如 INTERNAL_LLM_TOKEN、
    DEEPSEEK_API_KEY）。DSH 可能只有一个家目录 —— 桌面版（用户家目录下的 .dsh）或
    标准版 harness（ECHO 的 data 目录下 harness/），也可能两个都有、一个都没有，
    所以按列表逐份找，别只认桌面版那一份。
  - 路由自身令牌 ECHO_ROUTER_TOKEN（同库 refs，用于校验 DSH 发来的 Bearer）

用法
  python proxy.py                      # 默认 127.0.0.1:8899
  python proxy.py --port 8899 --config <path>
  python proxy.py --check              # 打印生效配置与组成员健康，退出
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8899

# 连接超时故意很短：内网 DNS/握手失败要「秒切」，不能让用户干等。
DEFAULT_CONNECT_TIMEOUT = 1.5
# 首个数据块前的耐心窗口：上游可达但迟迟不出流也算「不通」，切下一个。
DEFAULT_FIRST_BYTE_TIMEOUT = 20.0
DEFAULT_READ_TIMEOUT = 360.0
DEFAULT_WRITE_TIMEOUT = 120.0
# 后台健康探测间隔（秒）与探测请求超时
DEFAULT_PROBE_INTERVAL = 45.0
DEFAULT_PROBE_TIMEOUT = 4.0
# 熔断：连续失败 N 次 → 冷却，冷却结束后放行试水
DEFAULT_BREAKER_THRESHOLD = 2
DEFAULT_BREAKER_COOLDOWN = 30.0

CRED_YAML = Path.home() / ".dsh" / ".credentials.yaml"
TOKEN_REF = "ECHO_ROUTER_TOKEN"

#: "去哪儿找凭据"的清单：ECHO 每次注册模型组时写（app/llm_router.py 的 _write_homes_file），
#: 或者启动时用 ECHO_DSH_HOMES 环境变量告知。**热更新**：家目录可能在 ECHO/路由起来之后
#: 才出现（用户在面板里选中标准版，harness 家目录才被建出来），所以按 mtime 缓存并重读。
HOMES_FILE = Path(os.environ.get("ECHO_DSH_HOMES_FILE")
                  or (Path(__file__).resolve().parent / "homes.json"))
_HOMES_CACHE: dict = {"mtime": "init", "paths": None}

# 命中这些状态码时换下一个成员；其余 4xx 原样透传给 DSH
FAILOVER_STATUS = {401, 402, 403, 404, 429}

# 公网兼容：仅保留标准 OpenAI Chat Completions 字段，剔除内网网关专有字段
PUBLIC_SAFE_FIELDS = {
    "messages", "model", "temperature", "top_p", "top_k", "max_tokens", "max_completion_tokens",
    "stream", "stream_options", "stop", "n", "presence_penalty", "frequency_penalty", "logit_bias",
    "logprobs", "top_logprobs", "tools", "tool_choice", "parallel_tool_calls", "response_format",
    "seed", "user", "reasoning_effort", "metadata", "store", "grammar",
}


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------
def cred_paths() -> list:
    """要搜索的凭据库路径（按顺序、前面的优先）。

    顺序：① homes.json（ECHO 写的，热更新）；② ECHO_DSH_HOMES 环境变量（启动时告知）；
    ③ 桌面版那一份（老行为，一个家目录都没被告知时的兜底）。文件不存在就跳过 ——
    凭据库是 DSH 自己建的，这里绝不代建。
    """
    try:
        mtime = HOMES_FILE.stat().st_mtime
    except Exception:
        mtime = None
    if _HOMES_CACHE["mtime"] == mtime and _HOMES_CACHE["paths"] is not None:
        return _HOMES_CACHE["paths"]
    paths: list[Path] = []
    if mtime is not None:
        try:
            doc = json.loads(HOMES_FILE.read_text(encoding="utf-8"))
            for item in doc.get("credentials") or []:
                if isinstance(item, str) and item.strip():
                    paths.append(Path(item))
        except Exception:
            paths = []
    if not paths:
        for chunk in (os.environ.get("ECHO_DSH_HOMES") or "").split(os.pathsep):
            chunk = chunk.strip()
            if chunk:
                paths.append(Path(chunk) / ".credentials.yaml")
    if not paths:
        paths = [CRED_YAML]
    _HOMES_CACHE["mtime"], _HOMES_CACHE["paths"] = mtime, paths
    return paths


def _cred_refs() -> dict:
    """所有存在的 DSH 凭据库里的 refs（前一份优先，后面的补缺；不引入 YAML 依赖）。"""
    out: dict[str, str] = {}
    for path in cred_paths():
        try:
            text = Path(path).read_text(encoding="utf-8")
        except Exception:
            continue
        for m in re.finditer(r"^\s{2}([A-Za-z0-9_\-]+)\s*:\s*(\S+)\s*$", text, re.M):
            out.setdefault(m.group(1), m.group(2).strip().strip("\"'"))
    return out


def resolve_credential(ref: str) -> str:
    """ref 名 → 真实密钥：环境变量优先，其次各 DSH 凭据库。"""
    if not ref:
        return ""
    env = os.environ.get("FAILOVER_" + ref) or os.environ.get(ref)
    if env:
        return env.strip()
    return _cred_refs().get(ref, "")


# ---------------------------------------------------------------------------
# 组成员
# ---------------------------------------------------------------------------
def _now() -> float:
    return time.time()


def _hhmmss(ts: Optional[float] = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts if ts else _now()))


@dataclass
class Member:
    """一个可派发目标 = （端点 + 模型 + 凭据 + 请求改写策略）。"""

    name: str
    base_url: str
    model: str
    priority: int = 1
    enabled: bool = True           # 面板里取消勾选 = 暂时不用它（保留配置与统计）
    credential: str = ""          # DSH 凭据 ref 名
    token: str = ""               # 直接写死的 token（测试用，正式配置留空）
    headers: dict = field(default_factory=dict)
    body_mode: str = "passthrough"   # passthrough | openai-safe
    first_byte_timeout: float = DEFAULT_FIRST_BYTE_TIMEOUT
    trust_env: bool = True

    # ---- 健康表 ----
    ok: int = 0
    fail: int = 0
    consecutive_failures: int = 0
    open_until: float = 0.0
    reachable: Optional[bool] = None      # 后台探测结论：True/False/None(未探)
    detail: str = "尚未探测"
    last_probe_at: str = ""
    last_used_at: str = ""
    last_error: str = ""
    last_ttfb_ms: Optional[int] = None

    # ---- 派生 ----
    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    @property
    def models_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        return base + "/models"

    def bearer(self) -> str:
        return self.token or resolve_credential(self.credential)

    def breaker_open(self) -> bool:
        return self.open_until > _now()

    def mark_ok(self, ttfb_ms: int) -> None:
        self.ok += 1
        self.consecutive_failures = 0
        self.open_until = 0.0
        self.reachable = True
        self.detail = "正常"
        self.last_error = ""
        self.last_used_at = _hhmmss()
        self.last_ttfb_ms = ttfb_ms

    def mark_fail(self, reason: str, threshold: int = DEFAULT_BREAKER_THRESHOLD,
                  cooldown: float = DEFAULT_BREAKER_COOLDOWN) -> None:
        self.fail += 1
        self.consecutive_failures += 1
        self.last_error = reason
        self.last_used_at = _hhmmss()
        if self.consecutive_failures >= threshold:
            self.open_until = _now() + cooldown
            self.detail = f"熔断中（{reason}）"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            # 界面上统一显示成「通道号-昵称」（如 1-大ep）：通道号就是 priority/派发顺序
            "label": f"{self.priority}-{self.name}",
            "priority": self.priority,
            "enabled": self.enabled,
            "model": self.model,
            "endpoint": self.endpoint,
            "state": "open" if self.breaker_open() else "closed",
            "reachable": self.reachable,
            "detail": self.detail,
            "ok": self.ok,
            "fail": self.fail,
            "consecutive_failures": self.consecutive_failures,
            "last_ttfb_ms": self.last_ttfb_ms,
            "last_probe_at": self.last_probe_at,
            "last_used_at": self.last_used_at,
            "last_error": self.last_error,
            "has_token": bool(self.bearer()),
        }


@dataclass
class Group:
    """一个模型组 = 对 DSH 暴露的一个「本地模型」。"""

    id: str
    display_name: str
    members: list = field(default_factory=list)
    require_token: bool = False
    # 命中哪些 HTTP 状态码时换下一个成员；空集 = 只在「连不上 / 首字节不来」时切换
    failover_statuses: frozenset = frozenset()
    failover_on_5xx: bool = False

    def ordered(self) -> list:
        return sorted(self.members, key=lambda m: m.priority)

    def active(self) -> list:
        """实际参与派发的成员（按优先级）：面板里停用掉的跳过。"""
        return [m for m in self.ordered() if m.enabled]

    def should_failover(self, status: int) -> bool:
        if status >= 500:
            return self.failover_on_5xx
        return status in self.failover_statuses


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------
class Config:
    def __init__(self, **kw):
        self.host = kw.get("host", DEFAULT_HOST)
        self.port = int(kw.get("port", DEFAULT_PORT))
        self.connect_timeout = float(kw.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT))
        self.read_timeout = float(kw.get("read_timeout", DEFAULT_READ_TIMEOUT))
        self.write_timeout = float(kw.get("write_timeout", DEFAULT_WRITE_TIMEOUT))
        self.first_byte_timeout = float(kw.get("first_byte_timeout", DEFAULT_FIRST_BYTE_TIMEOUT))
        self.probe_interval = float(kw.get("probe_interval", DEFAULT_PROBE_INTERVAL))
        self.probe_timeout = float(kw.get("probe_timeout", DEFAULT_PROBE_TIMEOUT))
        # 关掉后台探测后，健康表只由真实请求驱动（每个请求都会真试一遍优先级序列）
        self.probe_enabled = bool(kw.get("probe_enabled", True))
        self.breaker_threshold = int(kw.get("breaker_threshold", DEFAULT_BREAKER_THRESHOLD))
        self.breaker_cooldown = float(kw.get("breaker_cooldown", DEFAULT_BREAKER_COOLDOWN))
        self._raw = kw
        # 配置来源（热重载用）：默认脚本同目录 config.json
        self.config_path = kw.get("_config_path") or str(Path(__file__).resolve().parent / "config.json")
        self.groups: dict[str, Group] = self._build_groups(kw)

    # -- 组定义 --
    def _build_groups(self, kw: dict) -> dict:
        groups: dict[str, Group] = {}
        for gid, g in (kw.get("groups") or {}).items():
            members = []
            for i, m in enumerate(g.get("members") or [], start=1):
                members.append(Member(
                    name=m.get("name") or f"{gid}-{i}",
                    base_url=m["base_url"],
                    model=m.get("model") or "",
                    priority=int(m.get("priority", i)),
                    enabled=bool(m.get("enabled", True)),
                    credential=m.get("credential", ""),
                    token=m.get("token", ""),
                    headers=dict(m.get("headers") or {}),
                    body_mode=m.get("body_mode", "passthrough"),
                    first_byte_timeout=float(m.get("first_byte_timeout", self.first_byte_timeout)),
                    trust_env=bool(m.get("trust_env", True)),
                ))
            groups[gid] = Group(
                id=gid,
                display_name=g.get("display_name") or gid,
                members=members,
                require_token=bool(g.get("require_token", False)),
                failover_statuses=frozenset(g.get("failover_on_status", sorted(FAILOVER_STATUS))),
                failover_on_5xx=bool(g.get("failover_on_5xx", True)),
            )
        return groups

    def all_members(self) -> list:
        return [m for g in self.groups.values() for m in g.members]

    # -- 热重载 --
    def reload(self) -> tuple:
        """重读 config.json 并原地换掉组定义（同名同端点的成员保留健康计数）。

        这样面板改完模型组不用重启路由进程：成员增删/优先级/启停立即生效。
        连接与读写超时挂在 httpx client 上，改这些仍需重启进程。
        """
        try:
            raw = Path(self.config_path).read_bytes().decode("utf-8-sig")
            kw = json.loads(raw)
        except Exception as exc:
            return False, f"读取配置失败：{type(exc).__name__}: {exc}"
        try:
            new = Config(**{**kw, "_config_path": self.config_path})
        except Exception as exc:
            return False, f"配置不合法（保留原配置）：{type(exc).__name__}: {exc}"
        old = {(m.name, m.endpoint, m.model): m for m in self.all_members()}
        for m in new.all_members():
            o = old.get((m.name, m.endpoint, m.model))
            if o:
                for f in ("ok", "fail", "consecutive_failures", "open_until", "reachable",
                          "detail", "last_probe_at", "last_used_at", "last_error", "last_ttfb_ms"):
                    setattr(m, f, getattr(o, f))
        self.groups = new.groups
        self.first_byte_timeout = new.first_byte_timeout
        self.probe_interval = new.probe_interval
        self.probe_timeout = new.probe_timeout
        self.probe_enabled = new.probe_enabled
        self.breaker_threshold = new.breaker_threshold
        self.breaker_cooldown = new.breaker_cooldown
        self._raw = new._raw
        return True, f"已重载 {len(self.groups)} 个组 / {len(self.all_members())} 个成员"

    def resolve_group(self, model_id: str) -> Optional[Group]:
        """按请求里的 model 找组；找不到返回 None（由调用方回 404，别乱派给别的组）。"""
        return self.groups.get(model_id)


def load_config(args) -> Config:
    kw: dict = {}
    # 配置文件默认取脚本同目录的 config.json（此前只在显式 --config 时才读，
    # 于是 config.json 形同虚设、真正的配置是代码里的 DEFAULT_*；这里改为默认读它）
    path = getattr(args, "config", None) or str(Path(__file__).resolve().parent / "config.json")
    if path and os.path.isfile(path):
        raw = Path(path).read_bytes().decode("utf-8-sig")
        kw.update(json.loads(raw))
    kw["_config_path"] = path
    for name in ("host", "port"):
        val = getattr(args, name, None)
        if val:
            kw[name] = val
    return Config(**kw)


# ---------------------------------------------------------------------------
# 路由可观测性
# ---------------------------------------------------------------------------
# 只统计「请求 / 失败 / 最近命中哪个通道」——不做位置分类（每个通道命中多少次看成员自己的 ok）
_route_stats = {
    "requests": 0, "failed": 0, "last_channel": None, "last_member": None, "last_route_at": None,
}
_route_history: list = []          # [{t, ok, req, channel, member}]


def _note_route(ok: bool, channel: Optional[int] = None, member: Optional[str] = None):
    _route_stats["requests"] += 1
    if not ok:
        _route_stats["failed"] += 1
    if ok:
        _route_stats["last_channel"] = channel
        _route_stats["last_member"] = member
    _route_stats["last_route_at"] = _hhmmss()
    last = _route_history[-1] if _route_history else None
    if not last or last["ok"] != ok or last.get("channel") != channel:
        _route_history.append({"t": _hhmmss(), "ok": ok, "req": _route_stats["requests"],
                               "channel": channel, "member": member})
        if len(_route_history) > 30:
            _route_history.pop(0)
    label = f"通道{channel} {member}" if ok else "没有通道接住"
    print(f"[route] {label} (requests={_route_stats['requests']} failed={_route_stats['failed']})",
          flush=True)


# ---------------------------------------------------------------------------
# HTTP 客户端与请求改写
# ---------------------------------------------------------------------------
def make_client(cfg: Config) -> httpx.AsyncClient:
    limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=cfg.connect_timeout, read=cfg.read_timeout, write=cfg.write_timeout, pool=None,
        ),
        limits=limits, follow_redirects=True, trust_env=True,
    )


def member_headers(member: Member, incoming: dict) -> dict:
    headers = {
        "Accept": incoming.get("Accept", "text/event-stream"),
        "Content-Type": "application/json",
        "user-agent": "echo-llm-router",
    }
    bearer = member.bearer()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    headers.update(member.headers or {})
    return headers


def rewrite_body(member: Member, body: dict) -> dict:
    """按成员的 body_mode 改写请求体：内网网关原样透传，公网只留标准字段并换模型名。"""
    if member.body_mode == "openai-safe":
        new = {k: v for k, v in body.items() if k in PUBLIC_SAFE_FIELDS}
    else:
        new = dict(body)
    if member.model:
        new["model"] = member.model
    return new


# ---------------------------------------------------------------------------
# 建流：读到首块才算「通」
# ---------------------------------------------------------------------------
async def _establish(cfg: Config, client: httpx.AsyncClient, member: Member,
                     body: dict, incoming: dict):
    """向成员发起请求并读取首块。

    返回 (response, body_iterator, error)。body_iterator 是**单一**迭代器（首块已并入），
    直接逐块消费即可——httpx 响应体只能流一次。
    """
    url = member.endpoint
    payload = rewrite_body(member, body)
    headers = member_headers(member, incoming)
    t0 = time.perf_counter()
    try:
        resp = await client.send(client.build_request("POST", url, json=payload, headers=headers), stream=True)
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"[:160]

    it = resp.aiter_bytes()
    try:
        first = await asyncio.wait_for(anext(it, None), timeout=member.first_byte_timeout)
    except asyncio.TimeoutError:
        await resp.aclose()
        return None, None, f"首字节超时（{member.first_byte_timeout:g}s）"
    except Exception as exc:
        await resp.aclose()
        return None, None, f"{type(exc).__name__}: {exc}"[:160]

    ttfb = int((time.perf_counter() - t0) * 1000)

    async def merged():
        try:
            if first is not None:
                yield first
            async for chunk in it:
                yield chunk
        finally:
            await resp.aclose()

    resp.extensions["echo_ttfb_ms"] = ttfb     # 供上层记录
    return resp, merged(), ""


def _stream_upstream(response: httpx.Response, body_iterator, extra_headers=None) -> StreamingResponse:
    async def gen():
        async for chunk in body_iterator:
            yield chunk

    media = (
        "text/event-stream"
        if response.headers.get("content-type", "").startswith("text/event-stream")
        else response.headers.get("content-type", "application/json")
    )
    return StreamingResponse(gen(), status_code=response.status_code, media_type=media,
                             headers=dict(extra_headers or {}))


# ---------------------------------------------------------------------------
# 后台健康探测（L1 TCP + L3 GET /models）
# ---------------------------------------------------------------------------
def _probe_tcp(url: str, timeout: float = 1.5):
    import socket
    from urllib.parse import urlparse
    u = urlparse(url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, int((time.perf_counter() - t0) * 1000), "tcp-ok"
    except Exception as exc:
        return False, int((time.perf_counter() - t0) * 1000), f"{type(exc).__name__}: {exc}"[:80]


async def probe_member(cfg: Config, client: httpx.AsyncClient, member: Member) -> None:
    """一次分层探测：TCP 通不通 → GET {base}/models 是否 200（同时验鉴权）。"""
    if not cfg.probe_enabled:
        return
    member.last_probe_at = _hhmmss()
    loop = asyncio.get_running_loop()
    tcp_ok, tcp_ms, tcp_detail = await loop.run_in_executor(None, _probe_tcp, member.endpoint)
    if not tcp_ok:
        member.reachable = False
        member.detail = f"网络不可达（{tcp_detail}）"
        return
    try:
        resp = await client.get(member.models_url, headers=member_headers(member, {}),
                                timeout=cfg.probe_timeout)
    except Exception as exc:
        member.reachable = False
        member.detail = f"探测失败：{type(exc).__name__}"
        return
    if resp.status_code == 200:
        member.reachable = True
        if not member.breaker_open():
            member.detail = f"正常（探测 {tcp_ms}ms）"
    elif resp.status_code in (401, 403):
        member.reachable = False
        member.detail = f"凭据无效（HTTP {resp.status_code}）"
    elif resp.status_code in (404, 405):
        # 端点不支持模型列表：不能据此判死，TCP 通即视为可能可用
        member.reachable = None
        member.detail = f"端点不支持 /models（HTTP {resp.status_code}），需真实请求判定"
    else:
        member.reachable = False
        member.detail = f"探测异常（HTTP {resp.status_code}）"


async def _probe_loop(cfg: Config, client: httpx.AsyncClient):
    while True:
        try:
            members = cfg.all_members()
            await asyncio.gather(*[probe_member(cfg, client, m) for m in members], return_exceptions=True)
        except Exception as exc:                      # 探测失败绝不能拖垮服务
            print(f"[probe] 探测循环异常: {exc}", file=sys.stderr, flush=True)
        await asyncio.sleep(cfg.probe_interval)


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------
def create_app(cfg: Config):
    app = FastAPI(title="echo-llm-router", version="2.0.0")
    # 同源守卫：本服务是 OpenAI 兼容端点、且自动带上你真实的 key，
    # 若允许任意网页调用，等于给外部页面一个"免费用你额度"的入口。
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app import netguard
        netguard.install(app)
    except Exception as e:      # 独立部署（脱离 ECHO 仓库）时不致命，但要能看见
        print(f"[router] 未能装载来源守卫 netguard: {e}", file=sys.stderr)
    client = make_client(cfg)
    probe_task: dict = {}

    @app.on_event("startup")
    async def _startup():
        if not cfg.probe_enabled:
            print("[probe] 后台探测已关闭（probe_enabled=false），健康表只由真实请求驱动", flush=True)
            return
        # 启动即并发探一遍，让首屏就有健康快照（DNS 失败 ~20ms 就出结论）
        await asyncio.gather(*[probe_member(cfg, client, m) for m in cfg.all_members()],
                             return_exceptions=True)
        probe_task["t"] = asyncio.create_task(_probe_loop(cfg, client))

    @app.on_event("shutdown")
    async def _shutdown():
        task = probe_task.get("t")
        if task:
            task.cancel()
        await client.aclose()

    @app.get("/health")
    async def health():
        groups = []
        for g in cfg.groups.values():
            members = [m.to_dict() for m in g.ordered()]
            act = g.active()
            groups.append({
                "id": g.id,
                "display_name": g.display_name,
                "require_token": g.require_token,
                "members": members,
                "active": len(act),
                "healthy": sum(1 for m in act if m.reachable is not False and not m.breaker_open()),
            })
        return Response(media_type="application/json; charset=utf-8",
                        content=json.dumps({
                            "status": "ok",
                            "service": "echo-llm-router",
                            "version": "2.0.0",
                            "groups": groups,
                            "routes": dict(_route_stats),
                            "history": list(_route_history),
                        }, ensure_ascii=False))

    @app.get("/models")
    async def models():
        data = []
        for g in cfg.groups.values():
            data.append({"id": g.id, "object": "model", "owned_by": "echo-router",
                         "name": g.display_name})
            for m in g.ordered():
                if m.model and not any(d["id"] == m.model for d in data):
                    data.append({"id": m.model, "object": "model", "owned_by": g.id})
        return Response(media_type="application/json; charset=utf-8",
                        content=json.dumps({"object": "list", "data": data}, ensure_ascii=False))

    @app.get("/")
    async def dashboard():
        path = Path(__file__).resolve().parent / "dashboard.html"
        try:
            html = path.read_text(encoding="utf-8")
        except OSError:
            html = "<h1>echo-llm-router</h1><p>dashboard.html missing</p>"
        return Response(content=html, media_type="text/html; charset=utf-8")

    # ---- 管理面（只给本机 ECHO 面板用；有令牌就校验令牌）----
    def _admin_allowed(request: Request) -> bool:
        want = resolve_credential(TOKEN_REF)
        if not want:
            return True
        got = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        return got == want

    def _unauthorized() -> Response:
        return Response(status_code=401, media_type="application/json",
                        content=json.dumps({"error": {"message": "echo-router: invalid router token"}}))

    @app.post("/admin/reload")
    async def admin_reload(request: Request):
        """面板改完 config.json 后调用：原地换组，不重启进程。"""
        if not _admin_allowed(request):
            return _unauthorized()
        ok, detail = cfg.reload()
        print(f"[admin] reload {'ok' if ok else 'fail'}: {detail}", flush=True)
        return Response(status_code=200 if ok else 500,
                        media_type="application/json; charset=utf-8",
                        content=json.dumps({"ok": ok, "detail": detail}, ensure_ascii=False))

    @app.post("/admin/probe")
    async def admin_probe(request: Request):
        """面板点「立即探测」：不等下一轮，马上把所有成员探一遍。"""
        if not _admin_allowed(request):
            return _unauthorized()
        await asyncio.gather(*[probe_member(cfg, client, m) for m in cfg.all_members()],
                             return_exceptions=True)
        groups = []
        for g in cfg.groups.values():
            groups.append({"id": g.id, "display_name": g.display_name,
                           "members": [m.to_dict() for m in g.ordered()]})
        return Response(media_type="application/json; charset=utf-8",
                        content=json.dumps({"ok": True, "groups": groups}, ensure_ascii=False))

    @app.api_route("/{path:path}", methods=["POST"])
    async def proxy_post(path: str, request: Request):
        if not (path.endswith("chat/completions") or path.endswith("/completions")):
            return Response(status_code=404, content='{"error":"not found"}', media_type="application/json")
        try:
            body = await request.json()
        except Exception:
            return Response(status_code=400, content='{"error":"invalid json"}', media_type="application/json")

        incoming = {k: v for k, v in request.headers.items()}
        group = cfg.resolve_group(body.get("model") or "")
        if group is None:                    # 未知 model id：直接说清楚，别乱派给别的组
            return Response(status_code=404, media_type="application/json; charset=utf-8",
                            content=json.dumps({"error": {"message":
                                f"echo-router: 没有名为 {body.get('model')!r} 的模型组"
                                f"（可用：{', '.join(sorted(cfg.groups)) or '无'}）"}},
                                ensure_ascii=False))

        # 组级令牌校验（ECHO AUTO 默认开启）
        if group.require_token:
            want = resolve_credential(TOKEN_REF)
            got = (incoming.get("authorization") or "").removeprefix("Bearer ").strip()
            if want and got != want:
                return Response(status_code=401, media_type="application/json",
                                content=json.dumps({"error": {"message": "echo-router: invalid router token"}}))

        ordered = group.active()
        if not ordered:                      # 组里一个成员都没启用 → 明确报错，别静默乱派
            return Response(status_code=503, media_type="application/json; charset=utf-8",
                            content=json.dumps({"error": {"message":
                                f"echo-router: 组 {group.id} 没有启用中的成员"}}, ensure_ascii=False))
        usable = [m for m in ordered if m.reachable is not False and not m.breaker_open()]
        if not usable:                       # 健康表说全挂 → 照样按序试，别把活路挡死
            usable = ordered
        plan = [m.name for m in usable]

        for idx, member in enumerate(usable):
            resp, iterator, err = await _establish(cfg, client, member, body, incoming)
            if resp is None:
                member.mark_fail(err or "连接失败", cfg.breaker_threshold, cfg.breaker_cooldown)
                print(f"[route] {group.id}: {member.name} 失败（{err}）→ 下一个", flush=True)
                continue
            if resp.status_code >= 500 or group.should_failover(resp.status_code):
                await resp.aclose()
                member.mark_fail(f"HTTP {resp.status_code}", cfg.breaker_threshold, cfg.breaker_cooldown)
                print(f"[route] {group.id}: {member.name} HTTP {resp.status_code} → 下一个", flush=True)
                continue

            ttfb = int(resp.extensions.get("echo_ttfb_ms", 0))
            member.mark_ok(ttfb)
            _note_route(True, member.priority, member.name)
            try:
                # HTTP 头只能是 latin-1：通道昵称可能含中文，这里做百分号编码
                return _stream_upstream(resp, iterator, extra_headers={
                    "X-ECHO-Route": quote(f"{member.priority}-{member.name}", safe=""),
                    "X-ECHO-Channel": str(member.priority),
                    "X-ECHO-Group": group.id,
                    "X-ECHO-TTFB-Ms": str(ttfb),
                })
            except Exception:
                await resp.aclose()          # 组装响应失败别把上游连接漏着
                raise

        _note_route(False)
        detail = "；".join(f"通道{m.priority} {m.name}: {m.last_error or m.detail}" for m in ordered)
        return Response(
            status_code=503, media_type="application/json",
            content=json.dumps({"error": {
                "message": f"echo-router: 模型组 {group.id} 所有通道都不可用（尝试顺序 {plan}）",
                "detail": detail,
            }}, ensure_ascii=False),
            headers={"X-ECHO-Channel": "none", "X-ECHO-Group": group.id},
        )

    return app, client


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ECHO LLM 路由（模型组按优先级派发）")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", default=None, help="JSON 配置文件路径")
    parser.add_argument("--check", action="store_true", help="打印生效配置并退出")
    args = parser.parse_args()

    cfg = load_config(args)
    if args.check:
        print(json.dumps({
            "host": cfg.host, "port": cfg.port,
            "connect_timeout": cfg.connect_timeout,
            "first_byte_timeout": cfg.first_byte_timeout,
            "groups": {
                g.id: {
                    "display_name": g.display_name,
                    "require_token": g.require_token,
                    "members": [{
                        "name": m.name, "priority": m.priority, "endpoint": m.endpoint,
                        "model": m.model, "body_mode": m.body_mode,
                        "has_token": bool(m.bearer()),
                    } for m in g.ordered()],
                } for g in cfg.groups.values()
            },
        }, ensure_ascii=False, indent=2))
        return

    import uvicorn

    # 单实例：本进程历史上被两个启动方同时拉起过（启动文件夹的「ECHO 模型路由」快捷方式
    # 与 ECHO 的 boot 组件）——结果一个 bind 成功，另一个不监听却活着占资源。
    # 用内核级锁串行化：重复实例在这里立即退出，不再走到 uvicorn。
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.single_instance import acquire as _acquire_single_instance
    _ok, _detail = _acquire_single_instance(
        "echo-router", os.path.dirname(os.path.abspath(__file__)))
    if not _ok:
        print(f"检测到另一个模型路由实例已在运行（{_detail}），本实例退出", flush=True)
        return

    app, _client = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
