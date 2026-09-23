# -*- coding: utf-8 -*-
"""服务端配置：一份 YAML + 环境变量覆盖。

**刻意独立于客户端配置**：客户端读 `settings` 表（SQLite），服务端读文件。
两边共用一个配置层，迟早会把业务配置项拖到服务端来。

默认值取自 `docs/ECHO能力后端-服务端设计.md` §10：

  * `max_concurrent: 2`      —— 服务端总通道（2026-09-23 定，实验后可能调）
  * `per_client_concurrent: 1` —— 每客户端硬性 1（公平性：一个客户端不许占满）
  * `queue_max: 0`           —— **不排队**：通道满了直接拒，重试由客户端负责
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DEFAULTS: Dict[str, Any] = {
    "server": {
        "id": "echo-backend-1",
        "listen": "0.0.0.0:8900",
        "instance_id": "default",       # 临时目录的命名空间（多实例互不清扫）
        "vram_budget_mb": 0,            # 0 = 不限制（CPU-only 或显存充足）
    },
    "limits": {
        "max_concurrent": 2,            # 服务端总通道
        "per_client_concurrent": 1,     # 每客户端（硬性）
        "queue_max": 0,                 # 不排队
        "busy_retry_after_s": 5,        # server_busy 时给客户端的建议等待
        "max_audio_seconds": 1800,
        "max_upload_bytes": 64 * 1024 * 1024,
        "inference_timeout_s": 900,
        "load_timeout_s": 300,          # 等模型加载完的上限
    },
    "tmp": {
        "root": "",                     # 空 = 系统临时目录下的 echo-server
        "ttl_hours": 4,                 # 定时清理：删超过这么久的
        "sweep_interval_s": 3600,       # 每小时扫一次
        "max_bytes": 4 * 1024 ** 3,     # 超了就**先删最老的**（不拒绝新请求）
    },
    "models": {
        "root": "",                     # 空 = {ECHO}/models（跟客户端同一个模型库）
        "specs": [],                    # 见下方 yaml 示例；空 = 用代码里的默认清单
    },
    "auth": {
        # **默认关**：本机起步（127.0.0.1）不该先跟凭据较劲。
        # 但监听地址不是回环时启动会**大声告警**（见 main.lifespan）——
        # 不配鉴权就等于"谁连上谁能用你的 GPU"，生产必须打开。
        # 配对码 → client_id + secret → 短期 JWT 见设计 §7.4/§7.5（下一步）。
        "enabled": False,
        "mode": "token",                # token（v1）| mtls（预留）
        "tokens": [],                   # [{"client_id": "...", "token": "..."}]；v1 够用
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    """深拷贝后再合并。

    **必须深拷贝**：`dict(base)` 是浅拷贝，嵌套的 section 字典会与 `DEFAULTS`
    共享同一个对象 —— 于是 `cfg.raw["auth"]["enabled"] = True` 会**改到全局默认值**，
    下一次 `load()` 就带上它了。这是 2026-09-23 实测踩到的：
    测试里给一个用例开了鉴权，后面所有用例都跟着要 Bearer 令牌。
    """
    import copy

    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides() -> dict:
    """环境变量覆盖（容器里最方便的一层）。

    只认少数几个真正需要按环境变的：
        ECHO_SERVER_ID / ECHO_LISTEN / ECHO_TMP_ROOT / ECHO_MODELS_ROOT / ECHO_MAX_CONCURRENT
    """
    out: Dict[str, Any] = {}
    if os.environ.get("ECHO_SERVER_ID"):
        out.setdefault("server", {})["id"] = os.environ["ECHO_SERVER_ID"]
    if os.environ.get("ECHO_LISTEN"):
        out.setdefault("server", {})["listen"] = os.environ["ECHO_LISTEN"]
    if os.environ.get("ECHO_TMP_ROOT"):
        out.setdefault("tmp", {})["root"] = os.environ["ECHO_TMP_ROOT"]
    if os.environ.get("ECHO_MODELS_ROOT"):
        out.setdefault("models", {})["root"] = os.environ["ECHO_MODELS_ROOT"]
    if os.environ.get("ECHO_MAX_CONCURRENT"):
        try:
            out.setdefault("limits", {})["max_concurrent"] = int(os.environ["ECHO_MAX_CONCURRENT"])
        except ValueError:
            pass
    return out


@dataclass
class Config:
    raw: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS))

    # ---- 读取 ----------------------------------------------------------------

    def section(self, name: str) -> dict:
        return dict(self.raw.get(name) or {})

    def get(self, path: str, default=None):
        """`limits.max_concurrent` 这种点分路径。"""
        cur: Any = self.raw
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    # ---- 派生 ----------------------------------------------------------------

    @property
    def host(self) -> str:
        return str(self.get("server.listen", "0.0.0.0:8900")).rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        try:
            return int(str(self.get("server.listen", "")).rsplit(":", 1)[1])
        except (IndexError, ValueError):
            return 8900

    @property
    def max_concurrent(self) -> int:
        try:
            return max(1, int(self.get("limits.max_concurrent", 2)))
        except (TypeError, ValueError):
            return 2

    @property
    def per_client_concurrent(self) -> int:
        try:
            return max(1, int(self.get("limits.per_client_concurrent", 1)))
        except (TypeError, ValueError):
            return 1

    @property
    def tmp_root(self) -> str:
        root = str(self.get("tmp.root", "") or "").strip()
        if root:
            return root
        import tempfile
        return os.path.join(tempfile.gettempdir(), "echo-server")

    @property
    def models_root(self) -> str:
        root = str(self.get("models.root", "") or "").strip()
        if root:
            return root
        # 默认跟客户端同一个模型库（{ECHO}/models）—— 服务端通常就装在仓库旁边
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")

    @property
    def specs(self) -> List[dict]:
        return [dict(s) for s in (self.get("models.specs") or [])]


def load(path: Optional[str] = None) -> Config:
    """读配置：默认值 → YAML → 环境变量（后者覆盖前者）。"""
    raw = dict(DEFAULTS)
    if path:
        import yaml
        with open(path, encoding="utf-8") as fh:
            raw = _deep_merge(raw, yaml.safe_load(fh) or {})
    raw = _deep_merge(raw, _env_overrides())
    return Config(raw=raw)


def install_paths_seam(cfg: Config) -> None:
    """把 `app.paths` 的取值源换成服务端配置。

    `app/paths.py` 的 `_settings_get()` 注释里写着"测试可替换本函数" —— 就是这里用。
    这样 `paths.models_root()` 会返回服务端的模型目录，而**不用改 paths.py 一行**，
    也不用把客户端的 `settings` 表拖进服务端。
    """
    from app import paths

    def _get(name: str) -> str:
        if name == "modelsDir":
            return cfg.models_root
        return ""

    paths._settings_get = _get
