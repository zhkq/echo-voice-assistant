# -*- coding: utf-8 -*-
"""「起本机后端」的面板后台（`app/backend_admin.py` + 三个端点，批 1d）。

这一层要回答的是**人能看懂的三件事**：现在什么状态、能不能点、为什么不能点。
所以用例盯的也是这三件，而不是内部结构：

  * `view()` 的每一个字段都要能被面板直接渲染（`json.dumps` 不炸），而且**该说的都说**
    （缺运行时 / 端口被别人占着 / 已配对到别的后端 / 许可是「不出机」而配对在别处）；
  * `start()` **立刻返回**（编排在后台线程里），进度逐条长在 `job()` 里 ——
    面板就是轮询它显示"正在第几步"；
  * `stop()` 只走进程层那条判据（pid 记录），**手工起的实例不动**。

隔离（照 `tests/test_capability_admin.py` / `tests/test_backend_proc.py`）：
`data/`（库与凭据）、pid/日志目录、后端目录、以及这台机器真实的设置值，全部指向临时对象。
**绝不**去读真实的 `data/logs/backend.pid`，也绝不让用例真的起一个后端。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                      # noqa: E402

import app.db as db                                            # noqa: E402
from app import backend_admin, backend_env, backend_pid, backend_proc, backend_ready, backend_setup  # noqa: E402
from app.api import router as api_router                        # noqa: E402
from app.capabilities import credentials as cred                # noqa: E402
from app.config import settings                                 # noqa: E402


def _app():
    app = FastAPI()
    app.include_router(api_router)
    return app


class _Isolated(unittest.TestCase):
    """把"这台机器真实的状态"隔在外面：库、凭据、pid/日志、后端目录、设置值。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-backend-admin-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "admin.db")
        db.init()
        settings._cache = None
        settings.seed_defaults()
        self.addCleanup(self._restore_db)

        for mod in (backend_proc, backend_setup):
            p = mock.patch.object(mod, "backend_root", lambda: self.root)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(backend_pid, "_logs_dir", lambda: self._logs)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(cred, "credentials_path",
                              lambda: os.path.join(self.tmp, "backend.json"))
        p.start()
        self.addCleanup(p.stop)
        # 设置值按用例给（默认：随客户端停=关、许可=lan）——顺便避免碰真实库。
        # **端口不在这里**：它是后端的设置，客户端要它就去读生成出来的 `server.yaml`
        # （`backend_setup.configured_ports()`，见 `test_the_ports_come_from_the_config`）。
        self.settings_values = {"capabilityBackendStopWithClient": False,
                                "capabilityPrivacy": "lan"}
        p = mock.patch.object(backend_admin, "_setting",
                              lambda k, d=None: self.settings_values.get(k, d))
        p.start()
        self.addCleanup(p.stop)
        backend_admin.reset_job()
        self.addCleanup(backend_admin.reset_job)

    def _restore_db(self):
        db.DATA_DIR, db.DB_FILE = self._old_db
        settings._cache = None

    @property
    def root(self):
        return os.path.join(self.tmp, "backend")

    @property
    def _logs(self):
        return os.path.join(self.tmp, "logs")

    # ---- 便捷 ----

    def write_config(self, port=8900, admin_port=8901):
        """写一份只带端口的 `server.yaml`（够 `configured_ports()` 用）。"""
        os.makedirs(self.root, exist_ok=True)
        with open(backend_setup.config_path(), "w", encoding="utf-8") as fh:
            fh.write('server:\n  listen: "127.0.0.1:%d"\n  admin_listen: "127.0.0.1:%d"\n'
                     % (int(port), int(admin_port)))
        return backend_setup.config_path()

    def install_fake_runtime(self):
        """放一个假的解释器文件，让 `python_exe()` 认为运行时装好了。"""
        path = os.path.join(self.root, "runtime", "Scripts", "python.exe")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        return path

    def record_pid(self, pid):
        self.assertTrue(backend_pid.write_pid(pid))


