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
        # 随包解释器也打桩（`ensure_pip` 要**跑真解释器**，而这里的 exe 是个文本文件）。
        # 它自己的行为（摘 PEP 668 标记 / 离线装回 pip）在 `PipPrepTests` 里单独测。
        pip_ok = kw.pop("pip_ok", True)
        from app import backend_fetch
        kwargs = {"variant": "cu126", "stamp": "20260930-0300"}
        if "python_from" not in kw:
            kwargs["runtime_from"] = self.runtime
        kwargs.update(kw)
        with mock.patch.object(portable, "abi_check",
                               lambda runtime_from: ({"ok": abi_ok, "torch": "2.14.0+cu126",
                                                      "torchaudio": "2.11.0+cu126"}
                                                     if abi_ok else {})), \
                mock.patch.object(backend_fetch, "ensure_pip",
                                  lambda exe, on_step=None: ((True, "pip 可用（桩）") if pip_ok
                                                             else (False, "externally managed，修不好"))):
            return portable.stage(self.out, **kwargs)


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

    def test_a_bad_runtime_in_a_thick_pack_never_produces_a_half_pack(self):
        """厚包那条路：ABI 不合就**一点东西都不留**（连目标目录都不该建）。"""
        with self.assertRaises(portable.PackError):
            self._stage(abi_ok=False)
        self.assertEqual(os.listdir(self.out), [], "出包失败时不该留下半个包")

    def test_the_default_is_a_thin_pack_with_a_source_list(self):
        """**默认出薄包**（用户 2026-09-30 拍板）：不要求随包运行时，几 GB 的 torch 不随包走 ——
        由 `sources.json` 说清"从哪下、下什么"（国内源；目标机点按钮时现装）。"""
        kit_dir, _zip, info = portable.stage(self.out, variant="cu126", stamp="thin-1")
        self.assertTrue(info["thin"])
        with open(os.path.join(kit_dir, portable.MANIFEST), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["mode"], "thin")
        self.assertFalse(manifest["hasRuntime"], "薄包不该带运行时")
        self.assertFalse(os.path.isdir(os.path.join(kit_dir, portable.RUNTIME_DIR)),
                         "薄包不该有 runtime/（除非只带解释器）")
        with open(os.path.join(kit_dir, portable.SOURCES), encoding="utf-8") as fh:
            sources = json.load(fh)
        self.assertIn("pytorch-wheels/cu126", sources["torchIndex"])
        self.assertTrue(any("tuna.tsinghua" in i for i in sources["pipIndexes"]))
        # torch 的索引**是一串**（2026-10-01）：一个源抖了就换下一个。
        # 契约是"清单里的第一个 == 主源"，而且与 `app/backend_fetch.py` **同源**（别在这里手抄）。
        self.assertEqual(sources["torchIndex"], sources["torchIndexes"][0])
        self.assertGreaterEqual(len(sources["torchIndexes"]), 2, sources["torchIndexes"])
        from app import backend_fetch
        self.assertEqual(sources["torchIndexes"], backend_fetch.torch_indexes("cu126"))
        self.assertEqual(portable.verify(kit_dir), [])

    def test_a_thin_pack_can_carry_just_the_interpreter(self):
        """薄包也可以**只带解释器**（几十 MB、与卡无关）：torch 仍然按国内源现装。"""
        kit_dir, _zip, info = self._stage(python_from=self.runtime)
        self.assertTrue(info["thin"])
        self.assertTrue(os.path.isfile(
            os.path.join(kit_dir, portable.RUNTIME_DIR, "Scripts", "python.exe")))
        with open(os.path.join(kit_dir, portable.MANIFEST), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertTrue(manifest["hasRuntime"])
        self.assertEqual(manifest["mode"], "thin")

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

    def test_the_template_can_bind_all_interfaces_when_asked(self):
        """`--listen` 能显式改成全网卡地址（2026-10-08）。

        为什么要有这条：原来模板把 `listen` **写死**成回环，而客户端那条路
        （`app/backend_setup.render_config()`）认设置 `capabilityBackendListen`
        —— 于是"用户在面板上把局域网打开"之后，**只要重装一次扩展包，
        安装脚本用模板重新生成 yaml，局域网就被关回去了**（同事实测）。

        出厂仍只绑回环（上面那条守门用例盯着）；这一条钉的是"显式要求时必须能改"。
        """
        text = portable.render_server_yaml(port=8900, admin_port=8901, root="{ROOT}",
                                           bind_host="0.0.0.0")
        self.assertIn('listen: "0.0.0.0:8900"', text)
        # 运维面**永远**只绑回环（改一处不改另一处 = 管理面对网段开着）
        self.assertIn('admin_listen: "127.0.0.1:8901"', text)

    def test_the_installer_never_overwrites_an_existing_server_yaml(self):
        """安装脚本**不许覆盖已有的 server.yaml**（否则重装就把用户的配置抹了）。

        这是"局域网访问丢了"的另一半：即使模板对了，只要安装脚本每次都覆盖，
        用户的 listen / 模型路径照样丢。判据落在**两个平台**的安装脚本里。
        """
        for name, body in (("install-windows.ps1", portable.INSTALL_PS1),
                           ("install-posix.sh", portable.INSTALL_SH)):
            self.assertIn("server.yaml", body, name)
            has_branch = ("Test-Path $yaml" in body) or ("-f \"$root/server.yaml\"" in body)
            self.assertTrue(has_branch, "%s 没有『已存在就保留』的分支" % name)
            self.assertIn("保留不动", body, "%s 保留了但没有如实告知用户" % name)

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


class PipPrepTests(_Case):
    """**随包解释器必须"能装东西"**（2026-10-01 真机实测的两个缺陷，都出在出包这一步）。

    薄包的 `runtime/` 是搬过来的 uv 托管 CPython：① 带着 `Lib/EXTERNALLY-MANAGED`（PEP 668）
    → 目标机上 pip **一律拒绝安装**；② 它的 pip 还可能是**残缺**的。应用侧会兜一遍
    （`backend_fetch.ensure_pip`），但出包时**就该是干净的** —— 别让每台机器修同一个缺陷。
    """

    def test_the_build_strips_the_pep668_marker(self):
        marker = os.path.join(self.runtime, "Lib", "EXTERNALLY-MANAGED")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("This environment is externally managed\n")
        kit_dir, _zip, info = self._stage()
        shipped = os.path.join(kit_dir, portable.RUNTIME_DIR, "Lib", "EXTERNALLY-MANAGED")
        self.assertFalse(os.path.exists(shipped),
                         "带着这个标记，目标机上 pip 会拒绝安装（看着像权限问题）")
        self.assertGreaterEqual(info["runtimePrepared"]["markersRemoved"], 1)
        with open(os.path.join(kit_dir, portable.MANIFEST), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertFalse([f for f in manifest["files"] if "EXTERNALLY-MANAGED" in f],
                         "标记已删，就不该还留在清单里")
        self.assertEqual(portable.verify(kit_dir), [])

    def test_verify_flags_a_marker_that_still_ships(self):
        """护栏：哪天有人在拷贝之后又把标记放回去（或换了实现），自检要挡住。"""
        kit_dir, _zip, _info = self._stage()
        marker = os.path.join(kit_dir, portable.RUNTIME_DIR, "Lib", "EXTERNALLY-MANAGED")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        problems = portable.verify(kit_dir)
        self.assertTrue([p for p in problems if "EXTERNALLY-MANAGED" in p], problems)

    def test_a_pip_that_cannot_be_fixed_stops_the_pack(self):
        """随包运行时**装不了依赖**的包不该出（那种包到目标机上就是"点一下没反应"）。"""
        with self.assertRaises(portable.PackError) as ctx:
            self._stage(pip_ok=False)
        self.assertIn("pip", str(ctx.exception))
        leftovers = os.listdir(self.out)
        self.assertTrue(all(not d.startswith(portable.PACKAGE_PREFIX) for d in leftovers),
                        "出包失败时不该留下半个包：%s" % leftovers)


class CopyFilterTests(unittest.TestCase):
    """拷运行时时**只许**剪缓存/版本库元数据（2026-10-01 真机事故的回归用例）。

    事故：`_copy_tree()` 曾经套用"仓库那棵树"的排除表（里面有裸的 `data` / `models` / `tests` / `docs`），
    而它是**按目录名在任意深度剪**的 —— 于是 `site-packages/torch/utils/data/`、
    `transformers/models/`、`funasr/models/` 被整目录剪掉。当时所有出包自检都过（它们只看 ABI 元数据），
    装到目标机上才炸，而且**伪装成显卡问题**：

        ImportError: cannot import name 'data' from partially initialized module 'torch.utils'
        → 服务端报"模型要求 GPU，但 torch 看不到 CUDA"

    判据：**第三方包里叫 `data`/`models`/`tests`/`docs`/`logs`/`dist` 的目录是真代码，必须留下**；
    `__pycache__` / `.git` 才是任何深度都不带的。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-pack-copy-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.src = os.path.join(self.tmp, "src")
        self.dst = os.path.join(self.tmp, "dst")
        for rel in ("Lib/site-packages/torch/utils/data/deep/__init__.py",
                    "Lib/site-packages/transformers/models/bert/modeling_bert.py",
                    "Lib/site-packages/funasr/models/asr/model.py",
                    "Lib/site-packages/sklearn/datasets/data/iris.csv",
                    "Lib/site-packages/somedep/tests/test_x.py",
                    "Lib/site-packages/somedep/docs/index.md",
                    "Lib/site-packages/somedep/logs/run.log",
                    "Lib/site-packages/somedep/dist/build.json",
                    "Lib/site-packages/somedep/__pycache__/cached.pyc",
                    "Lib/site-packages/somedep/keep.py",
                    "python.exe"):
            path = os.path.join(self.src, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x")

    def _copied(self):
        portable._copy_tree(self.src, self.dst, "runtime")
        got = set()
        for base, _dirs, files in os.walk(self.dst):
            for fn in files:
                got.add(os.path.relpath(os.path.join(base, fn), self.dst).replace("\\", "/"))
        return got

    def test_nested_package_dirs_that_share_a_repo_dir_name_are_kept(self):
        got = self._copied()
        for must in ("Lib/site-packages/torch/utils/data/deep/__init__.py",
                     "Lib/site-packages/transformers/models/bert/modeling_bert.py",
                     "Lib/site-packages/funasr/models/asr/model.py",
                     "Lib/site-packages/sklearn/datasets/data/iris.csv",
                     "Lib/site-packages/somedep/tests/test_x.py",
                     "Lib/site-packages/somedep/docs/index.md",
                     "Lib/site-packages/somedep/logs/run.log",
                     "Lib/site-packages/somedep/dist/build.json"):
            self.assertIn(must, got, "这条被剪掉了 —— 就是那次残包事故的形状：%s" % must)

    def test_caches_are_still_dropped(self):
        got = self._copied()
        self.assertNotIn("Lib/site-packages/somedep/__pycache__/cached.pyc", got)
        self.assertIn("Lib/site-packages/somedep/keep.py", got)

    def test_the_two_tables_are_deliberately_different(self):
        """源码树那张表可以按名字剪（`app/data` 是数据不是代码）；运行时那张**不行**。"""
        self.assertIn("data", portable.EXCLUDE_DIRS_SOURCE)
        for name in ("data", "models", "tests", "docs", "logs", "dist"):
            self.assertNotIn(name, portable.EXCLUDE_DIRS_TREE,
                             "运行时那张表里出现 %r 就会再剪出残包" % name)


class ImportCheckTests(unittest.TestCase):
    """出包自检必须**从打好的包里真 import**（不是只看元数据/文件数）。

    真跑子进程、真 import：解释器用 `sys.executable`（跑用例的那个 venv，**真的能用**），
    包目录通过探针里的 `sys.path.insert` 指到伪造的 site-packages —— 这样"import 得动/不动"
    是真实发生的，不靠打桩假装。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-pack-import-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.pack = os.path.join(self.tmp, "pack")
        self.sp = os.path.join(self.pack, "runtime", "Lib", "site-packages")
        os.makedirs(self.sp, exist_ok=True)
        self._find = mock.patch.object(portable, "find_interpreter",
                                       lambda _root: sys.executable)
        self._find.start()
        self.addCleanup(self._find.stop)

    def _probe(self, name):
        code = "import sys; sys.path.insert(0, r'%s'); import %s" % (self.sp, name)
        return ((name, code),)

    def test_a_pack_that_ships_a_broken_package_fails_loudly(self):
        """包里有这个包、却 import 不动 → 必须抛（那次残包本该在这里被拦住）。"""
        pkg = os.path.join(self.sp, "brokenpkg")
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write("raise ImportError(\"cannot import name 'data' from 'torch.utils'\")\n")
        with mock.patch.object(portable, "IMPORT_PROBES", self._probe("brokenpkg")):
            with self.assertRaises(portable.PackError) as ctx:
                portable.import_check(self.pack)
        text = str(ctx.exception)
        self.assertIn("brokenpkg", text)
        self.assertIn("残包", text)
        self.assertIn("EXCLUDE_DIRS_TREE", text, "报错要指到那次事故的根因上")

    def test_a_pack_that_does_not_ship_a_package_does_not_check_it(self):
        """薄包只有解释器：**没带的包一个都不查**（不然薄包永远出不来）。"""
        with mock.patch.object(portable, "IMPORT_PROBES",
                               (("torch", "import torch"), ("funasr", "import funasr"))):
            self.assertEqual(portable.import_check(self.pack), [])

    def test_a_healthy_package_is_reported_as_checked(self):
        pkg = os.path.join(self.sp, "okpkg")
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write("VALUE = 1\n")
        with mock.patch.object(portable, "IMPORT_PROBES", self._probe("okpkg")):
            self.assertEqual(portable.import_check(self.pack), ["okpkg"])

    def test_the_real_probe_list_covers_the_packages_that_were_mangled(self):
        """真表里必须盯着这次被剪坏的那几个（少一个，同类残包就能再溜过去）。"""
        names = [pkg for pkg, _code in portable.IMPORT_PROBES]
        for pkg in ("torch", "torchaudio", "transformers", "funasr", "fastapi"):
            self.assertIn(pkg, names)
        torch_code = dict(portable.IMPORT_PROBES)["torch"]
        self.assertIn("torch.utils.data", torch_code, "`torch.utils.data` 就是被剪掉的那个")
        transformers_code = dict(portable.IMPORT_PROBES)["transformers"]
        self.assertIn("transformers.models", transformers_code)


if __name__ == "__main__":
    unittest.main()
