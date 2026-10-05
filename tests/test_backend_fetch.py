# -*- coding: utf-8 -*-
"""薄包那条路的**取运行时**（`app/backend_fetch.py`）。

用户 2026-09-30 拍板：**默认 = 薄包 + 国内可下载**。这一层的价值全在"**别把坑留到运行时**"，
所以用例钉的就是那两条纪律 + 几条诚实边界：

  1. **torch 与 torchaudio 必须同一条 pip、同一个 CUDA 索引** —— 分开装/让 torchaudio 从普通
     PyPI 被顺带拉进来，会拿到无标签那份 → `import torchaudio` 崩 → funasr 加载失败 →
     **每个 `/v1/asr` 都 503**（`AGENTS.md` 记的坑）。这条用"抓到的 pip 参数"钉死。
  2. **装完必须自检**（ABI + `import funasr`），失败要带 pip 原文、要**响亮**。
  3. **永不抛**：pip 崩、包没解开、没解释器，全都变成 `(False, 人话)`。
  4. **不谎报**：老卡档（cu118）没有说话人分离，如实写在计划里。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backend_env, backend_fetch, backend_proc, backend_setup   # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-fetch-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for target, value in ((backend_setup, lambda: self.root),
                              (backend_proc, lambda: self.root),
                              (backend_fetch.backend_setup, lambda: self.root)):
            _ = target
        p = mock.patch.object(backend_setup, "backend_root", lambda: self.root)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(backend_proc, "backend_root", lambda: self.root)
        p.start()
        self.addCleanup(p.stop)
        self.exe = os.path.join(self.root, "runtime", "Scripts", "python.exe")
        os.makedirs(os.path.dirname(self.exe), exist_ok=True)
        open(self.exe, "w", encoding="utf-8").close()
        p = mock.patch.object(backend_proc, "python_exe", lambda: self.exe)
        p.start()
        self.addCleanup(p.stop)
        # 2026-10-04：**必须屏蔽"用户显式指的包"**（设置 `capabilityBackendPackage` /
        # 环境变量 `ECHO_BACKEND_PACKAGE`）。开发机上那个设置指着 3.1 GB 的离线包，
        # 用例于是走了"有离线包"的分支：不但前提（没有薄包）不成立，还会真的去解那个包
        # —— 单跑一次 225 秒、门禁连红两轮（全量里表现为 `assertIn("薄包", …)` 失败）。
        # 只屏蔽这一个键，别的设置照旧（本文件别的用例显式打桩了自己关心的那一层）。
        _real_setting = backend_fetch._setting
        p = mock.patch.object(
            backend_fetch, "_setting",
            lambda key, default=None: ("" if key == backend_fetch.PACKAGE_SETTING
                                       else _real_setting(key, default)))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"ECHO_BACKEND_PACKAGE": ""})
        p.start()
        self.addCleanup(p.stop)
        # `ensure_pip` 默认桩成"pip 可用"：它要**跑真解释器**，而这里的 `self.exe` 是个空文件。
        # 它自己的行为（摘 PEP 668 标记 / 离线装回 pip）在 `PipSelfHealTests` 里单独测。
        p = mock.patch.object(backend_fetch, "ensure_pip",
                              lambda exe, on_step=None: (True, "pip 可用（桩）"))
        p.start()
        self.addCleanup(p.stop)
        # 薄包已解开的样子：`server/requirements.txt` 在
        os.makedirs(os.path.join(self.root, "server"), exist_ok=True)
        with open(os.path.join(self.root, "server", "requirements.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("fastapi>=0.115\nuvicorn[standard]>=0.30\n")

    @property
    def root(self):
        return os.path.join(self.tmp, "backend")

    def _run_pip(self, calls, code=0, out="ok"):
        """桩掉 `_run_pip` 并**记下每次调用的参数**（好几条用例都要看"跑了几次 pip"）。"""
        def _fake(exe, args, label, on_step=None):
            calls.append({"exe": exe, "args": list(args), "label": label})
            return (code == 0), ("%s 完成" % label if code == 0
                                 else "pip 退出码 %d：\n%s" % (code, out))
        return _fake

    def stub_deps(self, ok=False, error="ModuleNotFoundError: No module named 'fastapi'"):
        """桩掉"运行时**到底能不能用**"那条快探（`backend_env.check_server_deps`）。

        **装依赖那几条用例必须显式桩它**：它们会把 `subprocess.run` 整个换成 Mock，而
        `check_server_deps` 走的正是**同一个 `subprocess` 模块对象** —— 不桩的话探测会被
        那个 Mock 顶成"能用"，`ensure_runtime` 于是直接早退、pip 一次都不跑，
        用例就测不到它本来要测的东西（第一版改完就是这么红了一条）。
        """
        p = mock.patch.object(
            backend_env, "check_server_deps",
            lambda exe, timeout=120.0: {"ok": bool(ok), "error": "" if ok else error,
                                        "output": ""})
        p.start()
        self.addCleanup(p.stop)


class SourceTests(_Case):
    def test_shapes_the_two_phase_plan(self):
        """两阶段：① torch/torchaudio 走 **CUDA 索引**；② 其余走**国内 PyPI 镜像**。"""
        need = backend_fetch.requirements("cu126")
        self.assertIn("mirror.sjtu.edu.cn/pytorch-wheels/cu126", need["torchIndex"])
        self.assertIn("torch", need["torch"][0])
        self.assertIn("torchaudio", need["torch"][1])
        self.assertTrue(all("tuna.tsinghua" in i or "aliyun" in i for i in need["pipIndexes"]),
                        need["pipIndexes"])
        self.assertIn("funasr", need["extras"])

    def test_the_old_card_variant_pins_torch_and_says_what_it_loses(self):
        """老卡档钉 torch 2.7.1，并且**如实**说"这一档没有分离"。"""
        need = backend_fetch.requirements("cu118")
        self.assertEqual(need["torch"], ["torch==2.7.1", "torchaudio==2.7.1"])
        self.assertIn("cu118", need["torchIndex"])
        self.assertIn("分离", need["note"])
        self.assertNotIn("pyannote.audio", need["extras"])

    def test_plan_is_read_only_and_says_where_things_go(self):
        got = backend_fetch.plan("cu126")
        self.assertTrue(got["sourceReady"], got)
        self.assertEqual(got["runtimeDir"], os.path.join(self.root, "runtime"))
        self.assertIn("国内源", got["headline"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "logs")),
                         "计划是只读的：不该建目录")


class EnsureRuntimeTests(_Case):
    def test_pip_runs_in_utf8_mode(self):
        """**跑 pip 必须强制 UTF-8 模式**（2026-10-01 真机踩到）。

        现场：`pip install -r server/requirements.txt` →
        `UnicodeDecodeError: 'gbk' codec can't decode byte 0xab in position 17`
        —— 那份清单是「UTF-8 中文注释 + 没有 PEP263 声明」，而 pip 的 `auto_decode()` 在没有
        BOM/cookie 时**按 locale 解码**（中文 Windows = cp936）。

        为什么这里必须是**环境变量**而不是"把文件改好就行"：目标机手上的包可能是**旧的**
        （发出去之后才发现这个坑），`PYTHONUTF8=1` 让 `locale.getpreferredencoding()` 变 UTF-8，
        于是**旧包也照样能装**。判据就是这两条环境变量真的传给了子进程。
        """
        seen = {}

        def _fake_run(cmd, **kw):
            seen["cmd"] = cmd
            seen["env"] = kw.get("env") or {}
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        with mock.patch.object(backend_fetch.subprocess, "run", _fake_run):
            ok, detail = backend_fetch._run_pip(self.exe, ["torch"], "装 torch")
        self.assertTrue(ok, detail)
        self.assertEqual(seen["env"].get("PYTHONUTF8"), "1",
                         "没给 PYTHONUTF8=1：中文 Windows 上 pip 会按 cp936 读清单并崩")
        self.assertEqual(seen["env"].get("PYTHONIOENCODING"), "utf-8")
        self.assertIn("-m", seen["cmd"][:3])

    def test_torch_and_torchaudio_share_one_index_and_one_pip_call(self):
        """**纪律 1**：两者必须出现在**同一条** pip、**同一个** `--index-url` 里。"""
        calls = []
        self.stub_deps()
        abi = {"ok": True, "torch": "2.10.0+cu128", "torchaudio": "2.10.0+cu128",
               "error": "", "note": ""}
        with mock.patch.object(backend_fetch, "_run_pip", self._run_pip(calls)), \
                mock.patch.object(backend_env, "check_torch_abi", lambda exe, timeout=180.0: abi), \
                mock.patch.object(backend_fetch.subprocess, "run",
                                  lambda *a, **kw: mock.Mock(returncode=0, stdout="1.2.3\n",
                                                             stderr="")):
            ok, detail = backend_fetch.ensure_runtime("cu128")
        self.assertTrue(ok, detail)
        first = calls[0]["args"]
        self.assertEqual([a for a in first if a.startswith("torch")], ["torch", "torchaudio"])
        self.assertIn("--index-url", first)
        self.assertIn(backend_fetch.torch_index("cu128"), first)
        self.assertEqual(abi["torch"], "2.10.0+cu128")

    def test_the_real_pip_runner_writes_everything_to_the_log(self):
        """`_run_pip` 真跑时：命令与输出都进 `{backend}/logs/fetch-runtime.log`（失败时要能查）。"""
        seen = {}

        def _fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return mock.Mock(returncode=0, stdout="Successfully installed torch\n", stderr="")

        with mock.patch.object(backend_fetch.subprocess, "run", _fake_run):
            ok, detail = backend_fetch._run_pip(self.exe, ["torch", "--index-url", "x"],
                                                "装 torch")
        self.assertTrue(ok, detail)
        self.assertIn("-m", seen["cmd"][:3])
        with open(backend_fetch.log_path(), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("torch", text)
        self.assertIn("Successfully installed", text)

    def test_a_failed_torch_install_stops_before_anything_else(self):
        """torch 那步失败就**停**（别接着装一堆没用的依赖），并把 pip 原文带出去。

        注意"停"的边界变了（2026-10-01）：torch 的索引现在是一**串**（一个源抖了就换下一个），
        所以"失败"指的是**所有索引都试过**；判据是**绝不进入"其余依赖"那一步**。
        """
        calls = []
        self.stub_deps()
        with mock.patch.object(backend_fetch, "_run_pip", self._run_pip(calls, code=1, out="boom")), \
                mock.patch.object(backend_env, "check_torch_abi",
                                  lambda exe, timeout=180.0: {"ok": False, "error": "不该走到这"}):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertFalse(ok)
        self.assertIn("pip 退出码 1", detail)
        self.assertEqual(len(calls), len(backend_fetch.TORCH_INDEX_TMPLS),
                         "每个索引都该试一次：%s" % [c["label"] for c in calls])
        self.assertTrue(all("torch" in c["args"][0] for c in calls),
                        "失败之后不该去装别的依赖：%s" % [c["label"] for c in calls])
        self.assertEqual([c["args"][-1] for c in calls], backend_fetch.torch_indexes("cu126"),
                         "按 `torch_indexes()` 的顺序试，最后一个参数就是那个 index-url")

    def test_the_first_working_torch_index_wins(self):
        """第一个通了就不试后面的（别把几 GB 的轮子从多个源各下一份）。"""
        calls = []
        self.stub_deps()
        abi = {"ok": True, "torch": "t", "torchaudio": "a", "error": "", "note": ""}
        index = backend_fetch.TORCH_INDEX_TMPLS[1] % "cu126"

        def _fake(exe, args, label, on_step=None):
            calls.append({"args": list(args), "label": label})
            torch_stage = label.startswith("装 torch")
            return ((not torch_stage) or args[-1] == index), "ok"

        with mock.patch.object(backend_fetch, "_run_pip", _fake), \
                mock.patch.object(backend_env, "check_torch_abi", lambda exe, timeout=180.0: abi), \
                mock.patch.object(backend_fetch.subprocess, "run",
                                  lambda *a, **kw: mock.Mock(returncode=0, stdout="1.0\n",
                                                             stderr="")):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertTrue(ok, detail)
        torch_calls = [c for c in calls if c["args"][0] == "torch"]
        self.assertEqual(len(torch_calls), 2, "第二个索引通了就该停：%s"
                         % [c["label"] for c in calls])

    def test_a_failing_abi_self_check_is_a_failure(self):
        """**纪律 2**：装完 ABIm 自检不过 = 失败（不许留到运行时表现为 503）。"""
        calls = []
        self.stub_deps()
        with mock.patch.object(backend_fetch, "_run_pip", self._run_pip(calls)), \
                mock.patch.object(backend_env, "check_torch_abi",
                                  lambda exe, timeout=180.0: {"ok": False,
                                                              "error": "标签不一致"}):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertFalse(ok)
        self.assertIn("ABI 自检没过", detail)
        self.assertIn("标签不一致", detail)

    def test_a_broken_funasr_import_is_a_failure_with_the_raw_text(self):
        calls = []
        self.stub_deps()
        abi = {"ok": True, "torch": "t", "torchaudio": "a", "error": "", "note": ""}
        with mock.patch.object(backend_fetch, "_run_pip", self._run_pip(calls)), \
                mock.patch.object(backend_env, "check_torch_abi", lambda exe, timeout=180.0: abi), \
                mock.patch.object(backend_fetch.subprocess, "run",
                                  lambda *a, **kw: mock.Mock(
                                      returncode=1, stdout="", stderr="ImportError: no funasr")):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertFalse(ok)
        self.assertIn("import funasr", detail)
        self.assertIn("no funasr", detail)

    def test_an_interpreter_alone_does_not_skip_the_install(self):
        """**2026-10-01 真机的那个 bug**：薄包**只带解释器**，依赖要靠这一步装。

        旧判据是 `backend_proc.python_exe()`（那个文件在就返回「运行时已在」）—— 于是 pip
        一次都不跑，后端一起来就 `ModuleNotFoundError: No module named 'fastapi'` 退出。
        """
        calls = []
        self.stub_deps(ok=False, error="ModuleNotFoundError: No module named 'fastapi'")
        abi = {"ok": True, "torch": "t", "torchaudio": "a", "error": "", "note": ""}
        with mock.patch.object(backend_fetch, "_run_pip", self._run_pip(calls)), \
                mock.patch.object(backend_env, "check_torch_abi", lambda exe, timeout=180.0: abi), \
                mock.patch.object(backend_fetch.subprocess, "run",
                                  lambda *a, **kw: mock.Mock(returncode=0, stdout="1.2.3\n",
                                                             stderr="")):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertTrue(ok, detail)
        self.assertEqual(len(calls), 2,
                         "解释器在但依赖没装全时**必须**真的去装（这就是那个 bug 的判据）：%s"
                         % [c["label"] for c in calls])

    def test_a_runtime_that_really_works_skips_the_install(self):
        """**两个方向都要钉**：真能用就别再折腾（pip 一次都不跑）。"""
        calls = []
        self.stub_deps(ok=True)
        with mock.patch.object(backend_fetch, "_run_pip", self._run_pip(calls)):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertTrue(ok, detail)
        self.assertIn("运行时已在", detail)
        self.assertEqual(calls, [], "能用就不该再跑 pip")

    def test_it_asks_whether_the_runtime_works_before_looking_for_a_pack(self):
        """顺序：**先问"能不能用"、再找薄包** —— 能跑时就与 `requirements.txt` 在不在无关。"""
        shutil.rmtree(os.path.join(self.root, "server"))
        self.stub_deps(ok=True)
        ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertTrue(ok, detail)
        self.assertIn("运行时已在", detail)

    def test_the_progress_line_says_half_installed_not_already_there(self):
        """面板上**不许**再出现「运行时已在」那句假话 —— 它正是把人骗过去的那一句。"""
        seen = []
        self.stub_deps(ok=False, error="ModuleNotFoundError: No module named 'fastapi'")
        with mock.patch.object(backend_fetch, "_run_pip",
                               lambda exe, args, label, on_step=None: (False, "boom")):
            ok, detail = backend_fetch.ensure_runtime("cu126", on_step=seen.append)
        self.assertFalse(ok)
        self.assertTrue(any("只装了一半" in s for s in seen), seen)
        self.assertTrue(any("fastapi" in s for s in seen), seen)
        self.assertFalse(any("运行时已在" in s for s in seen), seen)

    def test_it_never_raises_without_an_interpreter_or_an_unpacked_pack(self):
        with mock.patch.object(backend_proc, "python_exe", lambda: ""):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertFalse(ok)
        self.assertIn("CPython", detail)

        shutil.rmtree(os.path.join(self.root, "server"))
        # 薄包没了 → 这一步会**先去取薄包**（第 −1 步）；取不到就如实说"看过哪些地方"
        # （2026-09-30 之前这里报的是"薄包还没解开 —— 先手工解到 …"）。
        with mock.patch.object(backend_fetch, "search_dirs", lambda: []):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertFalse(ok)
        self.assertIn("薄包", detail)
        self.assertIn("capabilityBackendPackage", detail)

    def test_a_raising_pip_is_reported_not_propagated(self):
        def _boom(*a, **kw):
            raise OSError("管道断了")
        self.stub_deps()
        with mock.patch.object(backend_fetch.subprocess, "run", _boom):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertFalse(ok)
        self.assertIn("管道断了", detail)


class OfflinePackTests(_Case):
    """**后端离线包**（2026-10-01 加）：薄包 + **装好依赖的 `runtime/`**，同一个 zip。

    用户的口径：交付时判断交付目录里有没有离线包 —— **有就直接复制启用（零下载），
    没有才触发下载**。这一组钉三件事：
      ① 认包**靠内容**（有 `runtime/`），不靠名字；
      ② 认出来之后**零下载**（pip 一次都不跑）；
      ③ 离线包坏了**不许把整件事判死** —— 如实说一句，然后照旧走国内源。
    """

    def write_zip(self, name, *, runtime=True, deps=False, server=True, top=None):
        """造一个后端包 zip。`deps=True` = `runtime/` 里**装过依赖**（离线包那一半）。

        注意 `deps` 与 `runtime` 是**两件事**：薄包也带 `runtime/`（只有解释器 + 标准库），
        而离线包的判据是"装过依赖"（`_has_installed_deps`）。
        """
        path = os.path.join(self.tmp, name)
        top = top or name[:-4]
        with zipfile.ZipFile(path, "w") as zf:
            if server:
                zf.writestr("%s/server/requirements.txt" % top, "fastapi>=0.115\n")
            if runtime:
                zf.writestr("%s/runtime/python.exe" % top, "")
            if deps:
                zf.writestr("%s/runtime/Lib/site-packages/fastapi/__init__.py" % top, "")
                zf.writestr("%s/runtime/Lib/site-packages/fastapi-0.141.1.dist-info/METADATA"
                            % top, "Name: fastapi\n")
        return path

    def test_the_delivery_folder_beside_the_install_root_is_searched(self):
        """交付目录在搜索落点里（实测那台是 `D:\\ECHO` 配 `D:\\ECHO-delivery`）。

        "同级的交付资料夹"这一条是**实测**加进来的：原来只找"安装根自己"与"它的上一层"，
        而用户的交付目录是与安装根**平级**的一个文件夹 —— 于是那份 20 MB 薄包明明在手边，
        面板却还说"本机没找到"，逼人手工解压一次。

        注意这里要把**安装根**搭对：`backend_root = <tmp>/ECHO/backend` → 安装根 `<tmp>/ECHO`
        → 它的同级 `<tmp>/ECHO-delivery` 才是"与安装根平级"那一种。
        """
        install_root = os.path.join(self.tmp, "ECHO")
        os.makedirs(os.path.join(install_root, "backend"), exist_ok=True)
        deliv = os.path.join(self.tmp, "ECHO-delivery")
        os.makedirs(deliv, exist_ok=True)
        # **交付目录里那一层带日期的版本目录**（用户 2026-10-01 定的形态）：zip 摆在它里面。
        # 少了它，事后 ECHO 自己想重新解包时会说"没找到包、让你去下载"——而包就在旁边。
        dated = os.path.join(deliv, "ECHO-delivery-20261001-1028")
        os.makedirs(dated, exist_ok=True)
        for sub in ("交付", "delivery", "dist", "kit"):
            os.makedirs(os.path.join(install_root, sub), exist_ok=True)
        with mock.patch.object(backend_setup, "backend_root",
                               lambda: os.path.join(install_root, "backend")):
            dirs = backend_fetch.search_dirs()
        self.assertIn(os.path.abspath(deliv), dirs,
                      "与安装根**平级**的交付资料夹要在落点里：%s" % dirs)
        self.assertIn(os.path.abspath(dated), dirs,
                      "交付资料夹里那一层 **带日期的版本目录** 也要在（包就在它里面）：%s" % dirs)
        for sub in ("交付", "delivery", "dist", "kit"):
            self.assertIn(os.path.abspath(os.path.join(install_root, sub)), dirs, dirs)

    def test_the_dated_delivery_layer_is_where_the_pack_actually_is(self):
        """端到端那半句：把包放进"带日期那一层"，`find_offline_zip()` 必须找得到它。"""
        install_root = os.path.join(self.tmp, "ECHO")
        os.makedirs(os.path.join(install_root, "backend"), exist_ok=True)
        dated = os.path.join(self.tmp, "ECHO-delivery", "ECHO-delivery-20261001-1028")
        os.makedirs(dated, exist_ok=True)
        self.write_zip("ECHO-backend-offline-cu128-test.zip", deps=True)
        made = os.path.join(self.tmp, "ECHO-backend-offline-cu128-test.zip")
        os.replace(made, os.path.join(dated, os.path.basename(made)))
        with mock.patch.object(backend_setup, "backend_root",
                               lambda: os.path.join(install_root, "backend")):
            found = backend_fetch.find_offline_zip()
        self.assertIsNotNone(found, "带日期那一层里的离线包没被找到")
        self.assertEqual(os.path.dirname(os.path.abspath(found)), os.path.abspath(dated))

    def test_an_offline_pack_is_recognized_by_the_deps_inside_its_runtime(self):
        """判据是**内容**：`runtime/` 里**装过依赖**（site-packages 里有 fastapi）。"""
        z = self.write_zip("ECHO-backend-offline-cu126-test.zip", deps=True)
        with mock.patch.object(backend_fetch, "search_dirs", lambda: [self.tmp]):
            self.assertEqual(backend_fetch.find_offline_zip(), os.path.abspath(z))
            self.assertEqual(backend_fetch.find_package_zip(), os.path.abspath(z),
                             "离线包同时满足两件事 —— 取包那一步就该先挑它")

    def test_a_thick_pack_under_the_plain_name_is_still_an_offline_pack(self):
        """**名字不该决定判据**：`build_backend_portable.py --runtime-from` 出的厚包名字仍是
        `ECHO-backend-portable-*`，它照样是离线包（否则出包侧被逼着改名字才能被认出来）。"""
        z = self.write_zip("ECHO-backend-portable-thick-20261001.zip", deps=True)
        with mock.patch.object(backend_fetch, "search_dirs", lambda: [self.tmp]):
            self.assertEqual(backend_fetch.find_offline_zip(), os.path.abspath(z))
            self.assertEqual(backend_fetch.find_package_zip(), os.path.abspath(z))

    def test_a_thin_pack_with_a_bare_interpreter_is_never_an_offline_pack(self):
        """只带解释器（没有依赖）的薄包**不是**离线包 —— 把它当离线包会让用户
        "复制启用"完了还得下 3 GB（说错话比不说话更贵）。"""
        thin = self.write_zip("ECHO-backend-portable-thin.zip")          # runtime 在、依赖不在
        self.assertTrue(backend_fetch._has_runtime_dir(backend_fetch._zip_names(thin)))
        self.assertFalse(backend_fetch._has_installed_deps(backend_fetch._zip_names(thin)))
        with mock.patch.object(backend_fetch, "search_dirs", lambda: [self.tmp]):
            self.assertEqual(backend_fetch.find_offline_zip(), "")
            self.assertEqual(backend_fetch.find_package_zip(), os.path.abspath(thin))

    def test_an_offline_name_with_a_thin_body_is_refused(self):
        """名字像离线包但里面只有解释器 → 拒绝（名字只是提示，判据在内容）。"""
        self.write_zip("ECHO-backend-offline-fake.zip")                  # 没 deps
        with mock.patch.object(backend_fetch, "search_dirs", lambda: [self.tmp]):
            self.assertEqual(backend_fetch.find_offline_zip(), "")

    def test_a_named_like_an_offline_pack_but_empty_zip_is_refused(self):
        junk = os.path.join(self.tmp, "ECHO-backend-offline-junk.zip")
        with zipfile.ZipFile(junk, "w") as zf:
            zf.writestr("readme.txt", "hi")
        with mock.patch.object(backend_fetch, "search_dirs", lambda: [self.tmp]):
            self.assertEqual(backend_fetch.find_offline_zip(), "")

    def test_a_local_offline_pack_is_copied_in_without_any_pip(self):
        """**有就直接复制启用（零下载）** —— 这就是那"第 4 件"的正解。"""
        calls, seen = [], []
        state = {"deps": False}

        def _deps(exe, timeout=120.0):
            return {"ok": state["deps"], "output": "",
                    "error": "" if state["deps"] else "No module named 'fastapi'"}

        def _extract(zip_path, on_step=None):
            state["deps"] = True                      # 复制启用后运行时就能用了
            return True, "薄包已解开到 X（来自 %s）" % zip_path

        with mock.patch.object(backend_fetch, "find_offline_zip",
                               lambda: os.path.join(self.tmp, "ECHO-backend-offline-x.zip")), \
                mock.patch.object(backend_env, "check_server_deps", _deps), \
                mock.patch.object(backend_fetch, "extract_package", _extract), \
                mock.patch.object(backend_fetch, "_run_pip", self._run_pip(calls)):
            ok, detail = backend_fetch.ensure_runtime("cu126", on_step=seen.append)
        self.assertTrue(ok, detail)
        self.assertIn("离线包", detail)
        self.assertEqual(calls, [], "有离线包就不该跑任何 pip（零下载）：%s" % calls)
        self.assertTrue(any("不下载" in s for s in seen), seen)

    def test_a_broken_offline_pack_falls_back_to_the_download(self):
        """离线包坏了 → **如实说一句**，然后照旧走国内源（不把整件事判死）。"""
        calls, seen = [], []
        abi = {"ok": True, "torch": "t", "torchaudio": "a", "error": "", "note": ""}

        def _extract(zip_path, on_step=None):
            return False, "解不开：不是 zip"

        with mock.patch.object(backend_fetch, "find_offline_zip",
                               lambda: os.path.join(self.tmp, "ECHO-backend-offline-bad.zip")), \
                mock.patch.object(backend_env, "check_server_deps",
                                  lambda exe, timeout=120.0: {"ok": False, "output": "",
                                                              "error": "No module named 'fastapi'"}), \
                mock.patch.object(backend_fetch, "extract_package", _extract), \
                mock.patch.object(backend_fetch, "_run_pip", self._run_pip(calls)), \
                mock.patch.object(backend_env, "check_torch_abi", lambda exe, timeout=180.0: abi), \
                mock.patch.object(backend_fetch.subprocess, "run",
                                  lambda *a, **kw: mock.Mock(returncode=0, stdout="1.2.3\n",
                                                             stderr="")):
            ok, detail = backend_fetch.ensure_runtime("cu126", on_step=seen.append)
        self.assertTrue(ok, detail)
        self.assertEqual(len(calls), 2, "离线包坏了就该照旧装：%s" % calls)
        self.assertTrue(any("离线包没能启用" in s for s in seen), seen)

    def test_the_plan_promises_zero_download_when_an_offline_pack_is_there(self):
        """面板那两个数要跟着离线包走：`0 GB / 零下载` vs `3 GB / 国内源`。"""
        z = self.write_zip("ECHO-backend-offline-cu126-x.zip", deps=True)
        with mock.patch.object(backend_fetch, "search_dirs", lambda: [self.tmp]):
            got = backend_fetch.plan("cu126")
        self.assertTrue(got["offline"]["found"], got["offline"])
        self.assertEqual(got["offline"]["path"], os.path.abspath(z))
        self.assertEqual(got["approxDownloadGB"], 0.0)
        self.assertIn("零下载", got["headline"])

    def test_the_plan_still_promises_the_download_without_one(self):
        with mock.patch.object(backend_fetch, "search_dirs", lambda: [self.tmp]):
            got = backend_fetch.plan("cu126")
        self.assertFalse(got["offline"]["found"])
        self.assertEqual(got["approxDownloadGB"], 3.0)
        self.assertIn("国内源", got["headline"])


class _NoPackage(_Case):
    """**薄包还没解开**的机器：`server/requirements.txt` 不在，本机那些落点也找不到 zip。

    这一组用例盯的是 2026-09-30 接进来的"第 −1 步"：以前 `plan()` 只能说"薄包解好之后
    一次点击就够了"，因为**薄包本身**要人工解到 `{backend}`。
    """

    def setUp(self):
        super().setUp()
        shutil.rmtree(os.path.join(self.root, "server"), ignore_errors=True)
        for name, value in (("search_dirs", lambda: []), ("package_source", lambda: ""),
                            ("find_package_zip", lambda: "")):
            p = mock.patch.object(backend_fetch, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_package_zip(self, name="ECHO-backend-portable-test.zip",
                          top="ECHO-backend-portable-test"):
        """造一个**真的**薄包 zip（结构照 `scripts/build_backend_portable.py`）。"""
        path = os.path.join(self.tmp, name)
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("%s/server/requirements.txt" % top, "fastapi>=0.115\nuvicorn>=0.30\n")
            zf.writestr("%s/manifest.json" % top,
                        json.dumps({"package": "backend-portable", "mode": "thin"}))
            zf.writestr("%s/server.yaml.tmpl" % top, "server:\n  listen: 127.0.0.1:8900\n")
            zf.writestr("%s/app/main.py" % top, "# app\n")
        return path


class PackageStepTests(_NoPackage):
    def test_the_plan_is_read_only_and_lists_where_it_looked(self):
        plan = backend_fetch.package_plan()
        self.assertFalse(plan["ready"])
        self.assertFalse(plan["willFetch"])
        self.assertFalse(plan["source"])
        self.assertIn("薄包", plan["headline"])
        self.assertFalse(os.path.isdir(os.path.join(self.root, "logs")),
                         "计划是只读的：不该建目录")
        self.assertFalse(os.path.isdir(os.path.join(self.root, "tmp")))

    def test_a_local_zip_is_unpacked_into_the_backend_home(self):
        z = self.write_package_zip()
        with mock.patch.object(backend_fetch, "find_package_zip", lambda: z):
            self.assertTrue(backend_fetch.package_plan()["willFetch"])
            ok, detail = backend_fetch.ensure_package()
        self.assertTrue(ok, detail)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "server", "requirements.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "server.yaml.tmpl")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "app", "main.py")))
        leftovers = [n for n in os.listdir(os.path.join(self.root, "tmp"))]
        self.assertEqual(leftovers, [], "临时解包目录没清干净：%s" % leftovers)
        with open(backend_fetch.package_log_path(), encoding="utf-8") as fh:
            self.assertIn("已解开薄包", fh.read())

    def test_an_already_unpacked_package_skips_the_work(self):
        os.makedirs(os.path.join(self.root, "server"), exist_ok=True)
        with open(os.path.join(self.root, "server", "requirements.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("fastapi\n")
        ok, detail = backend_fetch.ensure_package()
        self.assertTrue(ok, detail)
        self.assertIn("已在", detail)
        self.assertTrue(backend_fetch.package_plan()["ready"])

    def test_an_explicit_path_is_honoured(self):
        z = self.write_package_zip()
        with mock.patch.object(backend_fetch, "package_source", lambda: z):
            ok, detail = backend_fetch.ensure_package()
        self.assertTrue(ok, detail)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "server", "requirements.txt")))

    def test_an_explicit_path_that_is_not_there_fails_honestly(self):
        missing = os.path.join(self.tmp, "nope.zip")
        with mock.patch.object(backend_fetch, "package_source", lambda: missing):
            ok, detail = backend_fetch.ensure_package()
        self.assertFalse(ok)
        self.assertIn("不在", detail)
        self.assertIn(missing, detail)

    def test_a_zip_that_is_not_a_backend_package_is_refused(self):
        """名字对得上但内容不对（没有 `server/requirements.txt`）→ 说"不像薄包"。"""
        junk = self.write_package_zip("ECHO-backend-portable-junk.zip", "ECHO-backend-portable-junk")
        with zipfile.ZipFile(junk, "a") as zf:          # 把那份 requirements 拿掉
            pass
        bad = os.path.join(self.tmp, "ECHO-backend-portable-bad.zip")
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("readme.txt", "hi")
        with mock.patch.object(backend_fetch, "package_source", lambda: bad):
            ok, detail = backend_fetch.ensure_package()
        self.assertFalse(ok)
        self.assertIn("不像薄包", detail)
        self.assertFalse(backend_fetch._is_backend_package(bad))
        self.assertTrue(backend_fetch._is_backend_package(junk))

    def test_a_renamed_delivery_zip_is_still_recognized_by_content(self):
        """交付汇总目录里那份被人工改名成 `3-本机GPU后端包-20MB.zip` —— **判据是内容**。"""
        z = self.write_package_zip("3-本机GPU后端包-20MB.zip", "ECHO-backend-portable-clean-room")
        self.assertTrue(backend_fetch._is_backend_package(z))

    def test_a_url_is_downloaded_and_then_unpacked(self):
        z = self.write_package_zip("ECHO-backend-portable-url.zip", "ECHO-backend-portable-url")
        with open(z, "rb") as fh:
            blob = fh.read()

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()
                return False

        with mock.patch.object(backend_fetch, "package_source",
                               lambda: "https://mirror.invalid/ECHO-backend-portable-url.zip"), \
                mock.patch.object(backend_fetch.urllib.request, "urlopen",
                                  lambda url, timeout=0: _Resp(blob)):
            ok, detail = backend_fetch.ensure_package()
        self.assertTrue(ok, detail)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "server", "requirements.txt")))

    def test_only_http_urls_are_downloaded(self):
        path, err = backend_fetch.download_package("ftp://mirror.invalid/x.zip")
        self.assertEqual(path, "")
        self.assertIn("http(s)", err)

    def test_a_download_that_blows_up_is_reported_not_propagated(self):
        def _boom(url, timeout=0):
            raise OSError("網絡斷了")
        with mock.patch.object(backend_fetch.urllib.request, "urlopen", _boom):
            path, err = backend_fetch.download_package("https://mirror.invalid/x.zip")
        self.assertEqual(path, "")
        self.assertIn("網絡斷了", err)

    def test_a_zip_that_tries_to_escape_is_refused(self):
        """薄包也是**外部输入**：`../` 条目不许写到 `{backend}` 之外。"""
        evil = os.path.join(self.tmp, "evil.zip")
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../escaped.txt", "boom")
            zf.writestr("ECHO-backend-portable-x/server/requirements.txt", "fastapi\n")
        ok, detail = backend_fetch.extract_package(evil)
        self.assertFalse(ok)
        self.assertIn("不安全", detail)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escaped.txt")))

    def test_a_zip_without_the_payload_is_refused(self):
        flat = os.path.join(self.tmp, "ECHO-backend-portable-flat.zip")
        with zipfile.ZipFile(flat, "w") as zf:
            zf.writestr("readme.txt", "hi")
        ok, detail = backend_fetch.extract_package(flat)
        self.assertFalse(ok)
        self.assertIn("没有薄包", detail)

    def test_the_package_comes_before_the_interpreter_check(self):
        """**顺序**：先取薄包、再问解释器 —— 解释器可能就在薄包自带的 `runtime/` 里。

        反过来的话说错了方向：薄包还没到时报"薄包里没有解释器"，会把人引去装 Python，
        而那条路根本不需要（薄包自己带 CPython，见 `scripts/build_backend_portable.py`）。
        """
        with mock.patch.object(backend_fetch, "ensure_package",
                               lambda on_step=None: (False, "薄包还没有：X")):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertFalse(ok)
        self.assertIn("薄包还没有", detail)
        self.assertNotIn("解释器", detail)

    def test_after_the_package_arrives_the_interpreter_is_the_next_complaint(self):
        def _fetch(on_step=None):
            os.makedirs(os.path.join(self.root, "server"), exist_ok=True)
            with open(os.path.join(self.root, "server", "requirements.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("fastapi\n")
            return True, "薄包已解开"
        with mock.patch.object(backend_fetch, "ensure_package", _fetch), \
                mock.patch.object(backend_proc, "python_exe", lambda: ""):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertFalse(ok)
        self.assertIn("解释器", detail)

    def test_plan_carries_the_package_block_the_panel_renders(self):
        got = backend_fetch.plan("cu126")
        self.assertIn("package", got)
        self.assertFalse(got["sourceReady"])
        self.assertFalse(got["willFetch"])
        self.assertIn("packageSearch", got)
        self.assertTrue(got["packageSearch"] == backend_fetch.search_dirs())
        json.dumps(got, ensure_ascii=False)


class PipSelfHealTests(unittest.TestCase):
    """**让解释器"能装东西"**（2026-10-01 真机实测的两条，叠在薄包那份 runtime 上）。

    薄包的 `runtime/` 是**搬过来的 uv 托管 CPython**：① 留着 `Lib/EXTERNALLY-MANAGED`
    （PEP 668）→ pip 一律拒绝安装；② 它的 pip 还**残缺**（`No module named 'pip._internal.models'`）。
    两条都会让"点一下就装依赖"失败，而报错**都不提"包没装"** —— 所以这一步单独钉。

    **刻意不继承 `_Case`**：那一套会把 `ensure_pip` 打桩成"pip 可用"，而这里要测的正是它自己。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-pip-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.exe = os.path.join(self.tmp, "runtime", "Scripts", "python.exe")
        os.makedirs(os.path.dirname(self.exe), exist_ok=True)
        open(self.exe, "w", encoding="utf-8").close()

    def _marker(self, rel="Lib/EXTERNALLY-MANAGED"):
        path = os.path.join(self.tmp, "runtime", *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").close()
        return path

    def test_the_runtime_root_is_found_from_either_layout(self):
        for rel in ("runtime/python.exe", "runtime/Scripts/python.exe",
                    "runtime/bin/python3", "runtime/bin/python"):
            with self.subTest(rel=rel):
                self.assertEqual(backend_fetch.runtime_root_for(os.path.join(self.tmp, *rel.split("/"))),
                                 os.path.join(self.tmp, "runtime"))

    def test_it_strips_the_externally_managed_marker(self):
        """uv / Debian 托管的 CPython 会留这个标记 —— 不摘掉，pip 一律说
        `This environment is externally managed`（看着像权限问题，其实是"不许装"）。"""
        a = self._marker("Lib/EXTERNALLY-MANAGED")
        b = self._marker("lib/python3.11/EXTERNALLY-MANAGED")
        self.assertEqual(backend_fetch.external_markers(self.exe), sorted([a, b]))
        removed = backend_fetch.unmark_externally_managed(self.exe)
        self.assertEqual(sorted(removed), sorted([a, b]))
        self.assertFalse(os.path.exists(a))
        self.assertFalse(os.path.exists(b))
        self.assertEqual(backend_fetch.unmark_externally_managed(self.exe), [],
                         "摘过一次之后不该再报（幂等）")

    def test_a_working_pip_is_left_alone(self):
        seen = []
        with mock.patch.object(backend_fetch, "pip_works", lambda exe, timeout=120.0: (True, "pip 24.0")), \
                mock.patch.object(backend_fetch.subprocess, "run",
                                  lambda *a, **kw: seen.append(a) or mock.Mock(returncode=0)):
            ok, detail = backend_fetch.ensure_pip(self.exe)
        self.assertTrue(ok, detail)
        self.assertEqual(seen, [], "pip 好用就不该再跑任何东西")

    def test_a_broken_pip_is_rebuilt_offline_from_ensurepips_wheels(self):
        """**全离线**：用 `ensurepip._bundled` 里那两个 wheel 把 pip 装回来（不碰网络）。"""
        calls = {"n": 0}

        def _pip_works(exe, timeout=120.0):
            calls["n"] += 1
            return (calls["n"] > 1), ("pip 24.0" if calls["n"] > 1
                                      else "No module named 'pip._internal.models'")

        seen = {}

        def _run(argv, **kw):
            seen["argv"] = argv
            return mock.Mock(returncode=0, stdout="Successfully installed pip-24.0\n", stderr="")

        steps = []
        with mock.patch.object(backend_fetch, "pip_works", _pip_works), \
                mock.patch.object(backend_fetch.subprocess, "run", _run):
            ok, detail = backend_fetch.ensure_pip(self.exe, on_step=steps.append)
        self.assertTrue(ok, detail)
        self.assertEqual(seen["argv"][:2], [self.exe, "-c"])
        self.assertIn("ensurepip", seen["argv"][2])
        self.assertIn("--force-reinstall", seen["argv"][2],
                      "不带 force-reinstall 会去装那个残缺的 pip（实测会失败）")
        self.assertTrue(any("离线装回 pip" in s for s in steps), steps)

    def test_a_pip_that_cannot_be_rebuilt_fails_with_the_raw_text(self):
        with mock.patch.object(backend_fetch, "pip_works",
                               lambda exe, timeout=120.0: (False, "No module named 'pip._internal.models'")), \
                mock.patch.object(backend_fetch.subprocess, "run",
                                  lambda *a, **kw: mock.Mock(returncode=1, stdout="",
                                                             stderr="ERROR: externally managed")):
            ok, detail = backend_fetch.ensure_pip(self.exe)
        self.assertFalse(ok)
        self.assertIn("pip._internal.models", detail, "两段原文都要带出来")
        self.assertIn("externally managed", detail)

    def test_ensure_runtime_heals_pip_before_running_any_pip(self):
        """顺序：**先修 pip、再装依赖** —— 反过来的话每一步都必然失败。"""
        root = os.path.join(self.tmp, "backend")
        os.makedirs(os.path.join(root, "server"), exist_ok=True)
        with open(os.path.join(root, "server", "requirements.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("fastapi>=0.115\n")
        order = []
        abi = {"ok": True, "torch": "t", "torchaudio": "a", "error": "", "note": ""}
        with mock.patch.object(backend_setup, "backend_root", lambda: root), \
                mock.patch.object(backend_proc, "backend_root", lambda: root), \
                mock.patch.object(backend_proc, "python_exe", lambda: self.exe), \
                mock.patch.object(backend_fetch, "ensure_pip",
                                  lambda exe, on_step=None: (order.append("pip"), (True, "ok"))[1]), \
                mock.patch.object(backend_fetch, "_run_pip",
                                  lambda exe, args, label, on_step=None: (order.append("install"), (True, "ok"))[1]), \
                mock.patch.object(backend_env, "check_torch_abi",
                                  lambda exe, timeout=180.0: abi), \
                mock.patch.object(backend_env, "check_server_deps",
                                  lambda exe, timeout=120.0: {"ok": False, "error": "",
                                                              "output": ""}), \
                mock.patch.object(backend_fetch, "find_offline_zip", lambda: ""), \
                mock.patch.object(backend_fetch.subprocess, "run",
                                  lambda *a, **kw: mock.Mock(returncode=0, stdout="1.0\n",
                                                             stderr="")):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertTrue(ok, detail)
        self.assertIn("pip", order, "修 pip 那一步必须真的被调到")
        self.assertLess(order.index("pip"), order.index("install"),
                        "先修 pip 再装依赖，顺序反了就是必然失败：%s" % order)


if __name__ == "__main__":
    unittest.main()