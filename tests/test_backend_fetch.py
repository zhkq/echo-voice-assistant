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
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock

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
        ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertFalse(ok)
        self.assertIn("薄包还没解开", detail)

    def test_a_raising_pip_is_reported_not_propagated(self):
        def _boom(*a, **kw):
            raise OSError("管道断了")
        with mock.patch.object(backend_fetch.subprocess, "run", _boom):
            ok, detail = backend_fetch.ensure_runtime("cu126")
        self.assertFalse(ok)
        self.assertIn("管道断了", detail)


if __name__ == "__main__":
    unittest.main()
