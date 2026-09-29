# -*- coding: utf-8 -*-
"""扩展包（不走容器）的出包（`scripts/build_backend_portable.py`，批 5）。

这一档的**真机端到端还没验过**（干净 Windows + N 卡：解包 → 起 → 配对 → 就绪 → 真音频出文字）——
它要么需要先拍板 "随包的 Python/torch 从哪来"（实施方案 §7-1），要么需要一台干净的 N 卡机器。
所以用例只钉**脚本自己能保证的三件事**：

  1. **ABI 闸门**（`test_abi_check_rejects_cpu_torchaudio`）：随包运行时里 torch 与 torchaudio
     的 **CUDA 源标签不一致**时**出包就失败**，不把这个问题留到目标机
     （那时只表现为"每个 /v1/asr 都 503"，很难查到是 ABI 不符）；
  2. **没有运行时/仓库时响亮失败**（不许偷偷拿开发机的 venv 充数：venv 里是绝对路径 +
     本机的 CUDA 版本，换台机器就是 import 崩）；
  3. **包的结构与自检**：源码 + runtime + 脚本 + 模板 + manifest + 先读我，且自检能查出缺项。

用例里的"运行时"是**造的**（一个空目录 + 一个假解释器文件），不依赖本机 venv，也不会拷几 GB。
"""
import importlib.util
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

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "build_backend_portable.py")
_spec = importlib.util.spec_from_file_location("echo_build_portable", _PATH)
portable = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(portable)


def _make_runtime(root, *, python_rel=os.path.join("Scripts", "python.exe")):
    """造一个**假的**运行时目录（只要"解释器这个文件在"）。"""
    exe = os.path.join(root, python_rel)
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n")
    with open(os.path.join(root, "README-runtime.txt"), "w", encoding="utf-8") as fh:
        fh.write("假运行时\n")
    return exe


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-portable-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "dist")
        os.makedirs(self.out, exist_ok=True)
        self.runtime = os.path.join(self.tmp, "runtime")
        self.exe = _make_runtime(self.runtime)

    def _stage(self, **kw):
        # ABI 闸门默认打桩成"过"（真跑要一个真解释器；那条判据单独测）
        abi_ok = kw.pop("abi_ok", True)
        with mock.patch.object(portable, "abi_check",
                               lambda runtime_from: ({"ok": abi_ok, "torch": "2.14.0+cu126",
                                                      "torchaudio": "2.11.0+cu126"}
                                                     if abi_ok else {})):
            return portable.stage(self.out, variant="cu126",
                                  runtime_from=self.runtime, stamp="20260930-0300", **kw)


