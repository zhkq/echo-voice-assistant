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
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server import __version__, engines
from server import auth as auth_mod
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
        app.state.echo = routes_mod.State(cfg, pool, sweeper, auth=auth_obj)

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
            pool.shutdown()
            try:
                store.close()
            except Exception:
                pass

    app = FastAPI(title="ECHO capability backend", version=__version__, lifespan=lifespan)

    @app.exception_handler(E.EchoError)
    async def _echo_error(_request: Request, exc: E.EchoError):
        headers = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(int(exc.retry_after))
        return JSONResponse(status_code=exc.status, content=exc.body(), headers=headers)

    app.include_router(routes_mod.router)
    return app


def _admin_cli(cfg, args) -> int:
    """管理动作的命令行入口。

    **为什么要有它**：`/v1/pair` 需要一次性配对码，而配对码只能由"服务端这边"生成。
    管理面（设计 §8.4）还没做，所以在那之前必须有一条命令行的路 ——
    否则这套鉴权装上了却发不出第一份凭据，等于没装。

    这些动作**不打 HTTP**：它们直接开库。理由很实在 ——
    给管理动作开一条免鉴权的内部端点，正是最容易变成漏洞的做法。
    """
    store = auth_mod.open_store(cfg)
    try:
        a = auth_mod.Auth(cfg, store)
        if args.new_pairing_code:
            code = a.create_pairing_code(created_by=args.created_by or "cli")
            host = str(cfg.get("server.listen", ""))
            # 配对串里带上服务端标识，客户端粘一次就够了（§7.5 ①）。
            # **证书指纹**（§7.5 的 `fp=`）等 TLS 落地时再加 —— 现在写上去是假的。
            print("配对码（15 分钟内有效，只能用一次）：")
            print("     echo://pair?host=%s&code=%s" % (host, code))
            print("把它给同事，在客户端面板里粘贴一次即可。")
            return 0
        if args.list_clients:
            rows = store.clients()
            if not rows:
                print("（还没有任何客户端）")
            for r in rows:
                print("%-12s ver=%-3s disabled=%-5s scopes=%-20s name=%s" % (
                    r["client_id"], r["token_version"], bool(r["disabled"]),
                    r["scopes"] or "(不限)", r["name"]))
            return 0
        if args.revoke:
            ver = a.cache.revoke(args.revoke)
            print("已撤销 %s（token_version → %s）。"
                  "服务端最迟 %s 秒后发现（跨进程靠轮询，见设计 §7.5 ④）。"
                  % (args.revoke, ver, cfg.get("auth.revoke_poll_s", 5)))
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
    ap.add_argument("--new-pairing-code", action="store_true",
                    help="生成一个一次性配对码并打印配对串（管理面做好之前的入口）")
    ap.add_argument("--created-by", default="", help="配对码的备注（谁发的）")
    ap.add_argument("--list-clients", action="store_true", help="列出已配对的客户端")
    ap.add_argument("--revoke", default="", metavar="CLIENT_ID",
                    help="撤销一个客户端（token_version +1，立即生效）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = settings_mod.load(args.config or None)
    if args.listen:
        cfg.raw.setdefault("server", {})["listen"] = args.listen

    if args.new_pairing_code or args.list_clients or args.revoke:
        return _admin_cli(cfg, args)

    import uvicorn
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level=str(args.log_level))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
