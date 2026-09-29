# -*- coding: utf-8 -*-
"""容器路：compose 的渲染与落盘（`app/backend_docker.py`，批 4）。

这一层最容易出的错**不是崩**，是"看着只绑了回环、其实对网段开着"——
所以用例的核心是那份渲染出来的 compose **逐条**对齐这两件事：

  * **宿主侧**：两个端口都只发布到 `127.0.0.1`（网段里连不上 = "只服务本机"的全部依据）；
  * **容器里**：`ECHO_LISTEN` / `ECHO_ADMIN_LISTEN` 必须是**通配**地址 ——
    发布是转发到**容器 IP** 的，容器内听回环会让发布出去的那个端口连不上。

（第二件与实施方案 §0 那句"只绑回环要改两处：`listen: 127.0.0.1:8900` 和端口发布"**不同**：
那句的第一处只适用于"后端以本机进程形态跑"（扩展包路）。容器里照抄会焊死端口。
两处都很像"只绑回环"，但一个是网络命名空间的事、一个是发布规则的事 —— 用例把这条差异钉住。）

**没有 Docker 的机器上跑不了 `docker compose config`**：本机就没有 docker，所以这里只验
"渲染出来的 YAML 是合法 YAML + 逐条断言关键字段"，并在 `verify_notes()` 里给出人必须手动做的
那一步（本机 `compose config` + **另一台机器** `curl` 必须连不上）。这一步是**未验证项**，
交付里如实写着。
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backend_docker, backend_proc, backend_setup      # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-docker-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for mod in (backend_setup,):
            p = mock.patch.object(mod, "backend_root", lambda: self.root)
            p.start()
            self.addCleanup(p.stop)

    @property
    def root(self):
        return os.path.join(self.tmp, "backend")


class ComposeRenderTests(_Case):
    def _cfg(self, **kw):
        return yaml.safe_load(backend_docker.render_compose(**kw))

    def test_loopback_ports_are_paired(self):
        """**方案 §5 的验收**：宿主侧两个端口都只发布到回环 ——
        另一台机器 `curl http://<宿主IP>:8900/v1/health` 必须连不上。"""
        svc = self._cfg()["services"]["echo-backend"]
        ports = svc["ports"]
        self.assertEqual(len(ports), 2, ports)
        for entry in ports:
            self.assertTrue(entry.startswith("127.0.0.1:"),
                            "端口 %r 没钉在宿主回环上 —— 这就是「只绑了回环」的假安全" % entry)
        self.assertIn("127.0.0.1:8900:8900", ports)
        self.assertIn("127.0.0.1:8901:8901", ports)

    def test_the_inside_listen_must_stay_wildcard(self):
        """容器**里**必须绑通配：发布是 DNAT 到容器 IP 的，听回环 = 发布出去也没人应答。

        （与实施方案 §0 那句的第一处不同 —— 那句适用于"本机进程形态"，容器里照抄会焊死。
        这条用例就是那条差异的记录：改这里之前先读 `app/backend_docker.py` 的文件头。）
        """
        env = self._cfg()["services"]["echo-backend"]["environment"]
        self.assertEqual(env["ECHO_LISTEN"], "0.0.0.0:8900")
        self.assertEqual(env["ECHO_ADMIN_LISTEN"], "0.0.0.0:8901")
        for key in ("ECHO_LISTEN", "ECHO_ADMIN_LISTEN"):
            self.assertFalse(str(env[key]).startswith(("127.", "localhost", "[::1]")),
                             "%s 绑了回环 —— 发布出去的那个端口会连不上" % key)

    def test_custom_ports_are_carried_into_both_places(self):
        svc = self._cfg(port=8902, admin_port=8903)["services"]["echo-backend"]
        self.assertIn("127.0.0.1:8902:8900", svc["ports"])
        self.assertIn("127.0.0.1:8903:8901", svc["ports"])

    def test_models_are_mounted_read_only_and_state_is_not(self):
        """权重只读挂；`state`（auth.db + local-pair.json）是耐久数据，**别动它**。"""
        vols = self._cfg(models_dir="/srv/models", state_dir="/srv/state",
                         tmp_dir="/srv/tmp", cache_dir="/srv/cache")["services"]["echo-backend"]["volumes"]
        joined = " ".join(vols)
        self.assertIn("/srv/models:/opt/echo/models:ro", joined)
        self.assertIn("/srv/state:/var/echo/state", joined)
        self.assertIn("/srv/tmp:/var/echo/tmp", joined)
        self.assertIn("/srv/cache:/var/echo/cache", joined)
        self.assertNotIn("/srv/state:/var/echo/state:ro", joined)

    def test_the_cache_and_home_are_writable(self):
        """根文件系统只读，而 funasr 会现拉 vad 模型 —— cache/home 必须落在可写卷里。"""
        env = self._cfg()["services"]["echo-backend"]["environment"]
        self.assertTrue(env["MODELSCOPE_CACHE"].startswith("/var/echo/cache"))
        self.assertEqual(env["HOME"], "/var/echo/cache")
        self.assertIs(self._cfg()["services"]["echo-backend"]["read_only"], True)

    def test_auth_three_lines_are_written_together(self):
        """鉴权三件套要么一起、要么都不写（只写一半：health 照常 200，而 /v1/token 回 503）。"""
        env_off = self._cfg()["services"]["echo-backend"]["environment"]
        self.assertNotIn("ECHO_AUTH_ENABLED", env_off)
        env_on = self._cfg(jwt_secret="deadbeef")["services"]["echo-backend"]["environment"]
        self.assertEqual(env_on["ECHO_AUTH_ENABLED"], "true")
        self.assertEqual(env_on["ECHO_AUTH_MODE"], "jwt")
        self.assertEqual(env_on["ECHO_JWT_SECRET"], "deadbeef")

    def test_local_pair_is_on(self):
        """本机形态就该走本机配对文件（同机不该让人抄配对码）。"""
        env = self._cfg()["services"]["echo-backend"]["environment"]
        self.assertEqual(env["ECHO_LOCAL_PAIR"], "true")
        self.assertEqual(env["ECHO_STATE_ROOT"], "/var/echo/state")

    def test_windows_paths_survive_the_yaml_round_trip(self):
        """Windows 路径里全是反斜杠：渲染出来必须仍是**合法 YAML**且路径一字不差。"""
        cfg = self._cfg(models_dir=r"C:\models dir\hub", state_dir=r"D:\echo\backend\state")
        vols = " ".join(cfg["services"]["echo-backend"]["volumes"])
        self.assertIn(r"C:\models dir\hub:/opt/echo/models:ro", vols)
        self.assertIn(r"D:\echo\backend\state:/var/echo/state", vols)


class ComposeWriteTests(_Case):
    def test_writing_is_idempotent_and_backs_up_a_hand_edit(self):
        first = backend_docker.write_compose()
        self.assertTrue(first["ok"], first)
        self.assertTrue(os.path.isfile(backend_docker.compose_path()))
        again = backend_docker.write_compose()
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["backup"], "", "内容没变就不该产生备份")

        with open(backend_docker.compose_path(), "w", encoding="utf-8") as fh:
            fh.write("# 手工改过\n")
        third = backend_docker.write_compose()
        self.assertTrue(third["ok"], third)
        self.assertTrue(third["backup"], third)
        with open(third["backup"], "r", encoding="utf-8") as fh:
            self.assertIn("手工改过", fh.read())

    def test_commands_point_at_the_written_file(self):
        cmds = backend_docker.commands()
        labels = [c["label"] for c in cmds]
        self.assertEqual(labels, ["起", "看日志", "停"])
        for c in cmds:
            self.assertIn(backend_docker.compose_path(), c["command"])
        self.assertIn("up -d", cmds[0]["command"])

    def test_verify_notes_tell_you_to_check_from_another_machine(self):
        """**"只绑回环"只有从另一台机器上才测得出来** —— 这几句是给人照着做的验收。"""
        notes = " ".join(backend_docker.verify_notes(port=8900, admin_port=8901))
        self.assertIn("compose", notes, "本机先要 compose config 过一遍")
        self.assertIn("另一台机器", notes)
        self.assertIn("必须**连不上**", notes)
        self.assertIn("8900", notes)
        self.assertIn("8901", notes)


class AvailabilityTests(_Case):
    def test_available_reports_the_docker_error_verbatim(self):
        fake = {"installed": True, "daemon": False, "version": "",
                "compose": "", "runtimes": [], "gpuRuntime": False,
                "error": "error during connect: daemon is not running"}
        with mock.patch("app.backend_env.probe", lambda force=False: {"docker": fake}):
            got = backend_docker.available()
        self.assertFalse(got["ok"])
        self.assertIn("daemon is not running", got["detail"])

    def test_available_is_true_when_docker_is_up(self):
        fake = {"installed": True, "daemon": True, "version": "27.0",
                "compose": "v2.29.1", "runtimes": ["runc", "nvidia"], "gpuRuntime": True,
                "error": ""}
        with mock.patch("app.backend_env.probe", lambda force=False: {"docker": fake}):
            got = backend_docker.available()
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["compose"], "v2.29.1")

    def test_available_never_raises(self):
        with mock.patch("app.backend_env.probe", side_effect=RuntimeError("炸了")):
            got = backend_docker.available()
        self.assertFalse(got["ok"])
        self.assertIn("炸了", got["detail"])


if __name__ == "__main__":
    unittest.main()
