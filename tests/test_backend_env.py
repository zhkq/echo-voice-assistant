# -*- coding: utf-8 -*-
"""「起本机后端」的前置探测与计划（`app/backend_env.py`，批 2）。

这一层是**只读**的：它决定了"点按钮之前用户先看到什么"，所以用例盯三件事：

  1. **两条路各自的判据**：没 Docker → 扩展包路（**不是错误**）；Docker 装了但守护进程
     没起来 / 没有 nvidia runtime / 没有 compose → 也要说得出**是哪一条**不满足
     （"没装 Docker"与"装了没起来"的下一步动作正好相反）；
  2. **诚实降级**（实施方案 §4）：没有 N 卡 / 算力不够 / 显存太小 / 磁盘不够 / 权重缺几棵 ——
     每一种都要有"哪一条不满足 + 下一步"，而且 `nvidia-smi` / `docker` 的**原文**要带出来；
  3. **永不抛**：面板每次刷新都问它，探针炸了不能把整块界面带下水。

隔离：所有探针（gpu / docker / runtime / 端口 / 磁盘 / 权重）都在用例里打成桩 ——
真去问这台机器的 docker 与 nvidia-smi 会让结果随机器而变，而"本机恰好有 Docker"不该
让任何一条断言变色。
"""
import os
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backend_env                                  # noqa: E402


def _gpu(ok=True, cap="8.6", vram=8192, err=""):
    if not ok:
        return {"vendor": "", "name": "", "vramMb": 0, "driver": "", "source": "",
                "computeCap": "", "error": err or "PATH 里没有 nvidia-smi"}
    return {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 2060 SUPER", "vramMb": vram,
            "driver": "560.94", "source": "nvidia-smi", "computeCap": cap, "error": ""}


def _docker(installed=True, daemon=True, compose="v2.29.1", gpu_runtime=True, err=""):
    return {"installed": installed, "daemon": daemon, "version": "27.0" if daemon else "",
            "compose": compose if daemon else "", "runtimes": ["runc"] + (["nvidia"] if gpu_runtime else []),
            "gpuRuntime": bool(gpu_runtime), "error": err}


