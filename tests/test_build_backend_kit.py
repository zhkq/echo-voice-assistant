# -*- coding: utf-8 -*-
"""后端交付目录 + 打包脚本（`scripts/build_backend_kit.py`）的护栏。

为什么要单独一份
----------------
后端要发给**别人的显卡环境**，而两类卡的镜像不一样（torch 从 cu126 还是 cu118 装）。
这份护栏钉住的是"分流本身"与"文档里的关键结论"：

  1. 两个变体目录都在，各自的四份模板都在（`先读我.md` / `compose.yaml` / `.env.example` /
     `server.yaml`）—— 少一个就别出包；
  2. 两份 `compose.yaml` 的**端口语义**：8900 发布到所有网卡、8901 **只**发布到宿主回环、
     `ECHO_ADMIN_LISTEN=0.0.0.0:8901`、GPU 直通段在位（这几条错一条，同事那边要么连不上
     能力面、要么管理面进不去、要么容器看不到 GPU）；
  3. 两份 `先读我.md` 必须说到：按算力选哪个目录、`nvidia-container-toolkit`、模型布局、
     `--new-admin`、`ssh -L`、以及**Docker Hub 被墙时"显式前缀直拉再 tag"那个拉法**；
  4. **本轮的两条关键结论不许被后人删掉**：
     * cu118（`sm_<7.5`）**不能跑 qwen3asr**（镜像里没有 qwen-asr，且没有 bf16）；
     * cu126 里 **Turing（`compute_cap=7.5`）没有 bf16**，必须明确告诉用户换成 SenseVoice
       （给出 `impl: sensevoice` 的替换片段）—— Turing 走的就是这个包，不写就是把人家坑在
       "qwen3asr 一路 model_failed"里；
  5. 打包脚本产出的 zip：**每条目都带 `<包名>/` 顶层前缀**（少了它解包散成一堆文件，
     而且不报错）、**不含 `data/` 与 `models/`**、含 `SHA256SUMS.txt`；
  6. `--check` 的三个退出码（一致 0 / 过期 2 / 还没出过包 3）。

关于 `tests/test_build_kit.py` 的白名单那条：`delivery/` **不在**主包白名单里
（`build-package.ps1` 的 `$dirs`），所以新增这两个交付目录不会被塞进主包 ——
本文件里那一条把它变成可执行断言，而不是靠记性。
"""
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = ROOT / "scripts"
DELIVERY = ROOT / "delivery"
VARIANTS = {"cu126": DELIVERY / "backend-cu126", "cu118": DELIVERY / "backend-cu118"}
TEMPLATES = ("先读我.md", "compose.yaml", ".env.example", "server.yaml")
STAMP = "20260928-0100"