class PortablePackTests(_Case):
    def test_abi_check_rejects_cpu_torchaudio(self):
        """**方案 §5 的验收**：ABI 不符时出包就失败（CUDA 源标签不一致 = 目标机每个 /v1/asr 都 503）。"""
        from app import backend_env
        fake = {"ok": False, "torch": "2.14.0+cu126", "torchaudio": "2.11.0+cpu",
                "error": "torch 2.14.0+cu126 与 torchaudio 2.11.0+cpu 不是同一个 CUDA 源"}
        with mock.patch.object(backend_env, "check_torch_abi", lambda exe, timeout=180.0: fake):
            with self.assertRaises(portable.PackError) as ctx:
                portable.abi_check(self.runtime)
        self.assertIn("不是同一个 CUDA 源", str(ctx.exception))
        self.assertIn("标签", str(ctx.exception), "报错要说清判据是标签而不是版本号")

    def test_abi_check_accepts_matching_tags(self):
        from app import backend_env
        with mock.patch.object(backend_env, "check_torch_abi",
                               lambda exe, timeout=180.0: {"ok": True, "torch": "2.14.0+cu126",
                                                           "torchaudio": "2.11.0+cu126",
                                                           "error": ""}):
            got = portable.abi_check(self.runtime)
        self.assertTrue(got["ok"])

    def test_abi_check_needs_an_interpreter(self):
        empty = os.path.join(self.tmp, "empty-runtime")
        os.makedirs(empty, exist_ok=True)
        with self.assertRaises(portable.PackError) as ctx:
            portable.abi_check(empty)
        self.assertIn("没找到解释器", str(ctx.exception))

    def test_a_bad_abi_stops_before_copying_anything(self):
        """闸门要在**拷之前**（否则拷 4 GB 才发现不对）。"""
        with self.assertRaises(portable.PackError):
            self._stage(abi_ok=False)
        self.assertEqual(os.listdir(self.out), [], "出包失败时不该留下半个包")

    def test_it_refuses_without_a_runtime_or_a_wheelhouse(self):
        with self.assertRaises(portable.PackError) as ctx:
            portable.stage(self.out, variant="cu126", stamp="x")
        message = str(ctx.exception)
        self.assertIn("runtime-from", message)
        self.assertIn("venv", message, "要说明为什么不能拿开发机的 venv 充数")
        self.assertIn("①", message, "要给出路")

    def test_the_kit_has_source_runtime_scripts_and_a_manifest(self):
        kit_dir, zip_path, info = self._stage()
        for rel in ("app", "server", portable.YAML_TMPL, portable.READ_ME,
                    portable.MANIFEST,
                    os.path.join(portable.RUNTIME_DIR, "Scripts", "python.exe"),
                    os.path.join(portable.SCRIPTS_DIR, "install-windows.ps1"),
                    os.path.join(portable.SCRIPTS_DIR, "install-posix.sh")):
            with self.subTest(rel=rel):
                self.assertTrue(os.path.exists(os.path.join(kit_dir, rel)), rel)
        self.assertEqual(portable.verify(kit_dir), [])
        with open(os.path.join(kit_dir, portable.MANIFEST), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["package"], "backend-portable")
        self.assertEqual(manifest["variant"], "cu126")
        self.assertTrue(manifest["hasRuntime"])
        self.assertTrue(manifest["abi"]["ok"])
        self.assertGreater(manifest["count"], 10, manifest["count"])
        # zip 的条目要带**顶层目录前缀**（解包出来是一个文件夹，不是一堆散文件）
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        self.assertTrue(names, "zip 是空的")
        self.assertTrue(all(n.startswith(os.path.basename(kit_dir)) for n in names),
                        "zip 条目少了顶层目录前缀：%s" % names[:3])

    def test_the_server_yaml_template_is_loopback_only(self):
        """扩展包的配置模板同样**只绑回环** + 本机自配对（与容器档一套口径）。"""
        text = portable.render_server_yaml(port=8900, admin_port=8901, root="{ROOT}")
        self.assertIn('listen: "127.0.0.1:8900"', text)
        self.assertIn('admin_listen: "127.0.0.1:8901"', text)
        self.assertIn("local_pair: true", text)
        self.assertNotIn("0.0.0.0", text)
        # **不在这里写死 jwt_secret**（那会让所有人共用同一把密钥）
        self.assertIn('jwt_secret: ""', text)

    def test_verify_lists_what_is_missing(self):
        kit_dir, _zip, _info = self._stage()
        os.remove(os.path.join(kit_dir, portable.READ_ME))
        os.remove(os.path.join(kit_dir, portable.RUNTIME_DIR, "Scripts", "python.exe"))
        problems = portable.verify(kit_dir)
        self.assertTrue([p for p in problems if portable.READ_ME in p], problems)
        self.assertTrue([p for p in problems if "解释器" in p], problems)

    def test_the_readme_carries_the_unverified_disclosure(self):
        """说明书里必须**照实**写着这条路还没真机验过，以及人必须自己验的两步。"""
        kit_dir, _zip, _info = self._stage()
        with open(os.path.join(kit_dir, portable.READ_ME), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("还没验过", text)
        self.assertIn("另一台机器", text)


if __name__ == "__main__":
    unittest.main()