class _Case(unittest.TestCase):
    """把六个探针都换成可控的桩，并把缓存清干净。"""

    def setUp(self):
        backend_env.reset_cache()
        self.addCleanup(backend_env.reset_cache)
        self.gpu = _gpu()
        self.docker = _docker()
        self.runtime = {"ready": True, "path": "/fake/python", "abiOk": True,
                        "torch": "2.14.0+cu126", "torchaudio": "2.11.0+cu126", "error": ""}
        self.disk_free = 100.0
        self.weights = {"root": "/models", "variant": "cu126", "wanted": ["sensevoice"],
                        "present": ["sensevoice"], "missing": [], "ready": True}
        self.ports_ok, self.ports_detail = True, "端口空着：8900、8901"
        for name, value in (("gpu", lambda: dict(self.gpu)),
                            ("docker", lambda: dict(self.docker)),
                            ("runtime", lambda: dict(self.runtime)),
                            ("_free_gb", lambda path: self.disk_free),
                            ("weights", lambda variant="": dict(self.weights)),
                            ("_modelscope_cache", lambda: "/cache/modelscope/hub")):
            p = mock.patch.object(backend_env, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(backend_env.backend_proc, "port_check",
                              lambda *a, **kw: (self.ports_ok, self.ports_detail))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(backend_env.backend_setup, "backend_root",
                              lambda: os.path.join(tempfile.gettempdir(), "echo-be-test"))
        p.start()
        self.addCleanup(p.stop)


class PreflightTests(_Case):
    """对外的判据：能不能走、走哪条、缺什么。"""

    def test_a_machine_without_docker_gets_a_portable_plan_not_an_error(self):
        """**方案 §5 的验收**：没有 Docker 的机器点按钮得到的是扩展包路计划，而不是错误。"""
        self.docker = _docker(installed=False, daemon=False, compose="", gpu_runtime=False,
                             err="PATH 里没有 docker（没装 Docker，或者它不在 PATH）")
        plan = backend_env.plan()
        self.assertEqual(plan["path"], "portable")
        self.assertFalse(plan["implemented"], "一键装好这一步还没做（批 5），不许说能")
        joined = " ".join(plan["notes"])
        self.assertIn("扩展包", joined)
        self.assertIn("批 5", joined, "要说清这一步归哪一批：%s" % joined)
        self.assertIn("docker 原文", " ".join(plan["reasons"]))

    def test_docker_installed_but_not_running_is_told_apart_from_missing(self):
        """装了但没起来 ≠ 没装 —— 两句不同的话，下一步也不同。"""
        self.docker = _docker(daemon=False, compose="", gpu_runtime=False,
                             err="error during connect: 拒绝连接")
        plan = backend_env.plan()
        self.assertEqual(plan["path"], "portable")
        reasons = " ".join(plan["reasons"])
        self.assertIn("守护进程连不上", reasons)
        self.assertNotIn("没装 Docker", reasons)
        self.assertTrue([n for n in plan["notes"] if "Docker Desktop" in n],
                        plan["notes"])

    def test_docker_without_the_nvidia_runtime_asks_for_the_passthrough_check(self):
        self.docker = _docker(gpu_runtime=False)
        plan = backend_env.plan()
        self.assertEqual(plan["path"], "portable")
        self.assertIn("nvidia runtime", " ".join(plan["reasons"]))

    def test_the_container_path_lists_the_verify_step(self):
        plan = backend_env.plan()
        self.assertEqual(plan["path"], "container")
        self.assertTrue(plan["verify"], "容器路必须先验一步 GPU 直通")
        self.assertIn("--gpus all", plan["verify"][0]["command"])

    def test_a_machine_without_an_nvidia_card_is_told_there_is_no_cuda(self):
        self.gpu = _gpu(ok=False, err="'nvidia-smi' 不是内部或外部命令")
        plan = backend_env.plan()
        self.assertEqual(plan["path"], "none")
        self.assertIn("没有可用的 NVIDIA 显卡", plan["whyNot"])
        self.assertIn("nvidia-smi", plan["whyNot"], "要把原文带出来")
        joined = " ".join(plan["notes"])
        self.assertIn("同事", joined, "要给出下一步（用别人的后端）")
        self.assertIn("在线", joined)

    def test_an_old_card_gets_cu118_and_is_told_it_cannot_do_diarization(self):
        self.gpu = _gpu(cap="6.1", vram=8192)          # GTX 1070：Pascal
        plan = backend_env.plan()
        self.assertEqual(plan["variant"], "cu118")
        self.assertEqual(plan["specsHint"], "asr-only")
        self.assertIn("做不了说话人分离", " ".join(plan["reasons"]))

    def test_a_small_card_gets_the_co_residency_warning(self):
        self.gpu = _gpu(cap="8.6", vram=6144)          # 6 GB
        plan = backend_env.plan()
        self.assertEqual(plan["specsHint"], "asr-only")
        joined = " ".join(plan["notes"])
        self.assertIn("同时常驻会顶爆", joined)
        self.assertIn("4.6", joined)                   # qwen3asr+对齐器的峰值

    def test_an_unknown_compute_cap_defaults_to_the_new_variant_and_says_so(self):
        self.gpu = _gpu(cap="")
        plan = backend_env.plan()
        self.assertEqual(plan["variant"], "cu126")
        self.assertIn("算力判不了", " ".join(plan["reasons"]))

    def test_disk_and_weights_are_reported_with_the_place_we_looked(self):
        self.disk_free = 1.0
        self.weights = {"root": "C:/models", "variant": "cu126",
                        "wanted": ["sensevoice", "pyannote"],
                        "present": ["sensevoice"], "missing": ["pyannote"], "ready": False}
        plan = backend_env.plan()
        self.assertTrue([m for m in plan["missing"] if "磁盘" in m], plan["missing"])
        self.assertTrue([m for m in plan["missing"] if "权重" in m], plan["missing"])
        joined = " ".join(plan["notes"])
        self.assertIn("客户端模型库", joined)
        self.assertIn("ModelScope", joined, "要说清另一处我们没算：%s" % joined)

    def test_occupied_ports_are_in_the_plan(self):
        self.ports_ok = False
        self.ports_detail = "端口被占：8900 被 python.exe（pid 1234）占着"
        plan = backend_env.plan()
        self.assertTrue([m for m in plan["missing"] if "8900" in m], plan["missing"])

    def test_the_plan_is_json_renderable(self):
        import json
        json.dumps(backend_env.plan(), ensure_ascii=False)


class ProbeTests(unittest.TestCase):
    """探针本身的行为：原文、缓存、永不抛。

    **刻意不继承 `_Case`**：那一套会把 `gpu` / `docker` / `runtime` / `_free_gb` / `weights`
    全打上桩，而这一组要验的正是**这几个真函数**自己（原文有没有带出来、标签怎么比）。
    只把两样东西固定住：端口探针（跑 netstat，慢且随机器变）与后端的家（临时目录）。
    """

    def setUp(self):
        backend_env.reset_cache()
        self.addCleanup(backend_env.reset_cache)
        self.tmp = tempfile.mkdtemp(prefix="echo-be-probe-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        for target, value in ((backend_env.backend_setup, "backend_root"),
                              (backend_env.backend_proc, "port_check")):
            p = mock.patch.object(target, value,
                                  (lambda: self.tmp) if value == "backend_root"
                                  else (lambda *a, **kw: (True, "端口空着")))
            p.start()
            self.addCleanup(p.stop)

    def test_docker_error_text_is_carried_verbatim(self):
        def _fake_run(argv, timeout=8.0):
            return {"ok": False, "code": 1, "stdout": "",
                    "stderr": "error during connect: this error may indicate that the docker "
                              "daemon is not running",
                    "error": ""}

        with mock.patch.object(backend_env.shutil, "which", lambda name: "/fake/docker"), \
                mock.patch.object(backend_env, "_run", _fake_run):
            info = backend_env.docker()
        self.assertTrue(info["installed"])
        self.assertFalse(info["daemon"])
        self.assertIn("daemon is not running", info["error"])

    def test_docker_runtimes_reveal_the_nvidia_runtime(self):
        def _fake_run(argv, timeout=8.0):
            if argv[1] == "version":
                return {"ok": True, "code": 0, "stdout": "27.0\n", "stderr": "", "error": ""}
            if argv[1] == "compose":
                return {"ok": True, "code": 0, "stdout": "v2.29.1\n", "stderr": "", "error": ""}
            return {"ok": True, "code": 0,
                    "stdout": '{"runc":{"path":"runc"},"nvidia":{"path":"nvidia-container-runtime"}}',
                    "stderr": "", "error": ""}

        with mock.patch.object(backend_env.shutil, "which", lambda name: "/fake/docker"), \
                mock.patch.object(backend_env, "_run", _fake_run):
            info = backend_env.docker()
        self.assertTrue(info["gpuRuntime"])
        self.assertEqual(info["runtimes"], ["nvidia", "runc"])

    def test_the_runtime_abi_check_compares_cuda_tags_not_versions(self):
        """判据是**CUDA 源标签一致**（都带 `+cu126`），不是版本号相等（AGENTS.md 记过）。"""
        def _fake_run(argv, timeout=8.0):
            return {"ok": False, "code": 3, "stdout": "2.14.0+cu126\n2.11.0+cpu\n",
                    "stderr": "", "error": ""}

        with mock.patch.object(backend_env.backend_proc, "python_exe",
                               lambda: "/fake/python"), \
                mock.patch.object(backend_env, "_run", _fake_run):
            info = backend_env.runtime()
        self.assertIs(info["abiOk"], False)
        self.assertIn("不是同一个 CUDA 源", info["error"])
        self.assertIn("torch 2.14.0+cu126", info["error"])

    def test_the_runtime_is_reported_missing_without_a_python(self):
        with mock.patch.object(backend_env.backend_proc, "python_exe", lambda: ""):
            info = backend_env.runtime()
        self.assertFalse(info["ready"])
        self.assertIn("还没装运行时", info["error"])

    def test_weights_list_what_is_missing_in_the_client_model_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sensevoice"))
            open(os.path.join(tmp, "sensevoice", "model.bin"), "wb").close()
            with mock.patch.object(backend_env.paths, "models_root", lambda: tmp):
                got = backend_env.weights("cu126")
        self.assertIn("sensevoice", got["present"])
        self.assertIn("hub/models--Qwen--Qwen3-ASR-0.6B", got["missing"])
        self.assertFalse(got["ready"])

    def test_probe_is_cached_and_force_refreshes(self):
        fake = {"vendor": "nvidia", "name": "x", "vramMb": 1, "driver": "", "source": "",
                "computeCap": "8.6", "error": ""}
        with mock.patch.object(backend_env, "gpu", lambda: dict(fake)):
            self.assertEqual(backend_env.probe(force=True)["gpu"]["vendor"], "nvidia")
            fake["vendor"] = ""
            self.assertEqual(backend_env.probe()["gpu"]["vendor"], "nvidia",
                             "10 秒内应该吃缓存（不然面板每次刷新都去问 nvidia-smi）")
            self.assertEqual(backend_env.probe(force=True)["gpu"]["vendor"], "")

    def test_probe_never_raises_when_everything_blows_up(self):
        """探针全炸也不能把面板带下水：每个格子退回"未知"，并写明是哪一条炸的。"""
        with mock.patch.object(backend_env, "gpu", side_effect=RuntimeError("gpu 炸了")), \
                mock.patch.object(backend_env, "docker", side_effect=RuntimeError("docker 炸了")), \
                mock.patch.object(backend_env, "runtime", side_effect=RuntimeError("runtime 炸了")), \
                mock.patch.object(backend_env.backend_proc, "port_check",
                                  side_effect=RuntimeError("netstat 炸了")), \
                mock.patch.object(backend_env, "_free_gb",
                                  side_effect=RuntimeError("磁盘炸了")):
            got = backend_env.probe(force=True)
        self.assertEqual(got["gpu"]["vendor"], "", "显卡探针炸了要退回'没有显卡'")
        self.assertIn("炸了", got["gpu"]["error"])
        self.assertFalse(got["docker"]["installed"])
        self.assertFalse(got["runtime"]["ready"])
        self.assertIsNone(got["diskFreeGB"])
        self.assertFalse(got["portsOk"])

    def test_free_gb_walks_up_to_an_existing_ancestor(self):
        """路径还不存在时也要给得出数字（后端的家往往还没建）。"""
        with tempfile.TemporaryDirectory() as tmp:
            deep = os.path.join(tmp, "a", "b", "c")
            self.assertIsInstance(backend_env._free_gb(deep), float)
        self.assertIsNone(backend_env._free_gb("\x00bad\x00path"))


if __name__ == "__main__":
    unittest.main()
