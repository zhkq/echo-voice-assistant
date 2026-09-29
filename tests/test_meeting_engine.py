# -*- coding: utf-8 -*-
"""会议链路的**本机引擎路径已不存在** —— 契约用例（原文件于 2026-09-29 整体下线）。

## 为什么这个文件从"验分派"变成了"验不存在"

本文件原来钉的是**本机引擎分派**（`resolve_meeting_engine` / `MEETING_LOCAL_ENGINES`
/ `_skeleton_model` / `_fallback_sv_rows` / `_sherpa_rows` / `MeetingEngineRefused`，
以及 `_transcribe_impl` 里那四个分支）。用户拍板后：**客户端进程内不再承担会议转写与
说话人分离** —— 即使本机有 GPU，也以"在本机起一个能力后端"的形式完成。

那些入口被整体删掉了，所以"验分派"这件事**没有对象可验**：一个 `resolve_meeting_engine`
不存在的模块里，"它对于 paraformer 会报什么错"是一道不存在的题。把用例改写成
"这些入口已不存在"的契约，是**不削弱断言**的替代：

  * 删掉分派之后最容易出的错不是"分派判错"，而是**有人（或某个 agent）又把它加回来**
    （例如为了让某台老机器"还能跑"而恢复一条本机兜底）—— 那条路一回来，
    L5（一场会不许混向量空间）与"配了却用不上要如实说"两条就同时被绕开。
    这个文件现在正是拦这件事的闸：老名字一个都不许出现在模块里。
  * 同时钉住**留下的那条路没有被顺手删掉**：`_capability_segment_rows` /
    `_capability_diarize_segment`（会议唯一的两条产出路径）必须还在，
    否则"删干净"会变成"把会议转写整个删了"。

隔离（与 `tests/test_meeting_capability.py` / `tests/test_meeting_speakers_standard.py`
同一套写法）：`db.DATA_DIR` / `db.DB_FILE` / `settings._cache` / 会议目录 / 凭据文件
全部指向临时目录，**一个真模型都不加载、一只麦克风都不开**。
"""
import ast
import inspect
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                            # noqa: E402
import app.meeting as meeting                                  # noqa: E402
from app.capabilities import credentials as cred_mod           # noqa: E402
from app.config import DEFAULTS, settings                      # noqa: E402


#: 2026-09-29 被整体删掉的本机会议引擎入口（**一个都不许回来**）。
REMOVED_ENTRY_POINTS = (
    "resolve_meeting_engine",
    "unsupported_engine_reason",
    "MEETING_LOCAL_ENGINES",
    "MEETING_ENGINE_LABELS",
    "MeetingEngineRefused",
    "_skeleton_model",
    "_fallback_sv_rows",
    "_sherpa_rows",
    "_boot_meeting_stt",
    "_boot_note_meeting_key",
)

#: 会议**唯一**的产出路径（删干净 ≠ 把会议转写整个删掉）。
KEPT_ENTRY_POINTS = (
    "_capability_segment_rows",
    "_capability_diarize_segment",
    "_apply_run_meta",
    "_new_diarize_state",
    "_transcribe_impl",
)