class ViewTests(_Isolated):
    def test_the_ports_come_from_the_backend_config_not_from_client_settings(self):
        """**端口是后端的设置**：客户端不许存一份（设计 §6.6 的"设置分家"）。

        客户端要这两个端口就去读**生成出来的 `server.yaml`** —— 所以用户手工把
        `listen` 改成别的端口也算数（这一条正是"再存一份"会做错的事：那份副本会说 8900）。
        """
        from app.config import DEFAULTS
        leaked = sorted(k for k in DEFAULTS if k.startswith("capabilityBackend")
                        and ("port" in k.lower() or "admin" in k.lower()))
        self.assertEqual(leaked, [], "客户端设置里出现了后端的端口：%s" % leaked)

        self.write_config(port=8902, admin_port=8903)
        v = backend_admin.view()
        self.assertEqual(v["port"], 8902, "读的应该是配置里那个端口")
        self.assertEqual(v["adminPort"], 8903)
        self.assertEqual(v["baseUrl"], "http://127.0.0.1:8902")

    def test_the_defaults_are_used_before_any_config_exists(self):
        self.assertEqual(backend_admin.ports(), (8900, 8901))

    def test_the_backend_dir_can_be_moved_by_the_client(self):
        """`capabilityBackendDir` 是**客户端挑的位置**（与 modelsDir / meetingsDir 同类）。"""
        from app import paths
        target = os.path.join(self.tmp, "another-disk", "backend")
        settings._cache = None
        settings.update({"capabilityBackendDir": target})
        settings._cache = None
        self.addCleanup(lambda: (settings.update({"capabilityBackendDir": ""}),
                                 setattr(settings, "_cache", None)))
        self.assertEqual(os.path.abspath(paths.backend_root()), os.path.abspath(target))

    def test_view_is_renderable_and_says_why_start_is_disabled(self):
        v = backend_admin.view()
        json.dumps(v, ensure_ascii=False)              # 面板要能直接下发
        for key in ("root", "port", "adminPort", "runtime", "config", "running", "pid",
                    "ports", "pairFile", "paired", "job", "canStart", "whyNot", "notes",
                    "ready"):
            self.assertIn(key, v)
        self.assertFalse(v["runtime"]["ready"], "临时目录里没有 runtime/")
        self.assertFalse(v["canStart"])
        self.assertIn("运行时", v["whyNot"])
        self.assertTrue([n for n in v["notes"] if "运行时" in n],
                        "缺运行时必须在 notes 里说出来：%s" % v["notes"])
        self.assertEqual(v["port"], 8900)
        self.assertEqual(v["adminPort"], 8901)

    def test_a_runtime_that_exists_makes_start_possible(self):
        self.install_fake_runtime()
        v = backend_admin.view()
        self.assertTrue(v["runtime"]["ready"], v)
        self.assertTrue(v["canStart"], v["whyNot"])
        self.assertIn("配对", v["whyNot"], "能起的时候也要说清它接下来会做什么")

    def test_a_running_backend_turns_the_button_into_stop(self):
        self.install_fake_runtime()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (proc.terminate(), proc.wait(timeout=5)))
        self.record_pid(proc.pid)
        v = backend_admin.view()
        self.assertTrue(v["running"], v)
        self.assertEqual(v["pid"], proc.pid)
        self.assertFalse(v["canStart"], "已经在跑时不该还能点「起」")
        self.assertIn("已经在跑", v["whyNot"])

    def test_notes_warn_about_a_pairing_on_another_machine(self):
        """已经配对到**别的**后端 + 许可是「不出机」→ 两条都要说出来。"""
        cred.save(cred.BackendCredentials(base_url="http://gpu-01:8900",
                                          client_id="cli-1", secret="s"))
        self.settings_values["capabilityPrivacy"] = "none"
        v = backend_admin.view()
        self.assertTrue(v["paired"]["paired"])
        joined = " ".join(v["notes"])
        self.assertIn("不是本机后端", joined)
        self.assertIn("不出机", joined)

    def test_a_loopback_pairing_is_not_warned_about(self):
        """配的是**本机**后端时不警告 —— 那正是"不出机"许可允许的那一档。"""
        cred.save(cred.BackendCredentials(base_url="http://127.0.0.1:8900",
                                          client_id="cli-2", secret="s"))
        self.settings_values["capabilityPrivacy"] = "none"
        v = backend_admin.view()
        self.assertNotIn("不是本机后端", " ".join(v["notes"]))

    def test_view_never_raises_when_the_probes_blow_up(self):
        """探针坏了也要给出一份状态（面板不能因为读不到端口就整块白屏）。"""
        with mock.patch.object(backend_proc, "port_check",
                               side_effect=RuntimeError("netstat 炸了")), \
                mock.patch.object(backend_proc, "port_owner",
                                  side_effect=RuntimeError("tasklist 炸了")), \
                mock.patch.object(backend_admin.pairing, "local_pair_state",
                                  side_effect=RuntimeError("配对文件坏了")), \
                mock.patch.object(backend_admin.pairing, "state",
                                  side_effect=RuntimeError("凭据坏了")):
            v = backend_admin.view()
        self.assertIn("canStart", v)
        self.assertEqual(v["ports"][0]["pid"], 0)
        self.assertFalse(v["paired"]["paired"])


