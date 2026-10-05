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
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backend_env, backend_fetch                      # noqa: E402


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
        self.runtime = {"ready": True, "depsOk": True, "usable": True, "path": "/fake/python",
                        "abiOk": True,
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
        # 「取薄包」那一步也要隔离：它的搜索目录里含着**真实的** Downloads/Desktop/Documents，
        # 开发机上恰好放着一个 `*.zip` 就会让 `implemented` 随人而变（假阳性/假阴性都算）。
        from app import backend_fetch
        self.backend_fetch = backend_fetch
        for name, value in (("search_dirs", lambda: []), ("package_source", lambda: ""),
                            ("find_package_zip", lambda: "")):
            p = mock.patch.object(backend_fetch, name, value)
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
        self.assertTrue(plan["implemented"],
                        "取薄包 + 取运行时都在一键路里了（2026-09-30）—— 这一档现在能一键做完")
        self.assertEqual(plan["whyNot"], "")
        self.assertIn("国内源", plan["fetch"]["headline"],
                      "要写清运行时会从哪来：%s" % plan["fetch"]["headline"])
        self.assertIn("docker 原文", " ".join(plan["reasons"]))

    def _force_portable(self):
        """把 Docker 这一路关掉 —— 只有"没有容器路"时计划才会走到扩展包（portable）那一档。"""
        self.docker = _docker(installed=False, daemon=False, compose="", gpu_runtime=False,
                              err="PATH 里没有 docker（没装 Docker，或者它不在 PATH）")

    def test_portable_is_not_implemented_until_the_thin_package_can_be_reached(self):
        """**薄包既不在本机、也没给地址** → 如实说不能一键，并写清"放哪/填哪"。

        这一条是 `implemented` 的另一半：上一版（§8.16）它恒为 False，因为"取薄包"要人工做；
        现在它取决于**薄包能不能到手**（本机找得到 / 设置给了路径或 URL）—— 所以这两面都要钉。
        """
        self._force_portable()
        self.runtime = {"ready": False, "depsOk": None, "usable": False, "path": "",
                        "abiOk": None, "torch": "",
                        "torchaudio": "", "error": ""}
        plan = backend_env.plan()
        self.assertEqual(plan["path"], "portable")
        self.assertFalse(plan["implemented"])
        self.assertIn("薄包", plan["whyNot"])
        self.assertTrue(any("薄包" in m for m in plan["missing"]), plan["missing"])
        self.assertIn("capabilityBackendPackage", " ".join(plan["notes"]))
        self.assertFalse(plan["fetch"]["willFetch"])

    def test_a_package_on_this_machine_makes_it_a_one_click_path(self):
        """本机找得到薄包（或用户填了地址）→ `implemented` 为真，且说得出**在哪找到的**。"""
        self._force_portable()
        self.runtime = {"ready": False, "depsOk": None, "usable": False, "path": "",
                        "abiOk": None, "torch": "",
                        "torchaudio": "", "error": ""}
        with mock.patch.object(self.backend_fetch, "find_package_zip",
                               lambda: r"D:\交付\ECHO-backend-portable-20260930-1309.zip"):
            plan = backend_env.plan()
        self.assertTrue(plan["implemented"], plan["whyNot"])
        self.assertEqual(plan["whyNot"], "")
        self.assertEqual(plan["fetch"]["packageSource"],
                         r"D:\交付\ECHO-backend-portable-20260930-1309.zip")
        joined = " ".join(plan["notes"])
        self.assertIn("薄包", joined)
        self.assertIn("国内源", joined, "运行时会从哪来也要写清：%s" % joined)

    def test_a_runtime_with_a_bad_abi_is_not_called_one_click(self):
        """运行时在、但 torch/torchaudio 的 CUDA 标签不一致 → 要清掉重装，**一键做不了**。"""
        self._force_portable()
        self.runtime = {"ready": True, "depsOk": True, "usable": False, "path": "/fake/python",
                        "abiOk": False,
                        "torch": "2.10.0+cu128", "torchaudio": "2.10.0+cpu",
                        "error": "标签不一致"}
        plan = backend_env.plan()
        self.assertFalse(plan["implemented"])
        self.assertIn("ABI", plan["whyNot"])

    def test_a_half_installed_runtime_is_not_called_ready_but_is_still_one_click(self):
        """**2026-10-01 真机那个 bug 的面板面**：解释器在、依赖没装全、薄包在手。

        旧判据（`rt["ready"] = bool(python_exe())`）在这里会说「运行时已在」；
        现在要**如实说"只装了一半"**，且**不能**报成"ABI 不符"（那会把人引去清 `runtime/`
        重装，而真相是依赖还没装）。同时它**仍然是一键**：点下去 `ensure_runtime` 会把依赖补齐。
        """
        self._force_portable()
        self.runtime = {"ready": True, "depsOk": False, "usable": False,
                        "path": r"D:\ECHO\backend\runtime\python.exe", "abiOk": None,
                        "torch": "", "torchaudio": "",
                        "error": "ModuleNotFoundError: No module named 'fastapi'"}
        # 真机上薄包就在手里（已被解到 `{backend}/`）—— 装依赖要用它的 requirements.txt
        with mock.patch.object(self.backend_fetch, "find_package_zip",
                               lambda: r"D:\交付\ECHO-backend-portable-20260930-1309.zip"):
            plan = backend_env.plan()
        joined = " ".join(plan["notes"])
        self.assertIn("依赖没装全", joined)
        self.assertIn("fastapi", joined, "要把 import 失败的原文带出来：%s" % joined)
        self.assertNotIn("ABI", joined, "依赖没装全时**不许**报成 ABI 不符：%s" % joined)
        self.assertNotIn("运行时已装但 ABI 不符", plan["whyNot"])
        self.assertTrue([m for m in plan["missing"] if "依赖" in m], plan["missing"])
        self.assertTrue(plan["implemented"], plan["whyNot"])
        self.assertEqual(plan["whyNot"], "")

    def test_a_half_installed_runtime_without_a_pack_says_the_pack_is_the_blocker(self):
        """解释器在、依赖没装全、**薄包也拿不到** → 一键做不了，而卡点**是薄包**。

        为什么这一档不能算"一键"：装依赖要用薄包里的 `server/requirements.txt`（装什么靠它）。
        说成"点一下就好"会让人点完得到一个"薄包还没解开"的失败 —— 那一句该在点之前就说。
        """
        self._force_portable()
        self.runtime = {"ready": True, "depsOk": False, "usable": False,
                        "path": r"D:\ECHO\backend\runtime\python.exe", "abiOk": None,
                        "torch": "", "torchaudio": "",
                        "error": "ModuleNotFoundError: No module named 'fastapi'"}
        plan = backend_env.plan()
        self.assertFalse(plan["implemented"])
        self.assertIn("薄包", plan["whyNot"])
        self.assertIn("依赖没装全", " ".join(plan["notes"]),
                      "卡点虽是薄包，也要把「运行时装了一半」这件事说清楚")

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

    def test_blackwell_gets_cu128(self):
        """**RTX 50 系（Blackwell，sm_120）要 cu128**（2026-10-01 在 RTX 5060 Laptop 上定）。

        旧判据只有"老卡 → cu118 / 其余 → cu126"，于是这台 5060（`compute_cap 12.0`）算成
        cu126 —— 而 cu126 那套轮子里**没有 sm_120 的 kernel**：症状是"装得上、起得来、
        一跑模型就 CUDA 报错"，最难查的一类。dev 这台能跑的正是 `torch 2.10.0+cu128`。
        """
        self.gpu = _gpu(cap="12.0")
        plan = backend_env.plan()
        self.assertEqual(plan["variant"], "cu128")
        joined = " ".join(plan["reasons"])
        self.assertIn("cu128", joined)
        self.assertIn("sm_120", joined, "理由要说清「为什么」（换个人看得懂）：%s" % joined)

    def test_hopper_still_gets_cu126(self):
        """门槛定在 12.0：Hopper（9.0）在 cu126 上没问题，别一起推去 cu128。"""
        self.assertEqual(backend_env.variant_for(_gpu(cap="9.0"))[0], "cu126")
        self.assertEqual(backend_env.variant_for(_gpu(cap="8.6"))[0], "cu126")

    def test_cu128_looks_for_the_same_weights_as_cu126(self):
        """cu128 也要**登记权重子树**：没登记时 `weights("cu128")` 的 `wanted` 是空表，
        面板会显示"模型 0/0 就绪"（看着像齐了），而实际上是这一档根本没在查。"""
        self.assertEqual([lbl for lbl, _c in backend_env.VARIANT_MODELS["cu128"]],
                         [lbl for lbl, _c in backend_env.VARIANT_MODELS["cu126"]])
        self.assertIn("cu128", backend_env.VARIANT_MODELS)
        self.assertIn("cu128", backend_fetch.VARIANT_EXTRAS,
                      "取运行时那一侧也要认这一档（否则装不了 torch）")

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
        """**两个 CUDA 版**之间：标签必须一致（cu13x 的 torchaudio 配 cu126 的 torch 会崩）。

        注意这里桩的是 `ok=True`（import 通过了）—— 因为"标签不一致"现在只有在 import
        过得去的时候才谈得上比较；import 就崩的那种另有一条用例钉。
        """
        fake_python = os.path.join(self.tmp, "fake-python")
        with open(fake_python, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")             # 只要"这个文件在"（ABI 校验前的存在性检查）

        def _fake_run(argv, timeout=8.0):
            return {"ok": True, "code": 0, "stdout": "2.14.0+cu126\n2.11.0+cu13x\n",
                    "stderr": "", "error": ""}

        with mock.patch.object(backend_env, "_run", _fake_run):
            got = backend_env.check_torch_abi(fake_python)
        self.assertFalse(got["ok"])
        self.assertIn("不是同一个 CUDA 源", got["error"])
        self.assertIn("torch 2.14.0+cu126", got["error"])

    def test_a_cpu_torchaudio_is_accepted_with_a_note(self):
        """**纯 CPU 版 torchaudio 不算错**（2026-09-30 真机证据）。

        dev 这台机器就是 `torch 2.10.0+cu128` + `torchaudio 2.10.0+cpu`，而它
        **qwen3asr 与 SenseVoice 都加载成功、真转写也成功** —— 第一条判据"标签必须一致"
        把一个能用的环境挡住了（假阳性）。现在只要求 import 通过，并把"它是 CPU 版"记成 note。
        """
        fake_python = os.path.join(self.tmp, "fake-python")
        with open(fake_python, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")

        def _fake_run(argv, timeout=8.0):
            return {"ok": True, "code": 0, "stdout": "2.10.0+cu128\n2.10.0+cpu\n",
                    "stderr": "", "error": ""}

        with mock.patch.object(backend_env, "_run", _fake_run):
            got = backend_env.check_torch_abi(fake_python)
        self.assertTrue(got["ok"], got)
        self.assertIn("CPU 版", got["note"])

    def test_an_import_crash_is_still_a_hard_failure(self):
        """AGENTS.md 记的那个坑（cu13x torchaudio 要 `libcudart.so.13`）表现就是 **import 崩** ——
        这一条必须仍然是硬失败，否则那道闸门等于没了。"""
        fake_python = os.path.join(self.tmp, "fake-python")
        with open(fake_python, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")

        def _fake_run(argv, timeout=8.0):
            return {"ok": False, "code": 1, "stdout": "",
                    "stderr": "OSError: Could not load this library: "
                              ".../torchaudio/lib/_torchaudio.abi3.so",
                    "error": ""}

        with mock.patch.object(backend_env, "_run", _fake_run):
            got = backend_env.check_torch_abi(fake_python)
        self.assertFalse(got["ok"])
        self.assertIn("_torchaudio.abi3.so", got["error"])

    def test_the_abi_check_rejects_a_missing_interpreter(self):
        got = backend_env.check_torch_abi(os.path.join(self.tmp, "nope"))
        self.assertFalse(got["ok"])
        self.assertIn("没有解释器", got["error"])

    def test_the_runtime_is_reported_missing_without_a_python(self):
        with mock.patch.object(backend_env.backend_proc, "python_exe", lambda: ""):
            info = backend_env.runtime()
        self.assertFalse(info["ready"])
        self.assertIn("还没装运行时", info["error"])

    # ---------------------------------------------------------------- 「能不能用」快探
    # 判据的由来（2026-10-01 真机）：薄包**只带解释器**，fastapi/uvicorn 要靠"取运行时"装。
    # 旧判据"`runtime/` 里有没有 python.exe"会记「运行时已在」→ 跳过装依赖 → 后端一起来就
    # `ModuleNotFoundError: No module named 'fastapi'` 退出。**"有解释器"不等于"能用"。**

    def test_the_deps_probe_asks_for_the_right_imports(self):
        seen = {}

        def _fake_run(argv, timeout=8.0):
            seen["argv"] = argv
            return {"ok": True, "code": 0, "stdout": "", "stderr": "", "error": ""}

        fake = os.path.join(self.tmp, "fake-python")
        open(fake, "w", encoding="utf-8").close()
        with mock.patch.object(backend_env, "_run", _fake_run):
            got = backend_env.check_server_deps(fake)
        self.assertTrue(got["ok"], got)
        self.assertEqual(seen["argv"][:2], [fake, "-c"])
        for name in backend_env.SERVER_IMPORTS:
            self.assertIn(name, seen["argv"][2])
        self.assertIn("fastapi", backend_env.SERVER_IMPORTS,
                      "这一条就是那个 bug 的判据：fastapi 必须在探的清单里")

    def test_the_deps_probe_carries_the_import_error_verbatim(self):
        """探不过时要把**原文**带出来（"说错原因会把人引去查错东西"）。"""
        def _fake_run(argv, timeout=8.0):
            return {"ok": False, "code": 1, "stdout": "",
                    "stderr": "ModuleNotFoundError: No module named 'fastapi'", "error": ""}

        fake = os.path.join(self.tmp, "fake-python")
        open(fake, "w", encoding="utf-8").close()
        with mock.patch.object(backend_env, "_run", _fake_run):
            got = backend_env.check_server_deps(fake)
        self.assertFalse(got["ok"])
        self.assertIn("No module named 'fastapi'", got["error"])

    def test_the_deps_probe_rejects_a_missing_interpreter(self):
        got = backend_env.check_server_deps(os.path.join(self.tmp, "nope"))
        self.assertFalse(got["ok"])
        self.assertIn("没有解释器", got["error"])

    def test_a_half_installed_runtime_never_reaches_the_abi_check(self):
        """**依赖没装全时不许去跑 ABI 校验**：`import torch` 也会失败，而那条会报
        "ABI 不符" —— 真相是"依赖还没装"。说错原因会把人引去清 `runtime/` 重装。"""
        fake = os.path.join(self.tmp, "fake-python")
        open(fake, "w", encoding="utf-8").close()

        def _must_not_run(*a, **kw):                                  # pragma: no cover
            raise AssertionError("依赖没装全时不该跑 ABI 校验")

        def _fake_run(argv, timeout=8.0):
            return {"ok": False, "code": 1, "stdout": "",
                    "stderr": "ModuleNotFoundError: No module named 'fastapi'", "error": ""}

        with mock.patch.object(backend_env.backend_proc, "python_exe", lambda: fake), \
                mock.patch.object(backend_env, "_run", _fake_run), \
                mock.patch.object(backend_env, "check_torch_abi", _must_not_run):
            info = backend_env.runtime()
        self.assertTrue(info["ready"], "解释器在 —— 这一栏照旧")
        self.assertFalse(info["depsOk"])
        self.assertFalse(info["usable"], "**能用**才是就绪的判据")
        self.assertIsNone(info["abiOk"], "没跑到那一步就不该给个 ABI 结论")
        self.assertIn("No module named 'fastapi'", info["error"])

    def test_a_runtime_is_usable_only_when_the_deps_and_the_abi_both_pass(self):
        fake = os.path.join(self.tmp, "fake-python")
        open(fake, "w", encoding="utf-8").close()
        abi = {"ok": True, "torch": "2.14.0+cu126", "torchaudio": "2.11.0+cu126",
               "error": "", "note": ""}
        with mock.patch.object(backend_env.backend_proc, "python_exe", lambda: fake), \
                mock.patch.object(backend_env, "_run",
                                  lambda argv, timeout=8.0: {"ok": True, "code": 0,
                                                             "stdout": "", "stderr": "",
                                                             "error": ""}), \
                mock.patch.object(backend_env, "check_torch_abi",
                                  lambda exe, timeout=180.0: abi):
            info = backend_env.runtime()
        self.assertTrue(info["depsOk"])
        self.assertTrue(info["abiOk"])
        self.assertTrue(info["usable"])

    def test_weights_list_what_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sensevoice"))
            open(os.path.join(tmp, "sensevoice", "model.bin"), "wb").close()
            with mock.patch.object(backend_env.paths, "models_root", lambda: tmp), \
                    mock.patch.dict(os.environ, {"MODELSCOPE_CACHE":
                                                 os.path.join(tmp, "no-such-cache")}):
                got = backend_env.weights("cu126")
        self.assertIn("sensevoice", got["present"])
        self.assertIn("qwen3asr", got["missing"])
        self.assertFalse(got["ready"])

    def test_weights_also_look_in_the_modelscope_cache(self):
        """**误报的修正**（2026-09-30 真机打脸）：只查客户端模型库会报"缺"，而它就在
        ModelScope 缓存里（服务端日志证明加载成功）。两处都查，并说清在哪找到的。"""
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "modelscope")
            os.makedirs(os.path.join(cache, "models", "Qwen--Qwen3-ASR-0.6B", "snapshots"))
            open(os.path.join(cache, "models", "Qwen--Qwen3-ASR-0.6B", "snapshots", "m.bin"),
                 "wb").close()
            with mock.patch.object(backend_env.paths, "models_root",
                                   lambda: os.path.join(tmp, "empty-models")), \
                    mock.patch.dict(os.environ, {"MODELSCOPE_CACHE": cache}):
                got = backend_env.weights("cu126")
        self.assertIn("qwen3asr", got["present"], got)
        self.assertIn("Qwen--Qwen3-ASR-0.6B", got["foundAt"]["qwen3asr"])
        self.assertIn("ModelScope", got["foundAt"]["qwen3asr"])
        self.assertTrue([r for r in got["roots"] if "ModelScope" in r["label"]], got["roots"])

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


class PackedTorchTests(unittest.TestCase):
    """**运行时里带了 torch，就必须 import 得动**（2026-10-01 真机事故的护栏）。

    现场：离线包被打包过滤剪掉了 `torch/utils/data`（还连带 `transformers/models`、
    `funasr/models`），而"运行时就绪"只看 fastapi/uvicorn → 残包被判就绪 →
    **永远不会重新解包** → 后端报"模型要求 GPU，但 torch 看不到 CUDA"（把打包问题说成显卡问题）。
    这一组钉住两半：① 探针本身认得出"带了却 import 不动"；② `ensure_runtime` 不再提前返回。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-packed-torch-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.exe = os.path.join(self.tmp, "python.exe")
        with open(self.exe, "wb") as fh:
            fh.write(b"x")

    def _run_returns(self, *, has_torch, second_ok, stderr=""):
        calls = []

        def _fake(argv, timeout=None):
            calls.append(argv)
            if len(calls) == 1:
                return {"ok": True, "stdout": "True\n" if has_torch else "False\n", "stderr": ""}
            return {"ok": second_ok, "stdout": "", "stderr": stderr}

        return calls, _fake

    def test_a_thin_runtime_without_torch_is_not_called_broken(self):
        """薄包只有解释器：`site-packages/torch` 不在 → 跳过（不能把薄包判成坏包）。"""
        _calls, fake = self._run_returns(has_torch=False, second_ok=False)
        with mock.patch.object(backend_env, "_run", fake):
            got = backend_env.check_packed_torch(self.exe)
        self.assertTrue(got["ok"])
        self.assertTrue(got["skipped"])

    def test_a_runtime_whose_torch_cannot_import_is_reported_with_the_real_error(self):
        err = ("ImportError: cannot import name 'data' from partially initialized module "
               "'torch.utils' (most likely due to a circular import)")
        calls, fake = self._run_returns(has_torch=True, second_ok=False, stderr=err)
        with mock.patch.object(backend_env, "_run", fake):
            got = backend_env.check_packed_torch(self.exe)
        self.assertFalse(got["ok"])
        self.assertIn("torch.utils", got["error"])
        self.assertFalse(got["skipped"])
        self.assertIn("torch.utils.data", calls[1][2], "第二次探针必须把子包也 import 上")

    def test_a_healthy_torch_passes(self):
        _calls, fake = self._run_returns(has_torch=True, second_ok=True)
        with mock.patch.object(backend_env, "_run", fake):
            got = backend_env.check_packed_torch(self.exe)
        self.assertTrue(got["ok"])
        self.assertFalse(got["skipped"])

    def test_ensure_runtime_does_not_stop_at_a_broken_torch(self):
        """核心那半句：torch 坏了 → **不许**提前返回"运行时已在"，要往下走去重解包。"""
        from app import backend_fetch
        calls = []

        def _steps(msg):
            calls.append(msg)

        with mock.patch.object(backend_env.backend_proc, "python_exe", lambda: self.exe), \
                mock.patch.object(backend_env, "check_server_deps",
                                  lambda *_a, **_k: {"ok": True, "error": ""}), \
                mock.patch.object(backend_env, "check_packed_torch",
                                  lambda *_a, **_k: {"ok": False, "error": "torch.utils 崩了",
                                                     "skipped": False}), \
                mock.patch.object(backend_fetch, "find_offline_zip", lambda *_a, **_k: None), \
                mock.patch.object(backend_fetch, "find_package_zip", lambda *_a, **_k: None), \
                mock.patch.object(backend_fetch, "_run_pip",
                                  lambda *_a, **_k: (False, "测试里不联网")):
            ok, detail = backend_fetch.ensure_runtime(on_step=_steps)
        self.assertFalse(ok, "残 runtime 不该被当成就绪")
        self.assertTrue(any("torch 不完整" in m for m in calls), calls)


if __name__ == "__main__":
    unittest.main()
