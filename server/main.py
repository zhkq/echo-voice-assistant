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
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server import __version__, engines
from server import auth as auth_mod
from server import calls as calls_mod
from server import errors as E
from server import routes as routes_mod
from server import settings as settings_mod
from server import tmp
from server.pool import EnginePool, ModelSpec

log = logging.getLogger("echo.server")


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


def _normalize_scopes(raw: str) -> str:
    """把 `"asr,diarize"` / `"asr diarize"` / `"asr  diarize"` 统一成空格分隔。

    存的格式就是 `_check_scope` 里 `.split()` 认的那种；不统一的话，
    `--set-scopes cli-x asr,diarize` 会存成一个**永远匹配不上任何槽**的字符串 ——
    表现是"我明明给了权限却全 403"，很难查。
    """
    parts = [p for p in str(raw or "").replace(",", " ").split() if p]
    return " ".join(parts)


def _fmt_time(ts) -> str:
    ts = float(ts or 0)
    return "-" if ts <= 0 else time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _print_pairing(code: str, cfg, name: str = "", scopes: str = "") -> None:
    host = str(cfg.get("server.listen", ""))
    ttl_min = int(float(cfg.get("auth.pairing_ttl_s", 900)) / 60)
    print("配对码（%d 分钟内有效，只能用一次）：" % ttl_min)
    # 配对串里带上服务端标识，客户端粘一次就够了（§7.5 ①）。
    # **证书指纹**（§7.5 的 `fp=`）等 TLS 落地时再加 —— 现在写上去是假的。
    print("     echo://pair?host=%s&code=%s" % (host, code))
    if name or scopes:
        print("这台客户端将建为：名字=%s  scopes=%s"
              % (name or "(对端自报)", scopes or "(不限)"))
    print("把它给同事，在客户端面板里粘贴一次即可。")


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


def _admin_cli(cfg, args) -> int:
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
            code = a.create_pairing_code(created_by=args.created_by or "cli",
                                         name=name, scopes=scopes)
            _print_pairing(code, cfg, name=name, scopes=scopes)
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
            rows = store.pairing_codes()
            now = time.time()
            if not rows:
                print("（没有待用的配对码）")
            for r in rows:
                left = float(r["expires_at"]) - now
                print("%-10s 剩余 %5.1f 分钟  名字=%-16s scopes=%-16s 由 %s 发" % (
                    "(哈希)", max(0.0, left / 60), r.get("name") or "(对端自报)",
                    r.get("scopes") or "(不限)", r.get("created_by") or "-"))
            if rows:
                print("注：配对码**只存哈希**，所以这里看不到明文 —— 明文只在生成时出现过一次。")
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
        if args.revoke:
            ver = a.cache.revoke(args.revoke)
            print("已撤销 %s（token_version → %s）。"
                  "服务端最迟 %s 秒后发现（跨进程靠轮询，见设计 §7.5 ④）。"
                  % (args.revoke, ver, cfg.get("auth.revoke_poll_s", 5)))
            return 0
        if args.disable or args.enable:
            cid = args.disable or args.enable
            a.set_disabled(cid, bool(args.disable))
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
            a.set_scopes(args.set_scopes, scopes)
            print("已把 %s 的 scopes 改成：%s" % (args.set_scopes, scopes or "(不限)"))
            print("**下一个请求就生效** —— 鉴权读的是库里的行，不是 JWT 里的声明。")
            return 0
        if args.set_quota:
            minutes = float(args.daily_audio_minutes or 0)
            a.set_quota(args.set_quota, minutes)
            if minutes > 0:
                print("已把 %s 的每日音频上限设成 %.1f 分钟。" % (args.set_quota, minutes))
            else:
                print("已把 %s 的每日音频上限设成 0（= 用全局默认，也就是 %s 分钟；0 表示不限）。"
                      % (args.set_quota, cfg.get("limits.daily_audio_minutes", 0)))
            # 两件必须说清楚的事，否则会被理解成"改了立刻全网生效、并且已用量归零"：
            print("注意：① **今天的已用量不清零**（额度是按自然日算的），只有上限变了；"
                  "上限的下一个请求就生效（鉴权缓存到期最迟 %s 秒）。"
                  % cfg.get("auth.client_cache_ttl_s", 60))
            print("      ② 用量计数在**进程内**，所以多实例部署时各实例各算一份"
                  "（设计 §7.2 写明的取舍）。")
            return 0
        if args.rotate_secret:
            secret = a.rotate_secret(args.rotate_secret)
            print("已轮换 %s 的 secret。**新的明文只出现这一次**：" % args.rotate_secret)
            print("     %s" % secret)
            print("同时 token_version +1：**它原来的令牌立刻全失效**，必须重新配对"
                  "（v1 不留宽限期，见设计 §7.5 ⑤）。")
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
    args = ap.parse_args(argv)

    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = settings_mod.load(args.config or None)
    if args.listen:
        cfg.raw.setdefault("server", {})["listen"] = args.listen

    admin_actions = (args.new_client, args.new_pairing_code, args.list_clients,
                     args.list_codes, args.show_client, args.revoke, args.disable,
                     args.enable, args.set_scopes, args.rotate_secret, args.set_quota,
                     args.stats, args.list_calls)
    if any(admin_actions):
        return _admin_cli(cfg, args)

    import uvicorn
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level=str(args.log_level))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