def _load_backend_kit():
    """把 `scripts/build_backend_kit.py` 当模块加载（它不在包路径里）。"""
    path = SCRIPTS / "build_backend_kit.py"
    spec = importlib.util.spec_from_file_location("build_backend_kit_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backend = _load_backend_kit()


def compose_of(key: str) -> dict:
    import yaml
    text = (VARIANTS[key] / "compose.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def backend_service(doc: dict) -> dict:
    svc = (doc.get("services") or {}).get("backend")
    assert isinstance(svc, dict), "compose.yaml 里没有 services.backend"
    return svc


def says_cannot(text: str, keyword: str) -> bool:
    """`keyword` 附近（±60 字符）有没有否定词 —— 用来断言"文档说了它跑不了"。

    为什么不用一条固定正则：同一句话说在前在后都算数（"不能跑 qwen3asr" 与
    "qwen3asr 跑不了"），钉字面量只会让后人改一句话就红。
    """
    negation = ("不能跑", "跑不了", "不能", "不建议", "不可用", "跑不通")
    for match in re.finditer(re.escape(keyword), text):
        window = text[max(0, match.start() - 60): match.end() + 60]
        if any(word in window for word in negation):
            return True
    return False


class VariantDirectoriesExist(unittest.TestCase):
    """两个变体目录都要在，而且四份模板齐全（模板在 git 里，不从 dist 捡）。"""

    def test_both_variant_dirs_exist(self):
        for key, path in VARIANTS.items():
            self.assertTrue(path.is_dir(), f"缺交付目录 {path}")

    def test_each_variant_has_the_four_templates(self):
        for key, path in VARIANTS.items():
            for name in TEMPLATES:
                target = path / name
                self.assertTrue(target.is_file(), f"{key} 缺 {name} —— 包组不出来")
                self.assertGreater(target.stat().st_size, 200, f"{key}/{name} 太小，像是空的")

    def test_readmes_are_utf8_chinese(self):
        """交付说明是给非开发者看的中文 —— 写成 ASCII 就是在敷衍（也真的发生过）。"""
        for key in VARIANTS:
            raw = (VARIANTS[key] / "先读我.md").read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{key} 的说明带了 BOM")
            text = raw.decode("utf-8")
            self.assertGreater(sum(1 for ch in text if ord(ch) > 0x2E80), 500,
                               f"{key}/先读我.md 里的中文太少，像是模板没写")

    def test_no_env_or_secret_files_in_the_delivery_dirs(self):
        """交付目录里只能有 `.env.example`：真 `.env` / 证书 / 私钥一个都不许有。"""
        for key, path in VARIANTS.items():
            for item in path.rglob("*"):
                if not item.is_file():
                    continue
                low = item.name.lower()
                self.assertNotEqual(low, ".env", f"{key} 里混进了真 .env")
                self.assertFalse(low.endswith((".key", ".pem", ".crt", ".p12")),
                                 f"{key} 里有密钥/证书：{item.name}")


class ComposeFiles(unittest.TestCase):
    """两份 compose 的硬事实：端口语义、管理面监听、GPU 直通、只读根。"""

    def test_both_parse_as_yaml(self):
        import yaml
        for key, path in VARIANTS.items():
            with self.subTest(variant=key):
                doc = yaml.safe_load((path / "compose.yaml").read_text(encoding="utf-8"))
                self.assertIsInstance(doc, dict)
                backend_service(doc)

    def test_capability_port_is_published_on_all_interfaces(self):
        for key in VARIANTS:
            ports = [str(p) for p in backend_service(compose_of(key)).get("ports") or []]
            self.assertTrue(any(p.startswith("8900:8900") for p in ports),
                            f"{key} 的 8900（能力面）没有发布到所有网卡：{ports}")

    def test_admin_port_is_published_to_loopback_only(self):
        """管理面只发布到宿主回环 —— 这条塌了，发授权/看客户端就对整个网段敞开。"""
        for key in VARIANTS:
            ports = [str(p) for p in backend_service(compose_of(key)).get("ports") or []]
            self.assertTrue(any(p.startswith("127.0.0.1:8901:8901") for p in ports),
                            f"{key} 的 8901 没有发布到宿主回环：{ports}")
            self.assertFalse([p for p in ports
                              if p.startswith("8901:") or p.startswith("0.0.0.0:8901")],
                             f"{key} 把管理面发布到了所有网卡：{ports}")

    def test_admin_listen_is_the_container_wildcard(self):
        """容器里必须绑通配 —— 绑回环的话宿主与 `ssh -L` 都进不来（"配了却进不去"）。"""
        for key in VARIANTS:
            env = backend_service(compose_of(key)).get("environment") or {}
            self.assertEqual(str(env.get("ECHO_ADMIN_LISTEN") or ""), "0.0.0.0:8901",
                             f"{key} 的 ECHO_ADMIN_LISTEN 不对")

    def test_gpu_passthrough_section_is_present(self):
        for key in VARIANTS:
            svc = backend_service(compose_of(key))
            devices = (((svc.get("deploy") or {}).get("resources") or {})
                       .get("reservations") or {}).get("devices")
            self.assertTrue(devices, f"{key} 没有 GPU 直通段")
            self.assertEqual(str(devices[0].get("driver")), "nvidia", f"{key} 的 driver 不是 nvidia")
            self.assertIn("gpu", [str(c) for c in devices[0].get("capabilities") or []])

    def test_root_is_read_only_and_the_two_volumes_are_opposite(self):
        for key in VARIANTS:
            svc = backend_service(compose_of(key))
            self.assertIs(svc.get("read_only"), True, f"{key} 的根文件系统不是 read_only")
            mounts = [str(v) for v in svc.get("volumes") or []]
            self.assertTrue(any("/opt/echo/models" in m and m.strip().endswith(":ro")
                                for m in mounts),
                            f"{key} 的模型卷不是只读挂载：{mounts}")
            self.assertTrue(any("/var/echo/state" in m for m in mounts),
                            f"{key} 缺 state 卷（鉴权库）")
            self.assertTrue(any("/var/echo/tmp" in m for m in mounts),
                            f"{key} 缺 tmp 卷")
            # state 绝不能是 tmpfs（丢了所有客户端要重新配对）
            self.assertNotIn("/var/echo/state", str(svc.get("tmpfs") or []))

    def test_image_tag_names_the_variant(self):
        for key in VARIANTS:
            image = str(backend_service(compose_of(key)).get("image") or "")
            self.assertIn(key, image, f"{key} 的镜像 tag 里没有变体名：{image}")
            self.assertIn("echo-backend", image)

    def test_build_args_carry_the_right_torch_source(self):
        """torch 源是**构建期**参数，装错就等于"构建成功但看不到这块卡"。"""
        for key in VARIANTS:
            args = backend_service(compose_of(key)).get("build", {}).get("args") or {}
            self.assertEqual(str(args.get("ECHO_EXTRA")), "1",
                             f"{key} 没打开 ECHO_EXTRA（那样模型一律 model_failed）")
            self.assertIn(key, str(args.get("ECHO_TORCH_INDEX") or ""),
                          f"{key} 的 ECHO_TORCH_INDEX 不是 {key} 那个源")
        # 老卡还必须**钉住**版本（cu118 源上带 Pascal 的最后一版就是 2.7.1）
        args118 = backend_service(compose_of("cu118")).get("build", {}).get("args") or {}
        self.assertEqual(str(args118.get("ECHO_TORCH_VERSION") or ""), "2.7.1",
                         "老卡没钉 torch 版本 —— 依赖可能把它顶成不含 Pascal 的构建")

    def test_the_config_file_with_specs_is_mounted_with_config_flag(self):
        """`models.specs` 没有环境变量：不挂配置的话容器会按出厂清单去加载 qwen3asr/pyannote。"""
        for key in VARIANTS:
            svc = backend_service(compose_of(key))
            mounts = [str(v) for v in svc.get("volumes") or []]
            self.assertTrue(any("server.yaml:/etc/echo/server.yaml:ro" in m for m in mounts),
                            f"{key} 没有把 server.yaml 挂进容器：{mounts}")
            command = [str(c) for c in svc.get("command") or []]
            self.assertIn("--config", command, f"{key} 的 command 里没有 --config")


class CapabilitySpecs(unittest.TestCase):
    """`server.yaml` 的两份 specs 必须与"这块卡真能跑什么"一致（不许宣告跑不了的槽）。"""

    def _specs(self, key: str) -> list:
        import yaml
        doc = yaml.safe_load((VARIANTS[key] / "server.yaml").read_text(encoding="utf-8")) or {}
        return ((doc.get("models") or {}).get("specs")) or []

    def test_cu126_declares_asr_diarize_and_speaker_embed(self):
        specs = {s.get("slot"): s for s in self._specs("cu126")}
        self.assertIn("asr.long", specs)
        self.assertIn("diarize.turns", specs)
        self.assertIn("speaker.embed", specs)
        self.assertEqual(specs["asr.long"].get("impl"), "qwen3asr")
        self.assertIn("asr.timestamps", specs["asr.long"].get("supports") or [],
                      "cu126 的 asr-long 必须宣告句级时间戳（对齐器在位才是真的）")
        # 分离与声纹必须**同一个** vectorSpaceId，否则向量没法互相比对
        self.assertEqual(specs["diarize.turns"].get("vectorSpaceId"),
                         specs["speaker.embed"].get("vectorSpaceId"))

    def test_cu118_declares_only_sensevoice(self):
        """老卡上 pyannote 装不上（torch 上限 2.7.1）：宣告那两档就是仓库明令禁止的假话。"""
        specs = self._specs("cu118")
        slots = [s.get("slot") for s in specs]
        self.assertEqual(slots, ["asr.long"], f"cu118 的 specs 只该有 asr.long：{slots}")
        self.assertEqual(specs[0].get("impl"), "sensevoice")
        self.assertNotIn("asr.timestamps", specs[0].get("supports") or [],
                         "SenseVoice 不给句级时间戳，不许宣告 asr.timestamps")

    def test_cu118_config_does_not_mention_the_unavailable_slots(self):
        text = (VARIANTS["cu118"] / "server.yaml").read_text(encoding="utf-8")
        body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        for needle in ("diarize", "speaker-embed", "pyannote-embed"):
            self.assertNotIn(needle, body,
                             f"cu118 的生效配置里出现了 {needle}（只能写在注释里解释为什么没有）")


class ReadmesSayTheRightThings(unittest.TestCase):
    """两份说明都要覆盖"怎么选、怎么装、怎么摆模型、怎么运维"，而且结论不许被删。"""

    def _text(self, key: str) -> str:
        return (VARIANTS[key] / "先读我.md").read_text(encoding="utf-8")

    def test_both_pick_the_directory_by_compute_capability(self):
        for key, other in (("cu126", "backend-cu118"), ("cu118", "backend-cu126")):
            text = self._text(key)
            self.assertIn("compute_cap", text, f"{key} 没告诉同事怎么查算力")
            self.assertIn("nvidia-smi", text)
            self.assertIn(other, text, f"{key} 没说明另一个目录什么时候用（{other}）")

    def test_both_name_the_container_toolkit_and_disk_requirement(self):
        for key in VARIANTS:
            text = self._text(key)
            self.assertIn("nvidia-container-toolkit", text,
                          f"{key} 没写容器 GPU 直通的前置（缺了 compose up 直接报错）")
            self.assertIn("nvidia-ctk runtime configure", text)
            self.assertIn("60 GB", text, f"{key} 没写磁盘要求")

    def test_both_describe_the_model_layout(self):
        for key in VARIANTS:
            text = self._text(key)
            self.assertIn("/opt/echo/models", text, f"{key} 没写容器里的模型根")
            self.assertIn("只读", text, f"{key} 没说模型卷是只读挂载")
        self.assertIn("models--Qwen--Qwen3-ASR-0.6B", self._text("cu126"),
                      "cu126 没写 qwen3asr 在容器里的落点")
        self.assertIn("models--Qwen--Qwen3-ForcedAligner-0.6B", self._text("cu126"),
                      "cu126 没写强制对齐器的落点（缺了 asr.timestamps 就是假话）")
        self.assertIn("sensevoice", self._text("cu118"),
                      "cu118 没写 SenseVoice 的落点 —— 那是它唯一的转写引擎")

    def test_both_show_the_admin_cli_and_the_ssh_tunnel(self):
        for key in VARIANTS:
            text = self._text(key)
            self.assertIn("--new-admin", text, f"{key} 没写怎么建管理员（只能 CLI）")
            self.assertIn("--list-admins", text)
            self.assertIn("ssh -L 8901:127.0.0.1:8901", text,
                          f"{key} 没写远程运维的端口转发姿势")

    def test_both_show_how_to_pull_around_a_blocked_docker_hub(self):
        """国内拉 Docker Hub 会挂住；可靠做法是**显式前缀直拉再 tag**（不是只加 mirror）。"""
        for key in VARIANTS:
            text = self._text(key)
            self.assertIn("docker.1panel.live/library/", text,
                          f"{key} 没给被墙时的显式前缀拉法")
            self.assertIn("docker tag", text, f"{key} 没写直拉之后要 tag 回本名")
            self.assertIn("registry-mirrors", text,
                          f"{key} 没说明只加 mirror 有时反而挂住这个坑")

    def test_both_list_the_troubleshooting_entries(self):
        needles = ("nvidia-container-toolkit", "auth_misconfigured", "model_failed",
                   "docker.1panel.live", "ssh -L")
        for key in VARIANTS:
            text = self._text(key)
            for needle in needles:
                self.assertIn(needle, text, f"{key} 的排错一节缺 {needle}")

    def test_cu118_says_qwen3asr_cannot_run(self):
        """本轮的关键结论之一：老卡上不是"不建议"，是**跑不了**（镜像里没有 qwen-asr，也没有 bf16）。"""
        text = self._text("cu118")
        self.assertIn("qwen3asr", text)
        self.assertTrue(says_cannot(text, "qwen3asr"),
                        "cu118 没写明 qwen3asr 跑不了")
        self.assertIn("stt.py:342", text, "没指出 dtype 写死在哪一行")
        self.assertIn("pyannote", text, "没解释为什么没有分离/声纹")

    def test_cu118_tells_the_client_degrades_honestly(self):
        """客户端会显示「说话人分离未执行：<真原因>」并照常出文字 —— 那是设计行为，不是故障。"""
        text = self._text("cu118")
        self.assertIn("说话人分离未执行", text)
        self.assertIn("诚实", text)

    def test_cu126_warns_that_turing_has_no_bf16(self):
        text = self._text("cu126")
        self.assertIn("Turing", text)
        self.assertIn("没有 bf16", text, "cu126 没写明 Turing 没有 bf16")
        self.assertIn("sm_80", text, "没写 bf16 的要求（sm_80+）")

    def test_cu126_gives_the_turing_switch_to_sensevoice(self):
        """Turing 用户走的就是这个包：必须给出可直接替换的 `impl: sensevoice` 片段。"""
        text = self._text("cu126")
        self.assertIn("impl: sensevoice", text,
                      "cu126 没给把 asr-long 换成 SenseVoice 的替换片段")
        self.assertIn("7.5", text)
        # 片段与 server.yaml 里那段注释要对得上（两处各写一份，漂了就坑人）
        specs = (VARIANTS["cu126"] / "server.yaml").read_text(encoding="utf-8")
        self.assertIn("impl: sensevoice", specs,
                      "server.yaml 里没留 Turing 的替换片段（说明文件指向了它）")

    def test_cu126_capability_table_covers_turing_and_sm80(self):
        text = self._text("cu126")
        for needle in ("sm_80", "8.0", "分离", "声纹"):
            self.assertIn(needle, text, f"cu126 的能力边界表缺 {needle}")


class PackerProducesTheKits(unittest.TestCase):
    """真跑一次打包（临时目录），断言 zip 的结构与内容。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="echobackendkit-")
        cls.out = Path(cls._tmp.name)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = backend.main(["--out", str(cls.out), "--stamp", STAMP])
        cls.build_log = buf.getvalue()
        if code != 0:
            raise AssertionError(f"打包失败（exit={code}）：\n{cls.build_log}")
        cls.zips = {key: cls.out / f"ECHO-backend-kit-{key}-{STAMP}.zip" for key in VARIANTS}
        for path in cls.zips.values():
            if not path.is_file():
                raise AssertionError(f"没产出 {path}\n{cls.build_log}")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _names(self, key: str) -> list:
        with zipfile.ZipFile(self.zips[key]) as zf:
            return zf.namelist()

    def test_every_zip_entry_carries_the_top_level_prefix(self):
        """少了 `<包名>/` 前缀，解包出来是一堆散文件 —— 这个坑不报错，只能靠断言。"""
        for key in VARIANTS:
            prefix = f"ECHO-backend-kit-{key}-{STAMP}/"
            names = self._names(key)
            self.assertTrue(names, f"{key} 的 zip 是空的")
            bad = [n for n in names if not n.startswith(prefix)]
            self.assertEqual(bad, [], f"{key} 有没带前缀的条目：{bad[:5]}")

    def test_zip_has_no_data_models_or_caches(self):
        """安全与体积：数据、权重、缓存、.git、密钥都不许进包。"""
        for key in VARIANTS:
            for name in self._names(key):
                rel = name.split("/", 1)[1] if "/" in name else name
                parts = [p for p in rel.strip("/").split("/") if p]
                with self.subTest(variant=key, entry=name):
                    self.assertNotIn("__pycache__", parts)
                    self.assertNotIn(".git", parts)
                    self.assertNotIn("data", parts)
                    self.assertNotIn("models", parts)
                    self.assertNotIn("dist", parts)
                    low = (parts[-1] if parts else "").lower()
                    self.assertFalse(low.endswith((".pyc", ".key", ".pem", ".crt", ".db", ".log")),
                                     f"{key} 的 zip 里有不该带的文件：{name}")
                    self.assertNotEqual(low, ".env")

    def test_zip_contains_the_readme_sums_and_info(self):
        for key in VARIANTS:
            prefix = f"ECHO-backend-kit-{key}-{STAMP}/"
            names = self._names(key)
            for need in ("先读我.md", "compose.yaml", ".env.example", "server.yaml",
                         "SHA256SUMS.txt", "BUILD-INFO.txt",
                         "server/Dockerfile", "server/requirements.txt",
                         "app/audio/stt.py",
                         "scripts/prepare-backend.sh", "scripts/smoke-echo-backend.py"):
                self.assertIn(prefix + need, names, f"{key} 的 zip 里缺 {need}")

    def test_sha256sums_lists_every_packed_source_file(self):
        for key in VARIANTS:
            prefix = f"ECHO-backend-kit-{key}-{STAMP}/"
            with zipfile.ZipFile(self.zips[key]) as zf:
                sums = zf.read(prefix + "SHA256SUMS.txt").decode("utf-8")
            entries = [ln.split("  ", 1)[1] for ln in sums.splitlines() if "  " in ln]
            self.assertGreater(len(entries), 50, f"{key} 的 SHA256SUMS 太短")
            for need in ("compose.yaml", "server/main.py", "app/audio/stt.py"):
                self.assertIn(need, entries, f"{key} 的 SHA256SUMS 里没有 {need}")

    def test_each_zip_carries_its_own_variant_template(self):
        """两个包不能长成同一个 —— compose 里的镜像 tag 与 specs 必须各是各的。"""
        for key, other in (("cu126", "cu118"), ("cu118", "cu126")):
            prefix = f"ECHO-backend-kit-{key}-{STAMP}/"
            with zipfile.ZipFile(self.zips[key]) as zf:
                compose = zf.read(prefix + "compose.yaml").decode("utf-8")
                specs = zf.read(prefix + "server.yaml").decode("utf-8")
                readme = zf.read(prefix + "先读我.md").decode("utf-8")
            self.assertIn(f"echo-backend:0.1.0-{key}", compose)
            self.assertNotIn(f"echo-backend:0.1.0-{other}", compose)
            # 包里那份说明就是模板本身（不是从 dist 里捡的旧货）
            self.assertEqual(readme, (VARIANTS[key] / "先读我.md").read_text(encoding="utf-8"))
            if key == "cu118":
                self.assertIn("impl: sensevoice", specs)
                self.assertNotIn("impl: qwen3asr", specs)
            else:
                self.assertIn("impl: qwen3asr", specs)

    def test_build_info_records_variant_time_and_git(self):
        for key in VARIANTS:
            prefix = f"ECHO-backend-kit-{key}-{STAMP}/"
            with zipfile.ZipFile(self.zips[key]) as zf:
                info = zf.read(prefix + "BUILD-INFO.txt").decode("utf-8")
            self.assertIn(f"variant      : {key}", info)
            self.assertIn(f"stamp        : {STAMP}", info)
            self.assertRegex(info, r"(?m)^git\s+: \S+", "BUILD-INFO 里没有 git 短哈希")

    def test_verify_rejected_nothing(self):
        self.assertIn("[ok]", self.build_log)
        self.assertNotIn("[FAIL]", self.build_log)


class CheckModeExitCodes(unittest.TestCase):
    """`--check` 的语义与 `build_kit.py` 一致：0 一致 / 2 过期 / 3 还没出过包。"""

    def _run(self, argv: list) -> int:
        buf = io.StringIO()
        with redirect_stdout(buf):
            return backend.main(argv)

    def test_current_then_absent(self):
        with tempfile.TemporaryDirectory(prefix="echobkout-") as tmp:
            self.assertEqual(self._run(["--out", tmp, "--stamp", STAMP]), 0)
            self.assertEqual(self._run(["--check", "--out", tmp]), backend.EXIT_OK)
        with tempfile.TemporaryDirectory(prefix="echobkempty-") as tmp:
            self.assertEqual(self._run(["--check", "--out", tmp]), backend.EXIT_ABSENT)

    def test_stale_after_a_packed_source_file_changes(self):
        """真改一个源文件（在沙箱里）→ `--check` 必须报过期，而不是"看着挺新"。"""
        with tempfile.TemporaryDirectory(prefix="echobksandbox-") as tmpdir:
            sandbox = Path(tmpdir)
            self._make_sandbox(sandbox)
            saved = (backend.ROOT, backend.SCRIPTS, backend.DELIVERY)
            backend.ROOT, backend.SCRIPTS, backend.DELIVERY = (
                sandbox, sandbox / "scripts", sandbox / "delivery")
            try:
                out = sandbox / "dist"
                self.assertEqual(self._run(["--out", str(out), "--only", "cu126",
                                            "--stamp", STAMP]), 0)
                self.assertEqual(self._run(["--check", "--out", str(out),
                                            "--only", "cu126"]), backend.EXIT_OK)
                (sandbox / "server" / "main.py").write_text("# 改过了\n", encoding="utf-8")
                self.assertEqual(self._run(["--check", "--out", str(out),
                                            "--only", "cu126"]), backend.EXIT_STALE)
            finally:
                backend.ROOT, backend.SCRIPTS, backend.DELIVERY = saved

    @staticmethod
    def _make_sandbox(sandbox: Path) -> None:
        """一份最小仓库：够 `planned_files()` 跑通，不复制几 MB 的源码。"""
        copies = {
            "server/Dockerfile": ROOT / "server" / "Dockerfile",
            "server/requirements.txt": ROOT / "server" / "requirements.txt",
            "server/main.py": ROOT / "server" / "main.py",
            "app/audio/stt.py": ROOT / "app" / "audio" / "stt.py",
            "scripts/build_kit.py": ROOT / "scripts" / "build_kit.py",
            "scripts/prepare-backend.sh": ROOT / "scripts" / "prepare-backend.sh",
            "scripts/smoke-echo-backend.py": ROOT / "scripts" / "smoke-echo-backend.py",
        }
        for rel, src in copies.items():
            dst = sandbox / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        shutil.copytree(ROOT / "delivery" / "backend-cu126",
                        sandbox / "delivery" / "backend-cu126")


class DeliveryStaysOutOfTheMainPackage(unittest.TestCase):
    """`delivery/` 不在主包白名单里 —— 新增这两个目录不该被塞进 `dist/ECHO-main-*.zip`。"""

    def test_build_package_whitelist_has_no_delivery_dir(self):
        src = (SCRIPTS / "build-package.ps1").read_text(encoding="utf-8")
        match = re.search(r"^\$dirs = @\(([^)]*)\)", src, re.M)
        self.assertIsNotNone(match, "build-package.ps1 里的白名单 $dirs 没找到（格式变了？）")
        self.assertNotIn("delivery", match.group(1),
                         "delivery/ 进了主包白名单：交付模板会被塞进主包")

    def test_the_packer_maps_the_delivery_templates_to_the_zip_root(self):
        for variant in backend.VARIANTS:
            key = variant["key"]
            files = backend.planned_files(variant)
            self.assertEqual(files.get("先读我.md"), VARIANTS[key] / "先读我.md")
            self.assertEqual(files.get("compose.yaml"), VARIANTS[key] / "compose.yaml")
            self.assertEqual(files.get("server.yaml"), VARIANTS[key] / "server.yaml")
            # 两层源码按仓库相对路径进包（与 server/Dockerfile 的 COPY 对齐）
            self.assertEqual(files.get("server/main.py"), ROOT / "server" / "main.py")
            self.assertEqual(files.get("app/audio/stt.py"), ROOT / "app" / "audio" / "stt.py")

    def test_the_two_variants_differ_only_in_the_template_files(self):
        """同一份源码 + 两套模板：差异必须**只在**交付目录那四个文件里。"""
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("build_kit_for_diff", SCRIPTS / "build_kit.py")
        build_kit = _ilu.module_from_spec(spec)
        spec.loader.exec_module(build_kit)
        plans = {v["key"]: backend.planned_files(v) for v in backend.VARIANTS}
        self.assertEqual(sorted(plans["cu126"]), sorted(plans["cu118"]))
        differing = sorted(rel for rel in plans["cu126"]
                           if build_kit.sha256_file(plans["cu126"][rel])
                           != build_kit.sha256_file(plans["cu118"][rel]))
        self.assertEqual(differing, sorted(TEMPLATES),
                         f"两个变体之间有意外差异：{differing}")


if __name__ == "__main__":
    unittest.main()
