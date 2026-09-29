# -*- coding: utf-8 -*-
"""服务端入口：组装 app、装异常处理、管生命周期。

跑起来：

    ECHO_MODELS_ROOT=/opt/echo/models \\
    python -m server.main --config server/echo-server.yaml

设计文档：`docs/ECHO能力后端-服务端设计.md`。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server import __version__, engines
from server import admin as admin_mod
from server import advertised as advertised_mod
from server import auth as auth_mod
from server import calls as calls_mod
from server import errors as E
from server import limits as limits_mod
from server import ops as ops_mod
from server import routes as routes_mod
from server import settings as settings_mod
from server import tmp
from server.pool import EnginePool, ModelSpec

log = logging.getLogger("echo.server")

#: 这两个小工具的实现 2026-09-25 搬去了 `server/ops.py` —— 管理面「发授权」也要拼
#: **一模一样**的配对串（含证书指纹），两处各写一遍必然漂移。
#: 这里保留旧名字：跨层用例（`test_server_contract.CertFingerprintTests`）在用它，
#: 改名只会把"两处实现"换成"两处名字"。
cert_fingerprint = ops_mod.cert_fingerprint
advertised_host = ops_mod.advertised_host


def _is_loopback(listen: str) -> bool:
    host = str(listen or "").rsplit(":", 1)[0]
    return host in ("127.0.0.1", "localhost", "::1")


def build_pool(cfg) -> EnginePool:
    """按配置建池。配置里没写 specs 就用出厂清单（见 engines.default_specs）。"""
    raw = cfg.specs
    specs = [ModelSpec.from_dict(d) for d in raw] if raw else engines.default_specs()
    device = str(cfg.get("models.device", "cuda") or "cuda")
    return EnginePool(
        specs,
        engines.build_loaders(device=device),
        vram_budget_mb=int(cfg.get("server.vram_budget_mb", 0) or 0),
        load_timeout_s=float(cfg.get("limits.load_timeout_s", 300)),
        busy_retry_after=int(cfg.get("limits.busy_retry_after_s", 5)),
    )


def create_app(cfg=None) -> FastAPI:
    cfg = cfg or settings_mod.load()
    # 「对外公布地址」**配错了就在这里炸**（2026-09-30）。
    # 为什么不等到发码那一刻：这是个手改的字面量，而"配了却无效"的症状是
    # **同事照串连不上** —— 那时没人会想到是服务端配置里一个认不出来的地址。
    # 服务端对"配置不对"的一贯立场是**启动就报错**（同 `_tls_kwargs` 的"只配一半"）。
    try:
        ops_mod.normalize_advertised_host(cfg.get("server.advertised_host", ""))
    except ValueError as exc:
        raise SystemExit("server.advertised_host 配错了：%s" % exc)
    # 把 app.paths 的取值源换成服务端配置 —— 这样复用客户端引擎层时，
    # 它找模型走的是**我们的**模型目录（见 settings.install_paths_seam 的说明）。
    settings_mod.install_paths_seam(cfg)
    pool = build_pool(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sweeper = tmp.Sweeper(
            cfg.tmp_root,
            ttl_hours=float(cfg.get("tmp.ttl_hours", 4)),
            interval_s=float(cfg.get("tmp.sweep_interval_s", 3600)),
            max_bytes=int(cfg.get("tmp.max_bytes", 0) or 0),
        )
        # 先清一次残留（上次崩溃留下的），再起定时器
        try:
            sweeper.last = tmp.sweep(cfg.tmp_root,
                                     float(cfg.get("tmp.ttl_hours", 4)),
                                     int(cfg.get("tmp.max_bytes", 0) or 0))
        except Exception:
            pass
        sweeper.start()
        # 鉴权元数据的库（设计 §8.5 的两张表）。**即使 auth.enabled=false 也开** ——
        # 管理面生成配对码时要用它，而"先关鉴权把库开起来"是正常的起步顺序。
        store = auth_mod.open_store(cfg)
        # 运行参数（总并发 / 每客户端并发 / 队列上限）的**管理面配置**（2026-09-29）。
        # 必须在能力面开始收请求之前应用：`State` 造出来之后闸门与 `/v1/capabilities`
        # 现读的就是这份 `cfg`，而"管理面配置 > 环境变量 > 出厂默认"这条优先级
        # 全靠这几行（见 `server/limits.py`）。
        try:
            over = limits_mod.apply_stored(cfg, store)
        except Exception as e:                                 # pragma: no cover - 兜底
            over = {}
            log.warning("管理面配置的运行参数读不出来（按环境变量 / 出厂默认跑）：%s", e)
        if over:
            log.info("运行参数以**管理面配置**为准：%s（存在 state 卷，改它去管理面"
                     "「性能」页签的「运行参数」）",
                     "、".join("%s=%s" % (limits_mod.BY_KEY[k]["api"], v)
                               for k, v in sorted(over.items())))
        for note in limits_mod.priority_notes(cfg, store):
            log.info("%s", note)
        # **对外公布地址**（2026-09-30）：管理面配过的那份压过环境变量 / 配置文件。
        # 与运行参数同一个形状（存在 state 卷、重启还在），而且必须在**发第一张码之前**
        # 应用 —— 本机自配对文件就在下面几行发。非法的值在 `create_app` 里已经炸过。
        try:
            adv = advertised_mod.apply_stored(cfg, store)
        except Exception as e:                                 # pragma: no cover - 兜底
            adv = {}
            log.warning("管理面配置的对外公布地址读不出来（按环境变量 / 配置文件 / 探测跑）：%s", e)
        if adv:
            log.info("配对串里的地址以**管理面配置**为准：%s（存在 state 卷，改它去管理面"
                     "「客户端 / 发授权」页签的「对外公布地址」）", adv.get("advertised_host"))
        for note in advertised_mod.priority_notes(cfg, store):
            log.info("%s", note)
        # **不配会怎样**：说清楚"现在发出去的地址是本机探测到的"。
        # 容器里探测到的就是 Docker 网桥地址（实测 172.18.0.2），同事必然连不上 ——
        # 所以这句话必须**在日志里就能看见**，不能只藏在配对串的提示里。
        _adv_now = advertised_mod.effective(cfg)
        if not _adv_now and not bool(cfg.get("server.local_pair", False)):
            log.warning("没有配 `server.advertised_host`（环境变量 ECHO_ADVERTISED_HOST）—— "
                        "配对串里的地址只能**本机探测**：容器里探测到的是 Docker 网桥地址"
                        "（如 172.18.0.x），同事连不上。对外服务请填**同事能访问到的那个 "
                        "IP**（如 ECHO_ADVERTISED_HOST=10.100.0.24）；"
                        "后端只服务本机时不需要它（走本机自配对文件，地址是 127.0.0.1）。")
        auth_obj = auth_mod.Auth(cfg, store)
        # 跨进程撤销的发现机制（设计 §7.5 ④）。**命令行 `--revoke` 是另一个进程**，
        # 没有它的话，跑着的服务会继续接受已撤销的 JWT 直到缓存自己过期。
        auth_obj.watcher.start()
        # 调用元数据的异步写入（设计 §7.3）：请求路径上只做一次 put_nowait，
        # 后台线程攒批写库。队列满了就丢并计数（审计不该拖慢或搞失败一次已经成功的转写）。
        call_log = calls_mod.CallLog(
            store,
            flush_interval_s=float(cfg.get("calls.flush_interval_s", 1.0)),
            max_queue=int(cfg.get("calls.max_queue", 1000)),
            retention_days=float(cfg.get("calls.retention_days", 30)),
            log=_db_log,
        )
        call_log.start()
        app.state.echo = routes_mod.State(cfg, pool, sweeper, auth=auth_obj,
                                         call_log=call_log)
        # 管理面（设计 §8.4）：**独立端口上的控制台**（2026-09-25 起带写端点，
        # 全部要求管理员会话 + 同站 Origin + 非简单请求标志头，见 `server/admin.py`）。
        # 等 state 造好再起 —— 它读的就是那份状态（池 / 配额账本 / metrics / 调用记录），
        # 而且"撤销 / 禁用 / 改 scopes"能当场让能力面那份鉴权缓存失效（同一个进程）。
        admin_server = None
        try:
            admin_server = admin_mod.start_admin_server(cfg, app.state.echo, log=log)
        except Exception as e:
            log.warning("管理面没起来（不影响能力面）：%s", e)

        # **本机自配对文件**（2026-09-28，会议转写方案 1）：同机客户端不必抄配对码。
        # **默认关**（`server.local_pair`）—— 它会往鉴权库发一张码，而"启动是惰性的"
        # 是值钱的默认；方案 1 的交付路径（compose / 后端包）把它设成 true。
        # 每次启动重发一张覆盖旧的；失败**不拦启动** —— 它是"方便"，不是"必需"。
        if bool(cfg.get("server.local_pair", False)):
            try:
                from server import localpair
                localpair.publish(cfg, auth_obj)
                log.info("本机自配对已写好：%s（同机客户端点「检测本机后端」即可）",
                         localpair.path(cfg))
            except Exception as e:
                log.warning("本机自配对文件没写成功（同机客户端仍可手工粘贴配对串）：%s", e)

        if not bool(cfg.get("auth.enabled", False)) and not _is_loopback(cfg.get("server.listen", "")):
            log.warning("鉴权是关的，而监听地址不是回环 —— 任何能连到这个端口的人都能用你的 GPU。"
                        "生产环境请打开 auth.enabled（配对 + 令牌见设计 §7）。")

        # 常驻模型**后台预热**，不等它 —— 否则 HTTP 端口会被模型加载卡住几十秒，
        # 而 /health 应该**立刻**就能回 200（设计 §9.7）。
        try:
            pool.warm(timeout=0)
        except Exception as e:
            log.warning("预热常驻模型时出错（不影响启动）：%s", e)
        log.info("echo-backend %s 起来了：listen=%s models=%d 并发上限=%d",
                 __version__, cfg.get("server.listen", ""), len(pool.status()),
                 cfg.max_concurrent)
        # GPU 可见性单独吼一声。这台机器上 `models.device: cuda` 而看不到 CUDA 时，
        # **每个**模型都会在加载时被拒（见 engines._assert_device）—— 那是刻意的，
        # 但日志里得能一眼看出根因，而不是只看到一堆 model_failed。
        _device = str(cfg.get("models.device", "cuda") or "cuda")
        if _device == "cuda" and not engines.cuda_available():
            log.warning("models.device=cuda，但本机看不到可用的 CUDA —— 所有模型都会"
                        "在加载时被拒（服务端刻意不回退 CPU）。请检查显卡驱动 / "
                        "容器 --gpus all / nvidia-container-toolkit。")
        try:
            yield
        finally:
            sweeper.stop()
            auth_obj.watcher.stop()
            # 先停记录线程（它退出前会把剩下的写完），再关库 —— 反了就会丢最后一批
            call_log.stop()
            if admin_server is not None:
                admin_server.should_exit = True
            pool.shutdown()
            try:
                store.close()
            except Exception:
                pass

    app = FastAPI(title="ECHO capability backend", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def _record_call(request: Request, call_next):
        """把一次调用的元数据记进 `calls`（设计 §7.3）。**只有元数据，没有内容。**

        为什么放在中间件里而不是三个端点各自写一遍：这是**一处**而不是三处容易漂的地方，
        而且它能拿到端点拿不到的东西（最终状态码、真实总耗时、`request_id`）。
        端点只负责往 `request.state.call_info` 里补它知道的事实（谁调的、哪个模型、
        几秒音频、排队等了多久）。

        **只记已鉴权的调用**（`clientId` 有值才记）：
          * 未鉴权的 401 是扫描器的噪音，不是"谁在用 GPU"；
          * 探针端点（`/health`、`/ready`、`/capabilities`）压根不进这张表 ——
            客户端每 30 秒拉一次 capabilities，记下来只会把库塞满噪音。
        判据直接用「有 scope 的端点」那张表（`routes.ENDPOINT_SCOPES`）—— 它就是
        "哪些端点是能力调用"的权威定义，别在这里再列一遍。
        """
        st = getattr(request.app.state, "echo", None)
        request.state.call_info = {}
        t0 = time.time()
        response = await call_next(request)
        try:
            if st is not None and request.url.path in routes_mod.ENDPOINT_SCOPES:
                info = dict(getattr(request.state, "call_info", {}) or {})
                if info.get("clientId"):
                    duration_ms = int((time.time() - t0) * 1000)
                    st.metrics.observe(request.url.path, response.status_code, duration_ms,
                                       float(info.get("audioSeconds") or 0.0),
                                       str(info.get("errorCode") or ""))
                    st.call_log.record(
                        ts=t0, client_id=str(info.get("clientId") or ""),
                        endpoint=request.url.path,
                        model_id=str(info.get("modelId") or ""),
                        audio_seconds=float(info.get("audioSeconds") or 0.0),
                        queue_wait_ms=int(info.get("queueWaitMs") or 0),
                        duration_ms=duration_ms, status=int(response.status_code),
                        error_code=str(info.get("errorCode") or ""),
                        request_id=str(request.headers.get("x-request-id") or ""))
        except Exception:                     # 记账失败**绝不**影响这次响应
            log.debug("记录调用元数据时出错", exc_info=True)
        return response

    @app.exception_handler(E.EchoError)
    async def _echo_error(_request: Request, exc: E.EchoError):
        headers = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(int(exc.retry_after))
        # 把错误码留给上面那个中间件 —— **异常处理器在中间件内侧**，
        # 中间件只看得到响应、看不到异常；不在这里留一句，`calls` 里就只有状态码、
        # 没有"为什么"（而 §6.3 那套错误码的全部意义就是回答"为什么"）。
        info = getattr(_request.state, "call_info", None)
        if isinstance(info, dict):
            info["errorCode"] = exc.code
        return JSONResponse(status_code=exc.status, content=exc.body(), headers=headers)

    app.include_router(routes_mod.router)
    return app


def _db_log(level: str, source: str, message: str) -> None:
    """给后台线程用的一句日志（`CallLog` 写库失败时报一声）。

    **不落客户端那个 `db` 表** —— 服务端只有一个审计库（`clients` / `pairing_codes` /
    `calls`），往里塞一行"日志"就得再加一张表，而设计 §8.5 说加表要走评审。
    写进进程日志就够了：那条路径的失败是运维信号，不是业务数据。
    """
    getattr(log, level if level in ("debug", "info", "warning", "error") else "info")(
        "[%s] %s", source, message)


def _tls_kwargs(cfg) -> dict:
    """配置 → uvicorn 的 TLS 参数。没配就返回空字典（走 http）。

    **只配了其中一个就报错退出**，绝不静默降级成 http —— 那是这类配置里最糟的结果：
    部署的人以为自己是 https，客户端却在明文上传音频。宁可起不来。
    """
    cert = str(cfg.get("server.tls.certfile", "") or "").strip()
    key = str(cfg.get("server.tls.keyfile", "") or "").strip()
    if not cert and not key:
        return {}
    if not cert or not key:
        raise SystemExit("server.tls 只配了一半（certfile=%r keyfile=%r）："
                         "两个都要填才启用 https。**不会静默按 http 起** —— "
                         "那会让你以为连的是 https。" % (cert, key))
    for path in (cert, key):
        if not os.path.isfile(path):
            raise SystemExit("server.tls 指向的文件不存在：%s" % path)
    return {"ssl_certfile": cert, "ssl_keyfile": key}


def _normalize_scopes(raw: str) -> str:
    """`"asr,diarize"` / `"asr diarize"` / `"asr  diarize"` 统一成空格分隔。

    实现在 `server/ops.py`（管理面的表单同样要认逗号 —— 同一件事只留一份）。
    这里留个薄别名，免得老调用方/脚本找不到它。
    """
    return ops_mod.normalize_scopes(raw)


def _fmt_time(ts) -> str:
    ts = float(ts or 0)
    return "-" if ts <= 0 else time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _fmt_remaining(seconds) -> str:
    """剩余时间：**不到 1 分钟就报秒**。

    "0.0 分钟"看着像"这张码已经不能用了"，而 `--list-codes` 里列出来的每一张都还能用
    （`ops.pending_pairing_codes()` 已经把过期的滤掉了）—— 2026-09-29 用户实测时
    在 .30 上看到的就是一行"剩余 0.0 分钟"，那是**过期**的行摆在"待用"表里。
    现在那些行不再出现；剩下可能出现的 0.x 分钟就如实报秒，不再有二义。
    """
    sec = int(seconds or 0)
    return ("%d 秒" % sec) if sec < 60 else ("%.1f 分钟" % (sec / 60.0))


def _print_pairing(code, cfg, name: str = "", scopes: str = "") -> None:
    """打印配对码与那一整串 `echo://pair?…`。

    `code` 可以是 `ops.issue_pairing_code()` 的**结果**（推荐：那时 URL 已按这次真正的
    有效期拼好），也可以是一个裸的码。两种都从 `ops` 那一处取同一串 ——
    命令行与管理面看到的配对串必须逐字相同（`fp=` / scheme 少一个，客户端就连不上，
    而且看不出原因）。
    """
    issued = code if isinstance(code, dict) else ops_mod.pairing_string(cfg, str(code))
    ttl_min = int(float(issued.get("ttlSeconds") or 0) / 60)
    print("配对码（%d 分钟内有效，只能用一次）：" % ttl_min)
    print("     %s" % issued["url"])
    if issued.get("note"):
        print("     （%s）" % issued["note"])
    if not issued.get("fingerprint"):
        print("     （没配 TLS，所以串里没有 fp= —— 客户端这次只能 TOFU，即第一次见谁信谁）")
    if name or scopes:
        print("这台客户端将建为：名字=%s  scopes=%s"
              % (name or "(对端自报)", scopes or "(不限)"))
    print("把它给同事，在客户端面板里粘贴一次即可。")


def cert_fingerprint(certfile: str) -> str:
    """证书指纹 `sha256:<hex>`。**实现在 `server/ops.py`**（管理面同一条路要用）。

    这里保留这个名字只是为了不打断老调用方与跨层用例
    （`test_server_contract.CertFingerprintTests.test_the_fingerprint_matches_the_client_side_implementation`）。
    """
    return ops_mod.cert_fingerprint(certfile)


def advertised_host(listen: str, tls: bool) -> tuple:
    """`server.listen` → 客户端能用的 `scheme://host:port`。**实现在 `server/ops.py`**。"""
    return ops_mod.advertised_host(listen, tls)


def _show_client(store, client_id: str) -> int:
    r = store.client(client_id)
    if r is None:
        print("没有这个客户端：%s" % client_id)
        return 1
    print("client_id     %s" % r["client_id"])
    print("名字          %s" % (r["name"] or "(未命名)"))
    print("scopes        %s" % (r["scopes"] or "(不限)"))
    print("状态          %s" % ("已禁用" if r["disabled"] else "正常"))
    print("token_version %s   （撤销/轮换一次 +1；JWT 里带着它）" % r["token_version"])
    print("创建          %s" % _fmt_time(r["created_at"]))
    print("最后改动      %s" % _fmt_time(r["updated_at"]))
    print("最后活跃      %s" % _fmt_time(r["last_seen"]))
    return 0


def read_password_from_stdin() -> str:
    """从 **stdin** 读一行口令。返回去掉行尾的原文（**空串 = 那一行是空的 / 没有输入**）。

    ## 为什么口令只走 stdin

    命令行参数会同时落在两个别人看得见的地方：**shell 历史**（`~/.bash_history`、
    审计日志、`set -x` 的 trace）与 **进程参数**（同机器上任何用户 `ps aux` 就看得到）。
    所以这里**刻意没有** `--password xxx` 这种写法 —— 它方便一次，泄漏一辈子。

    ## 无 tty 时的行为（**不卡住**）

    * stdin 接着管道（`printf '%s\\n' "$PW" | …`、`docker exec -i`、`ssh -t`）：
      一次 `readline()` 就拿到那一行，读到 `\\n` 即返回；
    * stdin 是 `/dev/null`、已关闭、或根本没有终端：`readline()` **立刻**返回空串
      —— 于是调用方按"口令不合规：新口令不能为空"**非零退出**，
      **不会**挂在那里等一个永远不会来的回车。

    唯一会等的情况是"人在终端前但还没敲回车"（tty 上 `readline()` 本来就会等）——
    那正是我们想要的。提示语由调用方写 **stderr**（`stdout` 只放"这条命令说了什么"，
    这样 `$(…)` / `| tee` 抓输出时不掺进提示）。
    （`readline()` 读到 EOF 会抛 `EOFError`，那只在 Python 3.13+ 才这么做；这里连它
    一起兜住 —— 拿到的仍然是空串，落到同一条"口令不能为空"的拒绝路径上。）

    ⚠️ **不做"隐藏输入"那套**：`getpass` 在无 tty 时会回落成"把输入回显出来"
    （实测 Python 会打一句 `GetPassWarning` 到 stderr），而管道进来的口令本来也不该
    被回显。想要"看不见地输入"就用 `ssh -t` + `read -s`（见 `docs/后端容器部署.md`），
    那是**外壳**该管的事，不是这条命令该假装有的能力。
    """
    try:
        line = sys.stdin.readline()
    except Exception:                                          # pragma: no cover - 兜底
        return ""
    return str(line or "").rstrip("\r\n")


#: 「`--set-password` 到底有没有读到过 stdin」的哨兵。用 `None` 是因为
#: 空串是一个**有意义的输入**（空口令该被口令策略挡下，而不是"跳过去"）。
#: `--set-password` **不给默认值**是有意的：参数里不存口令，
#: 口令只在 `main()` 里从 stdin 读一次、直接作为实参传给 `_admin_cli`。
_PASSWORD_NOT_READ = None


def _admin_cli(cfg, args, password: str = _PASSWORD_NOT_READ) -> int:
    """管理动作的命令行入口。

    **为什么是命令行而不是网页**：管理面（设计 §8.4）还没做，而
    `/v1/pair` 需要一次性配对码、只能由服务端这边生成 ——
    没有这条路，鉴权装上了也发不出第一份凭据，等于没装。
    网页版要独立端口 + 管理员密码哈希 + 会话 + CSRF（§8.4 的"管理面自己的安全"），
    那是另一块工作；**运维真正需要的那几个动作先把这里补齐**。

    这些动作**不打 HTTP**：直接开库。理由很实在 ——
    给管理动作开一条免鉴权的内部端点，正是最容易变成漏洞的做法。

    ⚠️ **这条命令的安全边界就是"能读到鉴权库文件"**（也就是 shell 权限）。
    所以它不需要再问一遍密码；但也意味着**别把库文件放到别人读得到的地方**。
    """
    store = auth_mod.open_store(cfg)
    try:
        a = auth_mod.Auth(cfg, store)
        scopes = _normalize_scopes(args.scopes)
        # 命令行是**另一个进程**，读的是同一个 state 卷 —— 所以管理面配过的
        # 「对外公布地址」必须在这里也生效，否则会出现最难查的那种不一致：
        # **网页上发出来的串和管理面配的地址一致，命令行发出来的却还是探测结果**。
        # （运行参数不用在这里应用：命令行不判并发。）
        try:
            advertised_mod.apply_stored(cfg, store)
        except Exception:                                      # pragma: no cover - 兜底
            pass

        # ---- 先统一处理"要指定一个客户端"的动作 ----
        # 不做这一步的话，不存在的 id 会一路走到 `Auth.set_disabled` 里抛
        # `EchoError`，然后以一段 traceback 收场 —— 对运维来说那是噪音，
        # 而且**说的事一样**（没有这个客户端）。`--revoke` 更糟：它会打印
        # "已撤销 xxx（token_version → 0）"，看着像成功了。
        target = (args.show_client or args.revoke or args.disable or args.enable
                  or args.set_scopes or args.rotate_secret or args.set_quota)
        if target:
            row = store.client(target)
            if row is None:
                print("没有这个客户端：%s" % target)
                ids = [r["client_id"] for r in store.clients()]
                print("现有的：%s" % (", ".join(ids) if ids else "（还没有任何客户端）"))
                return 1

        # ---- 新建客户端 / 发配对码 ----
        if args.new_client or args.new_pairing_code:
            name = str(args.new_client or "")
            issued = ops_mod.issue_pairing_code(cfg, a, created_by=args.created_by or "cli",
                                               name=name, scopes=scopes)
            _print_pairing(issued, cfg, name=name, scopes=scopes)
            return 0

        # ---- 查看 ----
        if args.list_clients:
            rows = store.clients()
            if not rows:
                print("（还没有任何客户端）")
            for r in rows:
                print("%-12s %-9s ver=%-3s scopes=%-18s 最后活跃=%-16s %s" % (
                    r["client_id"],
                    "已禁用" if r["disabled"] else "正常",
                    r["token_version"], r["scopes"] or "(不限)",
                    _fmt_time(r["last_seen"]), r["name"]))
            return 0
        if args.list_codes:
            # 与管理面「待用码」那一页共用同一个整理函数（`ops.pending_pairing_codes`）——
            # 两个出口**同一份判据**：只有「未消费 且 未过期」的码会出现在这里。
            # 所以这里不会再出现"剩余 0.0 分钟"的行（那以前是过期码的样子）。
            rows = ops_mod.pending_pairing_codes(store)
            if not rows:
                print("（没有待用的配对码）")
            for r in rows:
                print("%-10s 剩余 %-9s 名字=%-16s scopes=%-16s 由 %s 发" % (
                    "(哈希)", _fmt_remaining(r["remainingSeconds"]),
                    r["name"] or "(对端自报)",
                    r["scopes"] or "(不限)", r["createdBy"] or "-"))
            if rows:
                print("注：配对码**只存哈希**，所以这里看不到明文 —— 明文只在生成时出现过一次。")
                print("注：用掉/过期的码不在这张表里；用掉的留痕在审计里（pair-redeem）。")
            return 0
        if args.show_client:
            return _show_client(store, args.show_client)

        # ---- 统计（只读）----
        # 这两个动作是管理面（v3）要用的同一份查询，先在命令行落地：运维现在就能回答
        # "谁在吃 GPU / 谁在被打回 / 今天多少分钟音频"，不必等网页。
        if args.stats:
            hours = float(args.since_hours or 0)
            since = 0.0 if hours <= 0 else time.time() - hours * 3600.0
            s_ = store.calls_summary(since)
            span = "全部" if since <= 0 else "最近 %.1f 小时" % hours
            print("调用汇总（%s）：共 %d 次，其中失败 %d 次，音频 %.1f 分钟"
                  % (span, s_["total"], s_["errors"], s_["audioSeconds"] / 60.0))
            if not s_["groups"]:
                print("  （这段时间没有任何调用记录）")
            for g in s_["groups"]:
                print("  %-14s %-18s %6d 次  失败 %-4d 音频 %7.2f 分钟  "
                      "平均 %6d ms  p95 %6d ms  最慢 %6d ms"
                      % (g["client_id"] or "(无)", g["endpoint"], g["calls"], g["errors"],
                         float(g["audio_seconds"] or 0) / 60.0, int(g["avg_ms"] or 0),
                         int(g["p95_ms"] or 0), int(g["max_ms"] or 0)))
            print("注：p95 是「这一组里第 95 百分位那条的耗时」"
                  "（SQLite 没有百分位函数；数据量小、够看）—— 别当成严格分位数。")
            return 0
        if args.list_calls:
            rows = store.recent_calls(limit=int(args.list_calls))
            if not rows:
                print("（还没有调用记录）")
            for r in rows:
                print("  %s  %-14s %-18s %-10s %6.1fs  %5d ms  %s%s"
                      % (_fmt_time(r["ts"]), r["client_id"] or "(无)", r["endpoint"],
                         r["model_id"] or "-", float(r["audio_seconds"] or 0),
                         int(r["duration_ms"] or 0), r["status"],
                         ("  " + r["error_code"]) if r["error_code"] else ""))
            print("注：这张表里**只有元数据**（设计 §7.3）—— 没有音频、文本、嵌入、说话人数。")
            return 0

        # ---- 改 ----
        # 从这里往下的**每一个**动作都走 `server/ops.py` —— 管理面的写端点调的是
        # 同一批函数（`ops.revoke_client` / `ops.set_client_disabled` / …）。
        # 两个出口共用一份判断与落库，漂移就无从发生。
        if args.revoke:
            out = ops_mod.revoke_client(cfg, a, args.revoke)
            print("已撤销 %s（token_version → %s）。"
                  "服务端最迟 %s 秒后发现（跨进程靠轮询，见设计 §7.5 ④）。"
                  % (args.revoke, out["tokenVersion"], out["revokePollSeconds"]))
            return 0
        if args.disable or args.enable:
            cid = args.disable or args.enable
            ops_mod.set_client_disabled(cfg, a, cid, bool(args.disable))
            if args.disable:
                print("已禁用 %s。它现在会收到 403（不是 401 —— 是「认识你但不许用」）。"
                      % cid)
            else:
                # 注意：`disabled` 与 `token_version` 是两回事 ——
                # 启用只是把那一位置回 0，**它手上那个令牌仍然是有效的**
                # （版本号没变、scopes 没变），不需要重新换。别写成"要重新换令牌"。
                print("已启用 %s。它手上的凭据仍然有效，可以直接继续用。" % cid)
            return 0
        if args.set_scopes:
            out = ops_mod.set_client_scopes(cfg, a, args.set_scopes, scopes)
            print("已把 %s 的 scopes 改成：%s" % (args.set_scopes, out["scopes"] or "(不限)"))
            print("**下一个请求就生效** —— 鉴权读的是库里的行，不是 JWT 里的声明。")
            return 0
        if args.set_quota:
            minutes = float(args.daily_audio_minutes or 0)
            ops_mod.set_client_quota(cfg, a, args.set_quota, minutes)
            if minutes > 0:
                print("已把 %s 的每日音频上限设成 %.1f 分钟。" % (args.set_quota, minutes))
            else:
                print("已把 %s 的每日音频上限设成 0（= 用全局默认，也就是 %s 分钟；0 表示不限）。"
                      % (args.set_quota, cfg.get("limits.daily_audio_minutes", 0)))
            # 两件必须说清楚的事，否则会被理解成"改了立刻全网生效、并且已用量归零"：
            print("注意：① **今天的已用量不清零**（额度是按自然日算的），只有上限变了；"
                  "② 上限**最多 %s 秒**后才生效 —— 鉴权把客户端那一行缓存了那么久"
                  "（2026-09-24 实测：改完立刻打还是 200，等缓存过期才是 429）。"
                  "要它马上生效就重启服务端。"
                  % cfg.get("auth.client_cache_ttl_s", 60))
            print("      ② 用量计数在**进程内**，所以多实例部署时各实例各算一份"
                  "（设计 §7.2 写明的取舍）。")
            return 0

        if args.set_advertised_host or args.clear_advertised_host:
            # 「对外公布地址」（2026-09-30）—— 与管理面那条路**同一份实现**
            # （`server/advertised.py`：归一化、校验、落库）。
            # 它落在 state 卷的库里，所以命令行改完**服务端进程下一次发码就现读**到
            # （与运行参数同口径：管理面配置 > 环境变量 > 自动探测）。
            try:
                value = advertised_mod.set_advertised(
                    cfg, store, {"value": args.set_advertised_host},
                    updated_by=args.created_by or "cli")
            except E.EchoError as exc:
                print("改不了：%s" % (exc.detail or exc.message))
                return 2
            if value:
                print("已把对外公布地址设成：%s" % value)
                print("发码时配对串里的 host 就用它（端口没写的话用 listen 的端口）。")
            else:
                print("已清空对外公布地址 —— 回到**自动探测**（容器里探测到的是 Docker "
                      "网桥地址，同事连不上；对外服务请填同事能访问到的 IP）。")
            print("⚠️ 这一项是「每次发码时想公布的地址」，不是一次性全局常量："
                  "本机后端该用 127.0.0.1，同事要连就该用局域网 IP —— "
                  "两条路同时存在时发码各写各的。")
            return 0
        if args.show_advertised_host:
            view = advertised_mod.view(cfg, store)
            print("对外公布地址：%s" % (view["value"] or "（没配 —— 发码时本机探测）"))
            print("来源          %s" % view["source"])
            print("此刻发码会用  %s" % (view["url"] or "（读不出来）"))
            if view["note"]:
                print("              %s" % view["note"])
            print("说明          %s" % view["formNote"])
            return 0

        # ---- 管理面账号（设计 §8.4）----
        # 管理面能做的**只有一件事：改"自己"的口令**（`POST /admin/api/password`），而且
        # **必须先给出当前口令** —— 会话被劫持的人卡在这一步（口令一改，别的会话同时失效）。
        # 管理面**仍然不做**：新建 / 删除 / 禁用 / 启用管理员，以及改**别人**的口令 ——
        # 那是"谁能进门"的事，不该在门里做。（面板上那份管理员清单仍是只读的。）
        # 所以建账号 / 禁用 / 删除只在这里，与其他写动作同一条出口（能开库 = shell 权限）。
        # 口径与 `server/admin.py` 的 `selfServiceNote` 一致；用例见
        # `tests/test_admin_console.py::PasswordChangeConsoleTests`。
        if args.new_admin:
            pwd = admin_mod.new_password()
            store.upsert_admin(args.new_admin, admin_mod.hash_password(pwd))
            store.audit("cli", "new-admin", args.new_admin)
            print("已建（或重置）管理员 %s。**口令只出现这一次**：" % args.new_admin)
            print("     %s" % pwd)
            # 这一句是"普通运维自己记得住的口令"那条路的路标：不给它，
            # 人只会看到一串随机串，然后回去手搓 python 脚本（2026-10-02 用户绕了两回）。
            print("想自己指定一个记得住的口令：`python -m server.main --set-password %s`"
                  "（**从 stdin 读**，不进命令行历史与进程参数）" % args.new_admin)
            print("管理面板：http://%s/admin/ （要先在配置里设 server.admin_listen）"
                  % (cfg.get("server.admin_listen", "") or "127.0.0.1:8901"))
            return 0

        # ---- 指定口令（从 stdin 读；**绝不从命令行参数取**）----
        #
        # 为什么要有它：装完后端时 `--new-admin` 只给一串**随机**口令、只打印一次 ——
        # 想用"自己记得住的"就只能回去手搓一段 python（`settings.load` → `auth.open_store`
        # → `admin.hash_password` → `store.upsert_admin`）。那对普通运维是太高的一道坎，
        # 而"记得住的口令"是**正当需求**（不是要绕过什么）。
        #
        # 三条边界（都是刻意的）：
        #   ① **口令只从 stdin 来**。命令行参数会进 shell 历史（`~/.bash_history`）
        #      与 `ps` 的进程参数 —— 那是把口令写在墙上。所以这里**没有** `--password`。
        #   ② 走**与 `--new-admin` 完全相同的一条路**：`admin.hash_password()`（scrypt）
        #      + `store.upsert_admin()`，没有第二套哈希。
        #   ③ 策略**复用** `admin.password_policy_error()`（`MIN_PASSWORD_LEN` /
        #      `MAX_PASSWORD_LEN` 就是它读的那两个常量）—— 不合规**当场退出**：
        #      既不落库（`store.upsert_admin` 根本没被调用），也没有审计行 ——
        #      "试了一个不合规的口令"不该在审计里留下痕迹。
        if args.set_password:
            # ⚠️ **不区分"没有 stdin"与"stdin 里那一行是空的"**：`readline()` 在
            # `</dev/null`、已关闭的 fd 上返回的都是空串，与 `printf '' | …` 在**读回来
            # 的字节**上完全一样 —— 硬要分开只能去问 `isatty()`，而那个判断在这里没有
            # 价值（两种情况下要做的都是"拒绝并说清楚"，而不是"换一条路找口令"）。
            # 所以只给一句话，并且它是**策略那句话**（`新口令不能为空`）：
            # 用户真正需要知道的是"口令没成、库没动、再看一眼管道"。
            pwd = password if password is not None else ""
            reason = admin_mod.password_policy_error(pwd)
            if reason:
                # 这几行打 stdout，但**不是机器可读输出**：这条命令的全部输出就是
                # 给人看的中文。真正要紧的是**退出码非 0**（脚本靠它判断）。
                print("口令不合规：%s" % reason)
                print("（没有落库 —— 库里原来那一行没动。）")
                if not pwd:
                    print("--set-password 只从 **stdin** 读一行，口令本身要在那一行里。例如：",
                          file=sys.stderr)
                    print("    ssh -t <宿主> 'docker exec -i echo-backend python -m "
                          "server.main --set-password %s'   # 然后按提示输入"
                          % args.set_password, file=sys.stderr)
                    print("    printf '%%s\\n' \"$口令\" | docker exec -i echo-backend "
                          "python -m server.main --set-password %s" % args.set_password,
                          file=sys.stderr)
                return 2
            # 与 `--new-admin` **同一格语义**：**建或重置**（不存在就建）。
            # 刻意不做成"只许改已有的"：那要多一个 `--create` 开关，而两种语义都有人
            # 期待（"重置口令" vs "建账号并指定口令"）；`--new-admin` 本来就是 upsert，
            # 这里跟着它走，两条路的心智模型才是同一个。
            # ⚠️ 也别在这里加"名字打错了就自动建一个"的额外防线 —— 那一层做不了
            # （命令行没有"谁是谁"的概念，安全边界就是 shell 权限，见模块头）。
            store.upsert_admin(args.set_password, admin_mod.hash_password(pwd))
            store.audit("cli", "set-password", args.set_password)
            print("已建（或重置）管理员 %s，口令就是你输入的那个（%d 位）。"
                  % (args.set_password, len(pwd)))
            print("库里只有 scrypt 哈希 —— 这个口令**没有**出现在命令行参数里，"
                  "也不会被再次打印。")
            print("管理面板：http://%s/admin/ （容器里请先 `ssh -L 8901:127.0.0.1:8901`，"
                  "管理面只发布在宿主回环）"
                  % (cfg.get("server.admin_listen", "") or "127.0.0.1:8901"))
            return 0
        if args.list_admins:
            rows = store.admins()
            if not rows:
                print("（还没有管理员账号 —— 管理面登录会一直失败，用 --new-admin 建一个）")
            for r in rows:
                print("%-16s %-8s 建号 %-16s 最后登录 %s"
                      % (r["username"], "已禁用" if r["disabled"] else "正常",
                         _fmt_time(r["created_at"]), _fmt_time(r["last_login"])))
            return 0
        if args.disable_admin or args.enable_admin:
            name = args.disable_admin or args.enable_admin
            if store.admin(name) is None:
                print("没有这个管理员：%s" % name)
                return 1
            store.set_admin_disabled(name, bool(args.disable_admin))
            store.audit("cli", "disable-admin" if args.disable_admin else "enable-admin", name)
            print("已%s管理员 %s。" % ("禁用" if args.disable_admin else "启用", name))
            print("（禁用之后他手上的会话**下一个请求就失效** —— 管理面每请求都重读账号行。）")
            return 0
        if args.delete_admin:
            if not store.delete_admin(args.delete_admin):
                print("没有这个管理员：%s" % args.delete_admin)
                return 1
            store.audit("cli", "delete-admin", args.delete_admin)
            print("已删除管理员 %s。" % args.delete_admin)
            return 0
        if args.rotate_secret:
            grace = float(args.grace_hours or 0)
            cid = args.rotate_secret
            out = ops_mod.rotate_client_secret(cfg, a, cid, grace_hours=grace)
            print("已轮换 %s 的 secret。**新的明文只出现这一次**：" % cid)
            print("     %s" % out["secret"])
            print("同时 token_version +1：**它原来那些令牌立刻全失效**"
                  "（泄漏时最想立刻断掉的就是它们）。")
            if grace > 0:
                # 宽限期里旧 secret 还能换令牌 —— 所以这时候客户端**不需要**立刻做什么。
                # 但服务端只存哈希、发不出新 secret（设计原话"secret 只出现这一次"），
                # 所以"自动换新"做不到；能做的是**同时给一张新配对码**，让运维在宽限期内
                # 把新凭据交出去。
                name = str(out.get("name") or "")
                scopes_of = str(out.get("scopes") or "")
                print("宽限期 %.1f 小时内，**旧 secret 仍然能换令牌**：客户端不会断。"
                      % grace)
                print("⚠️ 但这把旧 secret 也照样进得来 —— **所以宽限期不能用于"
                      "「secret 泄漏」**，只用于例行轮换不打断客户端。泄漏请用默认"
                      "（不带 --grace-hours），那样旧的立刻失效。")
                print("再给你一张配对码，趁宽限期内把新凭据交给那台机器：")
                issued = ops_mod.issue_pairing_code(cfg, a, name=name or cid,
                                                   scopes=scopes_of, created_by="cli:rotate")
                _print_pairing(issued, cfg, name=name or cid, scopes=scopes_of)
            else:
                print("**不留宽限期**：旧 secret 立刻失效，它必须重新配对。"
                      "想让旧 secret 多活一阵（例行轮换、不打断客户端），加 "
                      "--grace-hours 24。")
            return 0

        return 2
    finally:
        store.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ECHO 能力后端")
    ap.add_argument("--config", default=os.environ.get("ECHO_SERVER_CONFIG", ""),
                    help="YAML 配置路径（不给就用内置默认值 + 环境变量）")
    ap.add_argument("--listen", default="", help="覆盖监听地址，如 0.0.0.0:8900")
    ap.add_argument("--log-level", default="info")
    # ---- 管理动作（做完就退出，不起服务）----
    # 「新建客户端」这个动作就是**发一张带着名字与 scope 的配对码** ——
    # 与 `--new-pairing-code` 是同一件事，区别只在于填不填名字/scope。
    ap.add_argument("--new-client", default="", metavar="NAME",
                    help="新建客户端：生成一张带着这个名字与 --scopes 的配对码")
    ap.add_argument("--new-pairing-code", action="store_true",
                    help="只发配对码，不预先指定名字与 scope（对端自报）")
    ap.add_argument("--scopes", default="", metavar="LIST",
                    help='权限列表，如 "asr diarize embed"（逗号或空格分隔；空 = 不限）')
    ap.add_argument("--created-by", default="", help="配对码的备注（谁发的）")
    ap.add_argument("--list-clients", action="store_true", help="列出已配对的客户端")
    ap.add_argument("--list-codes", action="store_true", help="列出待用的配对码与剩余时间")
    ap.add_argument("--show-client", default="", metavar="CLIENT_ID", help="看一个客户端的详情")
    ap.add_argument("--revoke", default="", metavar="CLIENT_ID",
                    help="撤销一个客户端（token_version +1，立即生效）")
    ap.add_argument("--disable", default="", metavar="CLIENT_ID",
                    help="禁用一个客户端（它收到 403，凭据仍然有效）")
    ap.add_argument("--enable", default="", metavar="CLIENT_ID", help="重新启用")
    ap.add_argument("--set-scopes", default="", metavar="CLIENT_ID",
                    help="改 scopes，配合 --scopes（下一个请求就生效）")
    ap.add_argument("--rotate-secret", default="", metavar="CLIENT_ID",
                    help="换 secret 并打印新的（只出现这一次）；旧令牌立即失效")
    ap.add_argument("--grace-hours", default="0", metavar="H",
                    help="配合 --rotate-secret：宽限期内**旧 secret 仍可换令牌**"
                         "（0 = 旧的立刻失效）。⚠️ 宽限期不能用于 secret 泄漏")
    ap.add_argument("--set-quota", default="", metavar="CLIENT_ID",
                    help="改这个客户端的每日音频分钟数上限（配合 --daily-audio-minutes）")
    ap.add_argument("--daily-audio-minutes", default="0", metavar="N",
                    help="每日音频分钟数；0 = 用全局默认（limits.daily_audio_minutes）")
    ap.add_argument("--stats", action="store_true",
                    help="看一段时间内的调用汇总（谁在用、错了多少、多少分钟音频）")
    ap.add_argument("--since-hours", default="24", metavar="H",
                    help="配合 --stats：看最近多少小时（默认 24；0 = 全部）")
    ap.add_argument("--list-calls", type=int, default=0, metavar="N",
                    help="看最近 N 条调用元数据（**只有元数据，没有内容**）")
    # ---- 对外公布地址（配对串里的 host；2026-09-30）----
    # 与管理面「客户端 / 发授权」页签那张卡是同一件事的**两个出口**
    # （`server/advertised.py` 一份实现）。
    #
    # ⚠️ 两个开关都用 `store_true` 会有一个很难看的坑：`args.set_advertised_host or
    # args.clear_advertised_host` 里的**布尔 `True` 会被 `str()` 成 `"True"` 当成地址**
    # 写进配置（写这段时当场踩到，用例抓住的）。所以清空那个也**带一个值**
    # （`--clear-advertised-host yes`），让两者都是字符串。
    ap.add_argument("--set-advertised-host", default="", metavar="HOST[:PORT]",
                    help="发配对码时公布的地址（`10.100.0.24` / `10.100.0.24:8900` / "
                         "`http://10.100.0.24:8900`）。同事要连就填局域网 IP；"
                         "本机后端该填 127.0.0.1；不填则自动探测")
    ap.add_argument("--clear-advertised-host", default="", metavar="yes", nargs="?",
                    const="yes", help="清空这一项，回到自动探测")
    ap.add_argument("--show-advertised-host", action="store_true",
                    help="看当前对外公布地址、来源，以及此刻发码会用哪个地址")
    # ---- 管理面（设计 §8.4）的账号：管理面**只读展示**清单，所以账号只能从这里建 ----
    ap.add_argument("--new-admin", default="", metavar="NAME",
                    help="建管理员账号（或重置其口令）；**口令是随机生成的，只打印这一次**。"
                         "想自己指定口令用 --set-password")
    # ⚠️ **没有 `--password`**（这是刻意的，别"顺手加上"）：口令当命令行参数会进
    # shell 历史与 `ps` 的进程参数。要指定就 `--set-password NAME` + stdin。
    ap.add_argument("--set-password", default="", metavar="NAME",
                    help="建（或重置）管理员并把口令设成 **stdin 里的那一行** ——"
                         "与 --new-admin 同一条路（scrypt 哈希 + upsert），"
                         "区别只是口令由你给、而不是随机生成。例：`ssh -t 宿主 "
                         "'docker exec -i echo-backend python -m server.main --set-password "
                         "ops'`，或 `echo '口令' | docker exec -i …`。"
                         "不合规（少于 %d 位 / 超过 %d 位 / 为空）→ 中文报错并非零退出，"
                         "不落库" % (admin_mod.MIN_PASSWORD_LEN, admin_mod.MAX_PASSWORD_LEN))
    ap.add_argument("--list-admins", action="store_true", help="看有哪些管理员账号")
    ap.add_argument("--disable-admin", default="", metavar="NAME",
                    help="禁用一个管理员（他手上的会话下一个请求就失效）")
    ap.add_argument("--enable-admin", default="", metavar="NAME", help="重新启用")
    ap.add_argument("--delete-admin", default="", metavar="NAME", help="删掉一个管理员")
    args = ap.parse_args(argv)

    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = settings_mod.load(args.config or None)
    if args.listen:
        cfg.raw.setdefault("server", {})["listen"] = args.listen

    admin_actions = (args.new_client, args.new_pairing_code, args.list_clients,
                     args.list_codes, args.show_client, args.revoke, args.disable,
                     args.enable, args.set_scopes, args.rotate_secret, args.set_quota,
                     args.stats, args.list_calls, args.new_admin, args.set_password,
                     args.list_admins,
                     args.disable_admin, args.enable_admin, args.delete_admin,
                     args.set_advertised_host, args.clear_advertised_host,
                     args.show_advertised_host)
    if any(admin_actions):
        # 两个都给了 = 用法错了（"随机生成"与"用我给的"是同一格的两种填法）。
        # **不当场二选一**：那会把"我打错了"变成"口令不是你给的那个"，最难查的那种。
        if args.new_admin and args.set_password:
            ap.error("--new-admin 与 --set-password 只能给一个：前者生成随机口令，"
                     "后者用 stdin 里那一行（想用自己记得住的口令就只给 --set-password）")
        # **先读 stdin 再进 `_admin_cli`**：这样"没读到口令 / 口令不合规"的失败路径
        # 一步都不会走到 `open_store()` 之后的写动作 —— 不落库、不写审计。
        # 判定本身留在 `_admin_cli` 里（那里离 `upsert_admin` 最近，用的也是那份
        # 与 `--new-admin` 共用的 store 对象）。
        password = _PASSWORD_NOT_READ
        if args.set_password:
            print("请输入管理员 %s 的新口令（从 stdin 读一行，回车结束；"
                  "`|` 进来的那一行同样可以）：" % args.set_password, file=sys.stderr)
            password = read_password_from_stdin()
        return _admin_cli(cfg, args, password)

    import uvicorn
    tls = _tls_kwargs(cfg)
    if tls:
        log.info("以 https 启动（证书 %s）", tls["ssl_certfile"])
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port,
                log_level=str(args.log_level), **tls)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