class StartTests(_Isolated):
    def test_start_refuses_without_a_runtime(self):
        ok, message = backend_admin.start()
        self.assertFalse(ok, message)
        self.assertIn("运行时", message)

    def test_start_refuses_while_another_job_is_running(self):
        self.install_fake_runtime()
        with mock.patch.dict(backend_admin._JOB, {"running": True, "stage": "launch",
                                                  "steps": [{"name": "configure"}]}):
            ok, message = backend_admin.start()
        self.assertFalse(ok, message)
        self.assertIn("进行中", message)

    def test_start_runs_the_orchestration_in_the_background(self):
        """`start()` 立刻返回；进度逐条进 `job()`；结束时有结论。"""
        self.install_fake_runtime()
        self.write_config(port=8902, admin_port=8903)
        seen = {}

        def _fake_start(**kw):
            seen.update(kw)
            on_step = kw.get("on_step")
            for name, ok, detail in (("configure", True, "配置已写好"),
                                     ("launch", True, "已启动后端 pid=1"),
                                     ("pair", False, "本机配对文件还不存在")):
                if on_step:
                    on_step({"name": name, "ok": ok, "detail": detail})
            return {"ok": False, "message": "本机配对文件还不存在",
                    "steps": [{"name": "configure", "ok": True, "detail": "配置已写好"},
                              {"name": "launch", "ok": True, "detail": "已启动后端 pid=1"},
                              {"name": "pair", "ok": False, "detail": "本机配对文件还不存在"}]}

        with mock.patch.object(backend_setup, "start", _fake_start):
            ok, message = backend_admin.start()
            self.assertTrue(ok, message)
            self.assertIn("已开始", message)
            # 后台线程：轮询等它收尾（**不 sleep 死等**，每 20ms 看一眼）
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and backend_admin.job()["running"]:
                time.sleep(0.02)
        job = backend_admin.job()
        self.assertFalse(job["running"], "线程没在 5 秒内收尾：%s" % job)
        self.assertIs(job["ok"], False)
        self.assertIn("还不存在", job["message"])
        self.assertEqual([s["name"] for s in job["steps"]],
                         ["configure", "launch", "pair"])
        self.assertEqual(job["stage"], "", "跑完不该留着'正在做哪一步'")
        self.assertTrue(job["doneAt"], job)
        # 端口来自**生成出来的配置**，并传给编排
        self.assertEqual(seen.get("port"), 8902)
        self.assertEqual(seen.get("admin_port"), 8903)
        self.assertTrue(callable(seen.get("on_step")))

    def test_a_crashing_orchestration_still_finishes_the_job(self):
        """编排自己炸了也要把 job 收尾（否则面板永远显示"正在起"）。"""
        self.install_fake_runtime()
        with mock.patch.object(backend_setup, "start",
                               side_effect=RuntimeError("boom")):
            ok, _message = backend_admin.start()
            self.assertTrue(ok)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and backend_admin.job()["running"]:
                time.sleep(0.02)
        job = backend_admin.job()
        self.assertFalse(job["running"])
        self.assertIs(job["ok"], False)
        self.assertIn("意外", job["message"])

    def test_a_step_callback_that_throws_does_not_break_the_flow(self):
        """`on_step` 是显示层的事 —— 它抛异常不许把起后端带崩。

        这里直接调**真的** `backend_setup.start(on_step=…)`（第一步就用桩打回失败），
        验的是那条保护本身：回调炸了，编排照旧把结论返回给调用方。
        """
        def _boom(_step):
            raise RuntimeError("面板那边炸了")

        with mock.patch.object(backend_setup, "configure",
                               lambda **kw: (False, "写不了配置", {})):
            res = backend_setup.start(on_step=_boom)
        self.assertFalse(res["ok"])
        self.assertEqual([s["name"] for s in res["steps"]], ["configure"])
        self.assertIn("写不了配置", res["message"])


class StopTests(_Isolated):
    def test_stop_is_refused_while_starting(self):
        with mock.patch.dict(backend_admin._JOB, {"running": True, "stage": "configure",
                                                  "steps": []}):
            ok, message = backend_admin.stop()
        self.assertFalse(ok, message)
        self.assertIn("进行中", message)

    def test_stop_delegates_to_the_process_layer(self):
        self.write_config(port=8902, admin_port=8903)
        seen = {}

        def _stop(reason="", ports=None):
            seen.update({"reason": reason, "ports": tuple(ports or ())})
            return True, "已停止 ECHO 起的后端（pid=1）"

        with mock.patch.object(backend_proc, "stop", _stop):
            ok, message = backend_admin.stop()
        self.assertTrue(ok, message)
        self.assertEqual(seen["ports"], (8902, 8903),
                         "要按配置里的两个端口判断（不是客户端自己的副本）")
        self.assertIn("停掉它", seen["reason"])

    def test_stop_if_configured_honors_the_switch(self):
        calls = []
        with mock.patch.object(backend_proc, "stop",
                               lambda **kw: (calls.append(kw) or (True, "停了"))):
            ok, message = backend_admin.stop_if_configured()
            self.assertTrue(ok, message)
            self.assertIn("是关的", message)
            self.assertEqual(calls, [], "开关关着时不许动手")
            self.settings_values["capabilityBackendStopWithClient"] = True
            ok, message = backend_admin.stop_if_configured()
        self.assertTrue(ok, message)
        self.assertEqual(len(calls), 1, "开关打开时才停一次")
        self.assertIn("ECHO 退出", calls[0]["reason"])


