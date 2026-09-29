# -*- coding: utf-8 -*-
"""服务端配置：一份 YAML + 环境变量覆盖。

**刻意独立于客户端配置**：客户端读 `settings` 表（SQLite），服务端读文件。
两边共用一个配置层，迟早会把业务配置项拖到服务端来。

默认值取自 `docs/ECHO能力后端-服务端设计.md` §10：

  * `max_concurrent: 6`      —— 服务端总通道（2026-09-23 定 2；2026-09-29 用户要求提到 6，
    并且**改由管理面配置**：管理面的值优先于这里与环境变量，见 `server/limits.py`）
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
        # 管理面（设计 §8.4）的独立监听地址。**空 = 管理面关着**（出厂值）。
        # 独立端口的理由：防火墙规则一条就够（`客户端网段 → 8900`、`运维网段 → 8901`），
        # 比在同一个端口上做路径级 ACL 可靠。
        # 2026-09-25 起管理面**可写**（发授权 / 禁用 / 撤销 / 改权限与配额 / 轮换 secret），
        # 全部要求管理员会话 + 同站 Origin + 非简单请求标志头 `X-ECHO-Admin` + CSRF；
        # 管理员账号本身仍然只在命令行改（见 server/admin.py 开头那七道闸）。
        # 容器里必须用 `ECHO_ADMIN_LISTEN` 覆盖成 `0.0.0.0:8901`（绑回环则宿主进不来），
        # 而**暴露范围由 Docker 的端口发布决定**：`server/compose.yaml` 把它只发布到
        # 宿主回环（`127.0.0.1:8901:8901`），所以实际可达范围与"绑回环"一致。
        "admin_listen": "",
        "instance_id": "default",       # 临时目录的命名空间（多实例互不清扫）
        "vram_budget_mb": 0,            # 0 = 不限制（CPU-only 或显存充足）
        # **耐久**状态（鉴权库）放哪。**故意与 `tmp.root` 分开**：
        # tmp 是"随便删"的（还会被清理器扫、可以挂 tmpfs 换性能），
        # 而鉴权库存着客户端凭据 —— 放一起意味着"照文档把 tmp 换成 tmpfs
        # 就会把配过的所有客户端清空"。两种相反的保留策略不能共用一个目录。
        "state_root": "",               # 空 = {ECHO}/data/server-state
        # **本机自配对**（2026-09-28，会议转写方案 1）：启动时在鉴权库旁边写一份
        # `local-pair.json`，同机的客户端读它就能自动配对（不用抄配对码）。
        # **默认关**，两个理由：
        #   ① 对"多人共用一台 GPU 后端"（方案 2）它没有意义 —— 那台机器上没人跑客户端；
        #   ② 它会在鉴权库里**发一张配对码**（每次启动一张），而"启动是惰性的"是
        #      一个值钱的默认（一堆用例与排障都依赖它）。
        # 方案 1 的交付路径（`compose.yaml` / 后端包）把它设成 **true**。
        # 见 `server/localpair.py` 与 `docs/3.0-设计总览与组件关系.md` §6.6。
        "local_pair": False,
        # **对外公布地址**（2026-09-30 用户实测的 bug）：配对串里 host 写什么。
        # 出厂值**故意是空** —— 空了才走现有的"本机探测 + 那句如实提示"，行为不变。
        #
        # 为什么必须有这一项：`server.listen` 出厂是通配地址 `0.0.0.0:8900`，
        # 那对客户端没有任何意义，于是只能探测。**在容器里探测到的就是 Docker 网桥
        # 地址**（实测 `172.18.0.2` / `172.21.0.2`），而"同事真正连得到哪台机器"
        # 只有部署的人知道 —— 每次都靠人手改串，第一次用必然踩。
        #
        # ⚠️ 这一项是**"每次发码时想公布的地址"**，不是"一次性全局常量"：
        #   * 同事要连 → 填**局域网/对外 IP**（如 `10.100.0.24`）；
        #   * 后端就跑在客户端这台机器上（本机后端）→ 填 **`127.0.0.1`**
        #     （本机客户端永远连得上，换网络/换 IP 都不受影响）；
        #   * 同一台后端同时服务本机与远程 → **发码时各写各的**：本机用 `127.0.0.1`、
        #     远程用局域网 IP（串本身带 host，两张码互不影响；见
        #     `ops.issue_pairing_code(..., advertised=...)`）。
        # 而且**串就是文本，手改 host 就能用**（刻意没做签名/校验，见 `ops.pairing_string`）。
        #
        # 接受格式（`ops.normalize_advertised_host` 归一化，非法值**报错不猜**）：
        #   `10.100.0.24` / `10.100.0.24:8900` / `http://10.100.0.24:8900` 都行
        #   （scheme 会被剥掉；端口不写就用 `server.listen` 的端口）。TLS 开着时
        #   scheme 一律由 TLS 决定 —— 所以这里**不要**写成 `https://…` 去"提前开 TLS"。
        # 环境变量：`ECHO_ADVERTISED_HOST`（别名 `ECHO_PUBLIC_URL`，两者名字不同、
        # 含义一致；同时设时**前者优先**，并按 `priority_notes` 的口径如实提示）。
        "advertised_host": "",
        # TLS（设计 §7.5 ① 的传输层）。**两个都填才启用 https**；
        # 只填一个是配置错误 —— `main` 会**启动就报错**，不静默降级成 http
        # （那是最糟的结果：部署的人以为连的是 https）。见 `main._tls_kwargs`。
        "tls": {
            "certfile": "",
            "keyfile": "",
        },
    },
    "limits": {
        # 服务端总通道。**出厂 6**（2026-09-29 用户要求从 2 提上来；
        # 按卡校准：小卡要下调就在管理面「性能」页签里改，见 `server/limits.py`）。
        # ⚠️ 优先级：**管理面配置 > 环境变量 > 这里**。管理面改过的值存在 state 卷的库里，
        # 重启后仍然覆盖环境变量 —— 这不是 bug，有一条用例钉着（见 `server/limits.py`）。
        "max_concurrent": 6,
        "per_client_concurrent": 1,     # 每客户端（硬性）
        "queue_max": 0,                 # 不排队
        "busy_retry_after_s": 5,        # server_busy 时给客户端的建议等待
        "max_audio_seconds": 1800,
        "max_upload_bytes": 64 * 1024 * 1024,
        "inference_timeout_s": 900,
        "load_timeout_s": 300,          # 等模型加载完的上限
        # 每客户端每日音频分钟数上限；**0 = 不限**（出厂值）。
        # 出厂给 0 的理由：v1 先在单机/小范围用，"一上来就限死"会让人以为服务端坏了。
        # 但它必须**可配、而且有执行者**（`server/quota.py` 与 `--set-quota`），
        # 否则就是设计 §7.2 里那句"没有执行者的话"。
        "daily_audio_minutes": 0,
    },
    "calls": {
        # 调用元数据（设计 §7.3）的异步写入参数。
        # `flush_interval_s` 是"最多憋多久"：请求路径上只做 put_nowait，后台线程攒批写。
        "flush_interval_s": 1.0,
        # 队列上限。满了**丢并计数**（`/v1/health` 的 `metrics.calls.dropped` 看得见）——
        # 审计不该把一次已经成功的转写拖慢或搞失败。
        "max_queue": 1000,
        # 保留天数。**这是"两个写卷保留策略相反"的那一半**（§9.3）：凭据长期留着，
        # 调用记录可以老死（留着的价值随时间迅速下降，而它每天都在长）。0 = 不自动清。
        "retention_days": 30,
    },
    "tmp": {
        "root": "",                     # 空 = 系统临时目录下的 echo-server
        "ttl_hours": 4,                 # 定时清理：删超过这么久的
        "sweep_interval_s": 3600,       # 每小时扫一次
        "max_bytes": 4 * 1024 ** 3,     # 超了就**先删最老的**（不拒绝新请求）
    },
    "models": {
        "root": "",                     # 空 = {ECHO}/models（跟客户端同一个模型库）
        # cuda | cpu。**要 cuda 而没有 CUDA 时直接失败**，不回退 CPU（设计 §3.4）。
        # 写进默认值是为了让它出现在配置清单里 ——
        # 它是"服务端绝不静默降级"这条铁律的开关，不该只藏在 `get(..., "cuda")` 里。
        "device": "cuda",
        "specs": [],                    # 见下方 yaml 示例；空 = 用代码里的默认清单
    },
    "auth": {
        # **默认关**：本机起步（127.0.0.1）不该先跟凭据较劲。
        # 但监听地址不是回环时启动会**大声告警**（见 main.lifespan）——
        # 不配鉴权就等于"谁连上谁能用你的 GPU"，生产必须打开。
        "enabled": False,
        # token = 静态令牌（**v1 起步用**，手工发凭据）
        # jwt   = 配对码 + secret + 短期 JWT（设计 §7.4/§7.5，多人时用这个）
        "mode": "token",
        "tokens": [],                   # [{"client_id": "...", "token": "...", "scopes": "asr"}]]
        # ---- mode=jwt 时才有意义 ----
        # **必须自己配**。刻意不"没配就随机生成一个"：那会让进程一重启
        # 所有客户端全部 401，而现象很难联想到"密钥每次都是新的"。
        # 生成：openssl rand -hex 32
        "jwt_secret": "",
        "token_ttl_s": 3600,            # 短期令牌 1 小时（不做 refresh token）
        "clock_skew_s": 60,             # 内网机器时钟未必准
        "pairing_enabled": True,        # 关掉之后新机器进不来（老的照用）
        "pairing_ttl_s": 900,           # 配对码 15 分钟
        # **本机自配对文件**里那张码的有效期（2026-09-28，会议转写方案 1）。
        # 比 `pairing_ttl_s` 宽得多是刻意的：那个文件就是"同机以后随时来配"用的
        # （客户端可能在后端跑了几周之后才点「检测本机后端」）。每次后端启动都会
        # 重发一张覆盖旧的，所以"长期不用"的代价只是重启一次后端。
        # 见 `server/localpair.py` 与 `docs/3.0-设计总览与组件关系.md` §6.6。
        "local_pair_ttl_s": 7 * 24 * 3600,
        "pair_window_s": 300,           # 失败退避窗口
        "pair_max_failures": 5,         # 窗口内失败几次开始退避
        "cache_ttl_s": 60,              # client 行缓存多久（撤销走主动失效，不靠它）
        "revoke_poll_s": 5,             # 跨进程撤销的发现间隔（设计 §7.5 ④：≤5 秒）
        "default_scopes": "",           # 空 = 新配对客户端没有额外限制
        "db": "",                       # 空 = {tmp.root}/echo-server-auth.db
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


_TRUTHY = ("1", "true", "yes", "on", "y", "t")
_FALSY = ("0", "false", "no", "off", "n", "f")


def _env_bool(name: str) -> Optional[bool]:
    """环境变量里的布尔。**认不出来就返回 None**（当作没设），不猜。

    容器编排工具里 `"false"` 是个字符串，而 Python 里非空字符串都是真 ——
    直接 `bool(os.environ[...])` 会让 `ECHO_AUTH_ENABLED=false` 变成**打开鉴权**。
    这类静默反向是配置层最贵的错。
    """
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return None


def _env_overrides() -> dict:
    """环境变量覆盖（容器里最方便的一层）。

    只认少数几个真正需要按环境变的。契约（哪些变量真的被读）由
    `tests/test_server_contract.py::EnvOverrideTests` 盯着：它会**扫
    `server/compose.yaml` 与 `server/echo-server.example.yaml` 里出现的每一个
    `ECHO_*` 名字**，逐个确认这里真的处理了 —— 写进部署文件的变量名不被读，
    是最容易发生、又最难发现的一种谎（服务照着文档配，行为却完全没变）。
    """
    out: Dict[str, Any] = {}
    if os.environ.get("ECHO_SERVER_ID"):
        out.setdefault("server", {})["id"] = os.environ["ECHO_SERVER_ID"]
    if os.environ.get("ECHO_LISTEN"):
        out.setdefault("server", {})["listen"] = os.environ["ECHO_LISTEN"]
    # 管理面的监听地址。**与 `ECHO_LISTEN` 同一套写法**（空串 = 没设，保留 YAML 里的值）。
    # 容器里的必要性：镜像里的默认值是空（管理面关着），而要开就得绑 `0.0.0.0:8901` ——
    # 绑 `127.0.0.1` 只对**容器自己的**回环生效，宿主上根本连不上（这是"配了却进不去"）。
    # 绑通配**不等于**对网段开放：`server/compose.yaml` 把 8901 只发布到宿主回环。
    # 代价是 `start_admin_server` 会照常打印"监听地址不是回环"那句告警 —— 那是**预期的**，
    # 别去消掉它：在容器外（直接跑进程）绑通配时那句话仍然是该看见的。
    if os.environ.get("ECHO_ADMIN_LISTEN"):
        out.setdefault("server", {})["admin_listen"] = os.environ["ECHO_ADMIN_LISTEN"]
    # 配对串里公布的地址（2026-09-30）。**空串 = 没设**（保留 YAML 里的值），
    # 与上面几个同一套写法 —— 不然环境里一个空变量会把配置文件那行静默抹掉。
    #
    # 两个变量名都收：`ECHO_ADVERTISED_HOST` 是**服务端自己的口径**（与
    # `server.advertised_host` 同名同义）；`ECHO_PUBLIC_URL` 是内网/反代部署里
    # 更常见的叫法（用户/同事一眼知道那是"对外地址"）。同时设时前者优先 ——
    # 一个名字只做一件事，另一个只作别名，免得"两个都设了听谁的"变成猜谜。
    _advertised = (os.environ.get("ECHO_ADVERTISED_HOST")
                   or os.environ.get("ECHO_PUBLIC_URL") or "")
    if _advertised:
        out.setdefault("server", {})["advertised_host"] = _advertised
    if os.environ.get("ECHO_TMP_ROOT"):
        out.setdefault("tmp", {})["root"] = os.environ["ECHO_TMP_ROOT"]
    if os.environ.get("ECHO_STATE_ROOT"):
        out.setdefault("server", {})["state_root"] = os.environ["ECHO_STATE_ROOT"]
    if os.environ.get("ECHO_MODELS_ROOT"):
        out.setdefault("models", {})["root"] = os.environ["ECHO_MODELS_ROOT"]
    if os.environ.get("ECHO_MAX_CONCURRENT"):
        try:
            out.setdefault("limits", {})["max_concurrent"] = int(os.environ["ECHO_MAX_CONCURRENT"])
        except ValueError:
            pass
    if os.environ.get("ECHO_PER_CLIENT_CONCURRENT"):
        try:
            out.setdefault("limits", {})["per_client_concurrent"] = \
                int(os.environ["ECHO_PER_CLIENT_CONCURRENT"])
        except ValueError:
            pass
    # 队列上限。**它现在没有执行者**（v1 不排队，见 `server/limits.py` 末尾）——
    # 这里读它是为了让"部署文件里写了这个变量 ⇒ 真的被读"这条契约不出现例外，
    # 而它与上面两个一样，会被管理面配过的值覆盖。
    if os.environ.get("ECHO_QUEUE_MAX"):
        try:
            out.setdefault("limits", {})["queue_max"] = int(os.environ["ECHO_QUEUE_MAX"])
        except ValueError:
            pass
    if os.environ.get("ECHO_DEVICE"):
        out.setdefault("models", {})["device"] = os.environ["ECHO_DEVICE"]

    enabled = _env_bool("ECHO_AUTH_ENABLED")
    if enabled is not None:
        out.setdefault("auth", {})["enabled"] = enabled
    if os.environ.get("ECHO_AUTH_MODE"):
        out.setdefault("auth", {})["mode"] = os.environ["ECHO_AUTH_MODE"]
    if os.environ.get("ECHO_JWT_SECRET"):
        out.setdefault("auth", {})["jwt_secret"] = os.environ["ECHO_JWT_SECRET"]
    pairing = _env_bool("ECHO_PAIRING_ENABLED")
    if pairing is not None:
        out.setdefault("auth", {})["pairing_enabled"] = pairing
    local_pair = _env_bool("ECHO_LOCAL_PAIR")
    if local_pair is not None:
        out.setdefault("server", {})["local_pair"] = local_pair
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
            return max(1, int(self.get("limits.max_concurrent", 6)))
        except (TypeError, ValueError):
            return 6

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
    def state_root(self) -> str:
        """**耐久**状态的根目录（目前只有鉴权库）。

        与 `tmp_root` 刻意分开，理由见 `DEFAULTS` 里那段注释：
        tmp 会被清理、可以挂 tmpfs，而这里的东西丢了就要重新配对一遍所有客户端。
        """
        root = str(self.get("server.state_root", "") or "").strip()
        if root:
            return root
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "server-state")

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


def install_paths_seam(cfg: Config):
    """把 `app.paths` 的取值源换成服务端配置。**返回一个「还原」函数。**

    `app/paths.py` 的 `_settings_get()` 注释里写着"测试可替换本函数" —— 就是这里用。
    这样 `paths.models_root()` 会返回服务端的模型目录，而**不用改 paths.py 一行**，
    也不用把客户端的 `settings` 表拖进服务端。

    ## ⚠️ 这是**进程内全局** Monkey-patch

    它改的是 `app.paths` 模块上的一个函数，**一旦装上，整个进程的 `paths.*` 都跟着变**
    （那些按客户端设置解析的目录会一并不认账，比如会议目录会返回空、掉回默认目录）。

    这件事在测试里真出过事故（2026-09-24）：一个新测试文件（`test_capabilities_contract`）
    起了真 app 来验客户端适配器，跑完没还原 —— 于是**排在它后面的** `test_config_compat`
    里，"路径跟随用户设置"那条突然红了，报的却是 `paths` 的默认值，
    看现象完全联想不到是**另一个测试文件**留下的全局状态。

    所以这里把"怎么撤销"明确交出去：调用方拿返回值去还原。
    起真 app 的测试**必须**还原（`create_app` 内部会调它，所以测试要自己快照
    `app.paths._settings_get` 再在 tearDown 里放回去）。
    """
    from app import paths

    original = getattr(paths, "_settings_get", None)

    def _get(name: str) -> str:
        if name == "modelsDir":
            return cfg.models_root
        return ""

    paths._settings_get = _get

    def restore() -> None:
        if original is None:
            try:
                delattr(paths, "_settings_get")
            except AttributeError:
                pass
        else:
            paths._settings_get = original

    return restore
