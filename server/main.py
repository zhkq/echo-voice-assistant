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
        app.state.echo = routes_mod.State(cfg, pool, sweeper)

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
            pool.shutdown()

    app = FastAPI(title="ECHO capability backend", version=__version__, lifespan=lifespan)

    @app.exception_handler(E.EchoError)
    async def _echo_error(_request: Request, exc: E.EchoError):
        headers = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(int(exc.retry_after))
        return JSONResponse(status_code=exc.status, content=exc.body(), headers=headers)

    app.include_router(routes_mod.router)
    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ECHO 能力后端")
    ap.add_argument("--config", default=os.environ.get("ECHO_SERVER_CONFIG", ""),
                    help="YAML 配置路径（不给就用内置默认值 + 环境变量）")
    ap.add_argument("--listen", default="", help="覆盖监听地址，如 0.0.0.0:8900")
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args(argv)

    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = settings_mod.load(args.config or None)
    if args.listen:
        cfg.raw.setdefault("server", {})["listen"] = args.listen

    import uvicorn
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level=str(args.log_level))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