class EndpointTests(_Isolated):
    def setUp(self):
        super().setUp()
        self.client = TestClient(_app())

    def test_get_backend_gives_the_panel_everything(self):
        r = self.client.get("/api/capability/backend")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("canStart", body)
        self.assertIn("job", body)
        self.assertEqual(body["port"], 8900)

    def test_start_without_a_runtime_is_a_400_with_a_readable_line(self):
        r = self.client.post("/api/capability/backend/start", json={})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("运行时", r.json()["detail"])

    def test_start_returns_immediately_with_the_job(self):
        self.install_fake_runtime()
        with mock.patch.object(backend_setup, "start",
                               lambda **kw: {"ok": True, "message": "好了", "steps": []}):
            r = self.client.post("/api/capability/backend/start",
                                 json={"replace_pairing": True, "vram_budget_mb": 7000})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("backend", body)
        self.assertIn("job", body["backend"])

    def test_the_plan_endpoint_is_read_only_and_cached_until_forced(self):
        """批 2 的只读计划：面板点按钮**之前**先给用户看走哪条路、缺什么、先验哪一步。"""
        seen = {}

        def _plan(force=False):
            seen["force"] = force
            return {"path": "portable", "variant": "cu126", "implemented": False,
                    "whyNot": "一键安装还没做", "missing": ["后端的运行时"],
                    "notes": ["扩展包要自带运行时"], "reasons": ["没装 Docker"],
                    "needsNetwork": True, "sizeMb": 10240, "etaMinutes": 15,
                    "diskFreeGB": 100.0, "verify": [], "probe": {}}

        with mock.patch.object(backend_env, "plan", _plan):
            r = self.client.get("/api/capability/backend/plan")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["path"], "portable")
            self.assertIs(seen["force"], False)
            self.client.get("/api/capability/backend/plan?force=true")
            self.assertIs(seen["force"], True)

    def test_the_ready_endpoint_runs_the_three_layer_self_test(self):
        """批 3：`/v1/health` → `/v1/ready` → **一次真实 /v1/asr**。失败回 400 + 那句话。"""
        seen = {}

        def _probe(url, **kw):
            seen.update({"url": url, "kw": kw})
            return {"ok": True, "state": "ok", "at": "10:00:00",
                    "headline": "三层都过了：…", "l3": {"text": "测试"}}

        self.write_config(port=8902, admin_port=8903)
        with mock.patch.object(backend_ready, "probe", _probe):
            r = self.client.post("/api/capability/backend/ready")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("三层都过", r.json()["message"])
        self.assertEqual(seen["url"], "http://127.0.0.1:8902",
                         "自测打的是**本机那个**后端（按生成出来的配置）")
        # 结论要留在 view() 里（面板轮询时显示上一次的结论，而不是每次都跑一次真推理）
        self.assertIn("ready", backend_admin.view())
        self.assertEqual(backend_admin.view()["ready"]["state"], "ok")

        with mock.patch.object(backend_ready, "probe",
                               lambda url, **kw: {"ok": False, "state": "asr-failed",
                                                  "headline": "模型就绪，但真实自测失败：503"}):
            r = self.client.post("/api/capability/backend/ready")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("真实自测失败", r.json()["detail"])

    def test_the_ready_probe_never_raises_through_the_admin_layer(self):
        with mock.patch.object(backend_ready, "probe", side_effect=RuntimeError("炸了")):
            ok, message = backend_admin.ready_probe()
        self.assertFalse(ok)
        self.assertIn("没跑起来", message)

    def test_stop_reports_the_service_sentence(self):
        with mock.patch.object(backend_proc, "stop",
                               lambda **kw: (True, "已停止 ECHO 起的后端（pid=1）")):
            r = self.client.post("/api/capability/backend/stop")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("已停止", r.json()["message"])

    def test_stop_failure_is_a_400(self):
        with mock.patch.object(backend_proc, "stop",
                               lambda **kw: (False, "停不掉：pid=1 还在跑")):
            r = self.client.post("/api/capability/backend/stop")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("停不掉", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
