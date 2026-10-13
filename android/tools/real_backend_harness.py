# -*- coding: utf-8 -*-
"""给 Android App 做「真后端」联调的插座（开发机上手动跑，不属于门禁）。

它做什么：
  1. 用**临时目录**当 ECHO 的数据根（`ECHO_DATA`），**绝不碰真实 data/**；
  2. 直接 import 真的 `app.api.router`，用 uvicorn 在回环的一个临时端口上服务；
  3. 预置一条**真实**的指令记录（走 `db.add_command` + `db.update_command`）；
  4. 用 `phone_pair.issue()` 生成一张**真的**配对码；
  5. 把 `{port, code, expected*}` 写成 JSON，交给 Kotlin 的
     `EchoApiRealBackendTest`（用 `-Decho.realBackend=<该文件>` 打开它）。

为什么需要它：MockWebServer 只能证明「App 发出去的形状对」，
证明不了「真后端收下这个形状、并且吐回来的 JSON 我们的 DTO 认得」。
后者只能拿真 `api.py` 验。

用法：
    <repo>\\venv\\Scripts\\python.exe android\\tools\\real_backend_harness.py --json <路径> [--port 18970]

⚠️ **一个插座只够跑一次测试**：配对码是**一次性**的（5 分钟、用掉即删），
   而 Kotlin 侧的 `sharedToken` 整轮只兑换一次。所以**跑第二遍必须重启插座**
   （2026-10-09 踩到：第二次跑直接 3 个用例红，后端原话是
   "配对码已过期或不存在，请在 ECHO 面板上重新生成"）。
"""

import argparse
import json
import os
import sys
import tempfile

# 仓库根（纯计算，无副作用）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SEED_TEXT = "把客厅灯打开"
SEED_REPLY = "已经打开了客厅的灯。"
SEED_BRIEF = "已开灯"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True, help="把插座信息写到这个文件（给 Kotlin 用例读）")
    ap.add_argument("--port", type=int, default=18970)
    ap.add_argument("--data-root", default="", help="复用某个数据根（默认临时建一个）")
    args = ap.parse_args()

    # 所有副作用都放在 main() 里：本模块被 import 时**什么都不做**
    #（否则谁 import 一下就把 ECHO_DATA 改了，那是最难查的那种污染）
    tmp = args.data_root or os.path.join(tempfile.mkdtemp(prefix="echo-android-harness-"), "data")
    os.environ["ECHO_DATA"] = os.path.abspath(tmp)
    os.makedirs(os.environ["ECHO_DATA"], exist_ok=True)
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    import uvicorn
    from fastapi import FastAPI

    from app import db, phone_pair
    from app.api import router

    # 0) 临时数据根是空的：先建库 + 跑迁移（幂等）
    db.init()

    # 1) 预置一条真实指令：待办 -> 完成，字段覆盖 App 界面真正会读的那几个
    db.add_command(SEED_TEXT, source="mobile", status="sent")
    seeded = db.list_commands(limit=1)[0]
    db.update_command(seeded["id"], status="done", reply=SEED_REPLY,
                      brief=SEED_BRIEF, duration_ms=1234)

    # 2) 生成一张真配对码（直接调 phone_pair，绕开"只有本机面板能生成"的那道闸 ——
    #    那道闸管的是"谁能发钥匙"，不是"钥匙能不能用"）
    issued = phone_pair.issue()

    info = {
        "port": args.port,
        "code": issued["code"],
        "expectedText": SEED_TEXT,
        "expectedReply": SEED_REPLY,
        "expectedBrief": SEED_BRIEF,
        "expectedStatus": "done",
        "expectedId": seeded["id"],
        "dataRoot": os.environ["ECHO_DATA"],
    }
    json_dir = os.path.dirname(os.path.abspath(args.json))
    os.makedirs(json_dir, exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=2)

    # 3) **别让插座碰到用户正在跑的智能体 / 别在这台机器上响 / 别弹通知。**
    #
    #    为什么必须做（2026-10-09 真踩到）：第一次给这个插座加"发指令"用例时，
    #    `_dispatch` 真的连上了本机 43199 上那个**在跑的** harness，还建了一个会话
    #    （那一行 status 停在 `sent`、session_id 是 `session-<uuid>`）。
    #    那等于在别人正在用的助理上插了一句话。
    #
    #    把 client 换成"永远 ping 不通"的替身之后，真实的异步链路**照样全跑**：
    #      后台线程 → 插行(pending) → 判 "DSH 未运行" → 写 failed + error
    #    —— 正是我们想验的那条路，但**一个包都不发到任何真服务**。
    from app import assistant

    class _NoAgentClient:
        """假 client：只有一个 ping()，且恒为 False。"""

        def ping(self):
            return False

    assistant.get_client = lambda: _NoAgentClient()
    # 失败分支会响一声 + 弹 Windows 通知（app/assistant.py:885-887）—— 这是别人的电脑，别响
    assistant.tts_mod.play_beep = lambda *a, **k: None
    assistant.notify = lambda *a, **k: None

    app = FastAPI()
    app.include_router(router)

    # 真机上 `/health` 不在 router 里（main.py:179 单独挂的静态/健康路由），
    # 而 App 现在用它做**探活**（无令牌、无 /api 前缀）。插座要像真机一样提供它，
    # 否则"探活"这条判断在插座上永远走不通。
    @app.get("/health")
    def _health():
        return {"ok": True}

    print("[harness] 数据根（临时）: %s" % os.environ["ECHO_DATA"], flush=True)
    print("[harness] 指令 id=%s，配对码=%s" % (seeded["id"], issued["code"]), flush=True)
    print("[harness] 智能体已打桩（ping 恒 False），不会碰到任何在跑的服务", flush=True)
    print("[harness] 监听 http://127.0.0.1:%d" % args.port, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
