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
        # 薄包已解开的样子：`server/requirements.txt` 在
        os.makedirs(os.path.join(self.root, "server"), exist_ok=True)
        with open(os.path.join(self.root, "server", "requirements.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("fastapi>=0.115\nuvicorn[standard]>=0.30\n")

    @property
    def root(self):
        return os.path.join(self.tmp, "backend")


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
    def _run_pip(self, calls, code=0, out="ok"):
        def _fake(exe, args, label, on_step=None):
            calls.append({"exe": exe, "args": list(args), "label": label})
            return (code == 0), ("%s 完成" % label if code == 0 else "pip 退出码 %d：\n%s" % (code, out))
        return _fake

    def test_torch_and_torchaudio_share_one_index_and_one_pip_call(self):
        """**纪律 1**：两者必须出现在**同一条** pip、**同一个** `--index-url` 里。"""
        calls = []
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
        """torch 那步失败就**停**（别接着装一堆没用的依赖），并把 pip 原文带出去。"""
        calls = []
        with mock.patch.object(backend_fetch, "_run_pip", self._run_pip(calls, code=1, out="boom")), \
                mock.patch.object(backend_env, "check_torch_abi",
                                  lambda exe, timeout=180.0: {"ok": False, "error": "不该走到这"}):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertFalse(ok)
        self.assertIn("pip 退出码 1", detail)
        self.assertEqual(len(calls), 1, "失败后不该继续装别的")

    def test_a_failing_abi_self_check_is_a_failure(self):
        """**纪律 2**：装完 ABIm 自检不过 = 失败（不许留到运行时表现为 503）。"""
        calls = []
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
        with mock.patch.object(backend_fetch.subprocess, "run", _boom):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertFalse(ok)
        self.assertIn("管道断了", detail)


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


if __name__ == "__main__":
    unittest.main()