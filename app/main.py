# -*- coding: utf-8 -*-
"""main.py — ECHO 服务入口（FastAPI 应用工厂 + 启动）

启动流程：建库/迁移 → 种子配置 → 写 pid → 状态上报 → 按配置拉起监听器。
静态托管：/ 与 /web/* 指向 web/（控制面板 SPA）。
运行：python -m app.main  （默认 http://127.0.0.1:8970）
"""
import os
import sys
import threading
import time
from contextlib import asynccontextmanager

from app import paths

# 输出被 -RedirectStandardOutput 重定向到文件后默认是块缓冲，
# 导致 print 日志迟迟不落盘。改成行缓冲，日志即时可见。
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

# 原生崩溃兜底诊断：注册 SIGSEGV/SIGABRT 等处理器，进程崩溃（访问冲突/
# 堆损坏等）时把各线程 Python 栈写入 faulthandler.log，用于定位崩溃点。
# （原生库内部崩溃可能不触发，但能覆盖大部分 Python→原生调用栈场景。）
try:
    import faulthandler
    # 日志属于**数据根**（D18）：Windows 下 = {ECHO}/data（与 1.x 同址，老用户无感），
    # macOS 下 = ~/Library/Application Support/ECHO（不能写进 .app 里）。
    _FH_PATH = os.path.join(paths.data_root(), "logs", "faulthandler.log")
    os.makedirs(os.path.dirname(_FH_PATH), exist_ok=True)
    with open(_FH_PATH, "a", encoding="utf-8") as _fh:
        _fh.write(f"\n===== ECHO 启动 {__import__('datetime').datetime.now()} =====\n")
        _fh.flush()
    faulthandler.enable(open(_FH_PATH, "a", encoding="utf-8"), all_threads=True)
except Exception:
    pass

from fastapi import FastAPI
from fastapi.responses import FileResponse

import app.db as db
from app import __version__ as ECHO_VERSION
from app.config import settings
from app import manager, paths, ports, runtime, services
from app.api import router
from app.pathutil import safe_under

# 安装根由路径层给（含 ECHO_ROOT 覆盖）；web/ 是代码资产，跟安装根走。
BASE_DIR = paths.echo_root()
WEB_DIR = os.path.join(BASE_DIR, "web")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 阶段 0：只做轻量初始化，面板立即可用 ----
    db.init()
    settings.seed_defaults()

    # D30：HF_HOME 必须赶在**任何** huggingface_hub 代码之前定一次——
    # huggingface_hub 在它自己被 import 时就把缓存路径算死了，之后再改环境变量无效。
    # 落点在 seed_defaults() 之后、boot.setup() 之前；只在用户显式配置了 modelsDir
    # 时才覆盖（paths.hf_home() 返回空串表示“不动”，见该函数与 D30）。
    _hf_home = paths.hf_home()
    if _hf_home:
        os.environ["HF_HOME"] = _hf_home

    manager.echo_write_pid()
    services.report_server()

    import app.boot as boot
    boot.setup()
    boot.start_all_async()   # 后台分阶段拉起其余组件，不阻塞 yield

    # 归档前置自检（后台 best-effort）：确保 DSH 新建会话的默认权限是全盘访问。
    # 笔记库在归档会话的工作区之外，权限不足时首次写入会被沙箱拦下、只能靠 DSH 的
    # "自动提权重试"补救，慢到会被面板判成失败（2026-09-15 事故）。失败只写日志。
    def _worklog_preflight():
        try:
            from app import worklog
            if not worklog.enabled():
                return
            ok, why = worklog.ensure_dsh_default_access()
            db.add_log("info" if ok else "warn", "meeting", f"归档权限自检：{why}")
        except Exception as e:
            try:
                db.add_log("warn", "meeting", f"归档权限自检异常：{e}")
            except Exception:
                pass

    threading.Thread(target=_worklog_preflight, daemon=True, name="worklog-preflight").start()

    # 启动后自动显示折叠条（设置 panelAutoStart / panelStartCollapsed）：
    # 等服务真正开始监听再起——折叠条页面是经 http://127.0.0.1:8970/web/rail.html 同源加载的，
    # 起太早会先撞上连接失败（边条侧虽有重试，但没必要）。已在运行则不打扰：
    # echo-sidebar.exe 是单实例应用，盲目再起一个等于给它发 toggle，会把用户展开的面板收起来。
    def _auto_rail():
        time.sleep(2.5)
        try:
            print("[hotkey] autostart: " + str(runtime.autostart_sidebar()))
        except Exception as e:
            print(f"[hotkey] autostart 异常: {e}")

    threading.Thread(target=_auto_rail, daemon=True, name="auto-rail").start()

    db.add_log("info", "server", "ECHO 面板已启动（后台组件拉起中）")
    yield
    # ---- 关闭 ----
    runtime.stop_all()
    db.add_log("info", "server", "ECHO 服务已停止")