class _IsolatedState(unittest.TestCase):
    """库 / 设置缓存 / 凭据文件 / 会议目录全指向临时目录（不碰真实 `data/meetings/**`）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-mtg-engine-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "engine.db")
        db.init()
        settings._cache = None
        settings.seed_defaults()
        self._cred = os.path.join(self.tmp, "backend.json")
        p = patch.object(cred_mod, "credentials_path", lambda: self._cred)
        p.start()
        self.addCleanup(p.stop)
        self.meetings_root = os.path.join(self.tmp, "meetings")
        os.makedirs(self.meetings_root, exist_ok=True)
        p = patch.object(meeting, "meetings_dir", lambda: self.meetings_root)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        db.DATA_DIR, db.DB_FILE = self._old_db
        settings._cache = None
        shutil.rmtree(self.tmp, ignore_errors=True)


class NoLocalMeetingEngineTests(_IsolatedState):
    """本机会议引擎的入口**全部不存在**，而且模块里不再引用本机分离栈。"""

    @classmethod
    def setUpClass(cls):
        cls.src = inspect.getsource(meeting)

    def test_the_removed_entry_points_are_really_gone(self):
        """两个判据：模块上取不到这个名字，且没有代码**调用**它（AST，不看注释）。

        为什么要 AST 而不是 `字符串 in src`：`_skeleton_model` 这类名字在注释里
        被当作历史提一句是正常的（"原来的做法是 …"），而**调用一个不存在的函数**
        才是要拦的事。用字符串判会把前者误判成后者。
        """
        tree = ast.parse(self.src)
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name:
                    called.add(name)
        for name in REMOVED_ENTRY_POINTS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(meeting, name),
                                 "%s 又回来了 —— 客户端进程内不再做会议转写" % name)
                self.assertNotIn(name, called,
                                 "还在调用 %s —— 那是个已经不存在的入口" % name)

    def test_the_capability_path_is_the_only_one_left(self):
        """删掉本机那条路 ≠ 把会议转写整个删了：能力层那两条必须还在。"""
        for name in KEPT_ENTRY_POINTS:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(meeting, name, None)),
                                "会议唯一的产出路径被删掉了：%s" % name)

    def test_the_meeting_link_never_calls_the_local_diarizer(self):
        """`app/audio/diarize.py` 在**会议链路**上不再被引用。

        判据走 AST（不看注释/文档字符串）：模块里不许**导入**或**调用**
        `diarize_wav_full` —— 那会在一场会里混进第二个向量空间
        （L5：认错人且不报错）。

        允许的唯一一处是 `SpeakerRegistry`（后端给了嵌入才建，把局部标签缝成同一个人）
        —— 它**不是**本机分离引擎。
        """
        tree = ast.parse(self.src)
        imported = set()          # `from x import y` 的 y / `import x`
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.Import):
                imported |= {(a.asname or a.name).split(".")[-1] for a in node.names}
            elif isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name:
                    called.add(name)
        self.assertNotIn("diarize_wav_full", imported | called,
                         "会议链路还在导入/调用本机分离引擎 —— 那正是这一步要断掉的引用")
        # `SpeakerRegistry` 只剩下"后端给了嵌入才 import"这一处，且必须带守卫
        self.assertIn("SpeakerRegistry", imported,
                      "后端给了嵌入时仍然要能把局部标签缝成同一个人")
        hits = [ln.strip() for ln in self.src.splitlines()
                if "SpeakerRegistry" in ln and ln.strip().startswith("from ")]
        self.assertEqual(len(hits), 1,
                         "SpeakerRegistry 的 import 点应当只剩一处：%s" % hits)
        # 它必须在"labels 非空"的分支里（后面紧跟构造），而不是无条件建
        idx = self.src.index("from app.audio.diarize import SpeakerRegistry")
        window = self.src[max(0, idx - 600):idx]
        self.assertIn("if labels:", window,
                      "注册表必须**只有后端给了嵌入才建**（没有向量就无从缝合）")

    def test_no_app_module_declares_a_local_meeting_slot(self):
        """本机后端**不再声明** `diarize.*` / `speaker.embed` / `asr.timestamps`。

        声明了却做不到（或做得到但语义已废）= 路由会把活派过来；而这里更要紧的是
        "声明"就是"用户可以指望"，一份多余的声明会让面板与路由各说一套。
        """
        from app.capabilities.local import LOCAL_SLOTS, LocalCapabilityClient
        self.assertEqual(set(LOCAL_SLOTS), {"asr.text"})
        self.assertEqual(set(LocalCapabilityClient().provides), {"asr.text"})

    def test_the_command_engine_is_still_what_the_dispatch_table_watches(self):
        """`stt-cmd` 是**留下**的那一条（铁律 L3）—— 换引擎仍要重载它。

        本步（2026-09-29）删掉了会议那一档，所以这里钉的是"留下的那条没被顺手删掉"：
        `sttModel` → `stt-cmd` 的映射必须还在（它由 `app/boot.py` 的
        `stt-cmd` 组件承载）。
        """
        from app import settings_effects
        self.assertIn("sttModel", settings_effects._STT_KEYS)
        self.assertEqual(settings_effects._STT_KEYS["sttModel"], ("stt-cmd", True))


class RetiredMeetingSttModelTests(_IsolatedState):
    """`meetingSttModel` 的废弃处理：**值留库、不出接口、写入被拒**。"""

    def test_the_key_is_deprecated_but_still_readable(self):
        self.assertTrue(DEFAULTS["meetingSttModel"].get("deprecated"),
                        "meetingSttModel 必须标成已废弃（它不再是设置）")
        db.set_setting("meetingSttModel", "sensevoice")
        settings._cache = None
        self.assertEqual(settings.get("meetingSttModel"), "sensevoice",
                         "库里的值一个字节都不许改（兼容读取）")
        self.assertNotIn("meetingSttModel",
                         [r["key"] for r in settings.all(include_hidden=True)])

    def test_writing_it_is_refused_without_an_exception(self):
        got = settings.update({"meetingSttModel": "sherpa"})
        self.assertNotIn("meetingSttModel", got)
        self.assertNotEqual(settings.get("meetingSttModel"), "sherpa")

    def test_the_retired_whisper_fallback_rule_is_gone(self):
        """键都废弃了，它那条"whisper 档折成 qwen3asr"的规则也随之删掉。

        判据是**读出来仍是原值**（没有一条代码再看它，也不需要折算）——
        折了反而会造出"库里 small、读出来 qwen3asr"这种没人消费的差异。
        """
        from app.config import RETIRED_VALUE_FALLBACKS
        self.assertNotIn("meetingSttModel", RETIRED_VALUE_FALLBACKS)
        db.set_setting("meetingSttModel", "small")
        settings._cache = None
        self.assertEqual(settings.get("meetingSttModel"), "small")

    def test_the_meeting_link_no_longer_reads_it(self):
        """**会议链路**里再也没有读取方（这一步的边界就到这里）。

        判据走 AST 的字符串字面量（注释与文档字符串不算"引用"）：`app/meeting.py`
        里不许再出现这个键 —— 它原来是"这场用哪个本机引擎"的唯一读取方，
        而那条分派已经删掉了。

        ⚠️ **本步不管** `app/install_state.py` / `app/model_usage.py` /
        `app/model_cleanup.py` 里那三处**模型管理/探测**的引用：它们属于
        "清模型管理与探测"那一步（见报告里的待办）。在那里一并收口，
        而不是在这一步顺手改掉 —— 那会让两批改动混在一起，出问题时分不清是谁引入的。
        """
        tree = ast.parse(inspect.getsource(meeting))
        hits = [node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and node.value == "meetingSttModel"]
        self.assertEqual(hits, [],
                         "app/meeting.py 里还在读写 meetingSttModel（第 %s 行）" % hits)

    def test_there_are_no_replacement_knobs_for_local_meeting_asr(self):
        """废弃是"整项下线"，不是"换个键接着配"：`/api/settings` 里不该有任何
        "本机会议转写引擎"这一类可选值。"""
        rows = settings.all(include_hidden=True)
        keys = {r["key"] for r in rows}
        self.assertNotIn("meetingSttModel", keys)
        for key in keys:
            self.assertFalse(re.search(r"meeting.*stt.*model", key, re.I),
                             "又冒出一个'本机会议引擎'的设置项：%s" % key)


if __name__ == "__main__":
    unittest.main()