def create_app():
    app = FastAPI(title="ECHO 个人助理", version=ECHO_VERSION, lifespan=lifespan)

    # 本地 API 来源守卫（2026-09-13 安全审计 CRITICAL-1）：
    # CORS 收紧到回环来源 + Host/Origin 中间件拒绝非回环请求（同时封 DNS Rebinding）。
    # 原来是 allow_origins=["*"]，等于「任意网页都能读走会议录音、并 POST 让 ECHO 开麦录音」。
    # 判定细则、攻击面与副作用见 app/netguard.py 顶部注释。
    from app import netguard
    netguard.install(app)
    app.include_router(router)

    # 静态面板：禁用缓存（no-store），避免浏览器缓存旧版 app.js/index.html 导致面板异常
    if os.path.isdir(WEB_DIR):
        from fastapi import HTTPException

        @app.get("/web/{path:path}")
        def web_static(path: str):
            # 防路径穿越
            target = safe_under(WEB_DIR, path)
            if not target or not os.path.isfile(target):
                raise HTTPException(status_code=404, detail="Not Found")
            return FileResponse(target, headers={"Cache-Control": "no-store"})

    @app.get("/")
    def index():
        return FileResponse(os.path.join(WEB_DIR, "index.html"),
                            headers={"Cache-Control": "no-store"})

    # PWA：manifest 与 Service Worker 需从根路径提供（SW scope 覆盖全站）
    @app.get("/manifest.json")
    def pwa_manifest():
        return FileResponse(os.path.join(WEB_DIR, "manifest.json"),
                            headers={"Cache-Control": "no-store"})

    @app.get("/sw.js")
    def pwa_sw():
        return FileResponse(os.path.join(WEB_DIR, "sw.js"),
                            headers={"Cache-Control": "no-store"})

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


app = create_app()


def main():
    import socket
    import uvicorn
    from app import single_instance

    # 防重复实例（第 1 道、权威判定）：内核级单实例锁。
    # 为什么不用端口探测当权威：探测与 uvicorn 真正 bind 之间有竞态窗口，
    # 两个实例同时启动会双双重入 —— 2026-09-18 实测 18060 上有一个在服务、
    # 另一个不监听却活着，还在后台加载 SenseVoice（CPU 空转）。锁由内核持有、
    # 进程退出即释放，没有竞态也没有残留。详见 app/single_instance.py。
    _lock_ok, _lock_detail = single_instance.acquire("echo", db.DATA_DIR)
    if not _lock_ok:
        # 拒绝路径必须"又短又不出错"：不碰数据库、不做任何可能阻塞的事。
        # 2026-09-18 实测教训：这里若调用 db.init()/db.add_log()，在另一个实例
        # 正持着 SQLite 写锁时本进程会**卡住不退**，于是又留下一个"活着但不监听"
        # 的僵尸（CPU 0、内存 6MB）。诊断信息走 stdout，由守护脚本的重定向收走。
        print(f"检测到另一个 ECHO 实例已在运行（{_lock_detail}），本实例退出", flush=True)
        sys.exit(0)

    # 尽早写 pid：让守护脚本（scripts\startup.ps1）能区分"正在启动"与"没在跑"，
    # 从而不会在 ECHO 加载模型的那几十秒里再拉起一个。
    try:
        manager.echo_write_pid()
    except Exception:
        pass

    db.init()
    preferred = int(settings.get("serverPort", 8970))
    # 防重复实例（第 2 道、兜底）：端口上已经有东西在监听。锁只能挡住"也用这把锁的实例"，
    # 挡不住老版本进程或别的程序占着端口，所以这一层保留 —— 但它是兜底，不是权威。
    # 注意：这里**只**判"有人在听"，不判"能不能 bind"（后者见下一段的自动让位）。
    if ports.listening("127.0.0.1", preferred):
        print(f"检测到端口 {preferred} 已被其他 ECHO 实例占用，本实例退出（重复实例）", flush=True)
        sys.exit(0)
    # 端口可能落在 Windows 保留段里：Hyper-V/WSL 会划走动态端口段，且每次重启漂移，
    # bind 报的是 Errno 13（没有权限）而不是"已占用"。这种情况必须自动让位——
    # 否则表现为"进程活着但端口没监听"，从外面完全看不出原因（2026-09-14 的事故）。
    port, note = ports.pick(preferred)
    if not port:
        print(f"[fail] 无法分配监听端口：{note}", flush=True)
        sys.exit(1)
    if note:
        print(f"[warn] {note}（原配置 serverPort={preferred}）", flush=True)
    # 把**实际**监听端口写到 data\echo-port.txt：外部脚本（launch-desktop.ps1 等）
    # 与 DSH 插件据此定位服务，从而不必把端口写死在多处。端口一旦让位，这里必须跟着更新，
    # 否则脚本会去找旧端口。
    if not ports.write_port_file(db.DATA_DIR, port):
        print("[warn] 写入 echo-port.txt 失败", flush=True)
    print(f"ECHO 服务启动: http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
