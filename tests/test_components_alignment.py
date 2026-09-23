# -*- coding: utf-8 -*-
"""清单对齐守卫（P2）：`modelinfo.CATALOG` ↔ 组件清单不能悄悄分叉。

P2 期间两份清单**并存**：面板「模型」页签仍读 `/api/models`（`modelinfo.CATALOG`），
新的「组件」页签读 `/api/components`（`app/components.py`）。它们描述的是同一批东西，
所以必须一一对应 —— 否则会出现"模型页签说已装、组件页签说缺失"这种自相矛盾的状态，
而向导（D23/D24）正是按组件清单决定"还要装什么"的。

这个测试把对应关系钉住（两个方向都查）：
  * `modelinfo.CATALOG` 里每个模型条目 → 组件清单里必须有对应条目；
  * 组件清单里模型类（stt-*/wake-*/diarize-*）条目 → `modelinfo.CATALOG` 里也得认识它。

命名映射是**故意显式列出来**的：哪天有人改 id，这里会红，而不是让两份清单默默错位。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import components, modelinfo                            # noqa: E402

#: modelinfo 的 id -> 组件清单的 id（不含 whisper，它按档位拼）
ID_MAP = {
    "sensevoice": "stt-sensevoice",
    "sherpa": "stt-sherpa",
    "qwen3asr": "stt-qwen3asr",
    "kws": "wake-kws",
    "pyannote": "diarize-pyannote",
}

#: 组件清单里属于"模型"的类别（其余是 runtime/accel/agent，不在 modelinfo 里）
MODEL_KINDS = ("stt", "wake", "diarize")

#: 组件清单里允许存在、但 modelinfo 不管的模型类条目（新增能力时在此显式登记）
ALLOWED_EXTRA = ()


def component_id_for(modelinfo_id):
    """modelinfo id → 组件 id。whisper 按档位拼：whisper-small → stt-whisper-small。"""
    if modelinfo_id.startswith("whisper-"):
        return "stt-" + modelinfo_id
    return ID_MAP.get(modelinfo_id)


class CatalogAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.comp = {i["id"]: i for i in components.load_manifests()}
        self.mi = {e["id"]: e for e in modelinfo.CATALOG}

    def test_every_modelinfo_entry_has_a_component(self):
        missing = []
        for mid in self.mi:
            cid = component_id_for(mid)
            if cid is None or cid not in self.comp:
                missing.append("%s -> %s" % (mid, cid))
        self.assertEqual(missing, [],
                         "modelinfo 里的模型没有对应的组件条目（两份清单分叉了）：%s" % missing)

    def test_every_model_component_is_known_to_modelinfo(self):
        known = {component_id_for(m) for m in self.mi}
        extra = []
        for cid, item in self.comp.items():
            if item.get("kind") not in MODEL_KINDS:
                continue
            if cid in known or cid in ALLOWED_EXTRA:
                continue
            extra.append(cid)
        self.assertEqual(extra, [],
                         "组件清单里有 modelinfo 不认识、也没登记的模型类条目：%s" % extra)

    def test_sizes_are_in_the_same_ballpark(self):
        """体积不该差一个数量级 —— 差了说明其中一份没跟着改。"""
        bad = []
        for mid, entry in self.mi.items():
            cid = component_id_for(mid)
            if not cid or cid not in self.comp:
                continue
            comp_mb = self.comp[cid].get("size_mb") or 0
            mi_mb = entry.get("size_mb") or entry.get("expected_mb") or 0
            if comp_mb and mi_mb and (comp_mb > mi_mb * 5 or mi_mb > comp_mb * 5):
                bad.append("%s: modelinfo=%s组件=%s" % (mid, mi_mb, comp_mb))
        self.assertEqual(bad, [], "同一组件的体积在两份清单里差太多：%s" % bad)

    def test_required_components_are_runtime_and_the_command_engine(self):
        """必装项 = 运行时核心 + **语音指令的转写引擎**。

        D23 的原意是"模型都该由向导按环境逐项问，不该默认必装" —— 这条**仍然成立**，
        所以**会议转写模型（sensevoice / whisper / qwen3asr）依旧全部可选**。

        2026-09-23 对 `stt-sherpa` 开了一个**有意的例外**，理由两条：

          1. 指令转写是 ECHO 的核心交互链路，**不允许依赖服务端可用性**
             （3.0 设计里的铁律 L3）；
          2. 那天的实测事故证明"靠向导逐项问"在这件事上**问不住** ——
             稳定版 `models/sherpa-onnx-streaming` 齐全、面板报「已就绪」，
             而 runtime-core 里没装 `sherpa_onnx`：每次语音指令都在转写处抛
             ModuleNotFoundError，面板一声不响、DSH 什么都没收到。
             用户根本不知道该去装什么。

        例外**只开给这一个**（它同时是 pip 引擎与模型，且是指令链路必需品）；
        下面第二条断言就是这条例外的边界。
        """
        req = [i["id"] for i in components.load_manifests() if i.get("required")]
        self.assertEqual(req, ["runtime-core", "stt-sherpa"])
        models_required = [i["id"] for i in components.load_manifests()
                           if i.get("required") and i.get("kind") in MODEL_KINDS]
        self.assertEqual(models_required, ["stt-sherpa"],
                         "除语音指令引擎外，不许再有模型类组件变成必装（D23 仍然有效）")

    def test_model_components_declare_model_id(self):
        """模型类组件必须写明 `model_id`（2026-09-19 合并「模型/组件」页签时加的）。

        有了它，面板才能在同一个页签里既显示"装没装"、又给出「下载」按钮
        （`/api/models/download` 只认 modelinfo 的 id），不必维护第二份 id 映射。
        """
        bad = []
        for cid, item in self.comp.items():
            if item.get("kind") not in MODEL_KINDS:
                continue
            mid = item.get("model_id")
            want = None
            for m, c in [(m, component_id_for(m)) for m in self.mi]:
                if c == cid:
                    want = m
            if mid != want:
                bad.append("%s: model_id=%r 应为 %r" % (cid, mid, want))
        self.assertEqual(bad, [], "模型类组件的 model_id 与 modelinfo 对不上：%s" % bad)

    def test_model_readiness_has_a_single_source(self):
        """就绪判定只能有一个来源：有 model_id 的组件必须与 `modelinfo.ready()` 一致。

        否则两份判据会分叉（组件写死路径、modelinfo 各写一个 ready 函数），
        用户会看到"组件说已装、模型说没装"这种自相矛盾。
        """
        from app import paths
        cat = {i["id"]: i for i in components.catalog(include_blocked=True)["items"]}
        checked = 0
        for cid, item in self.comp.items():
            mid = item.get("model_id")
            if not mid:
                continue
            checked += 1
            with self.subTest(component=cid):
                self.assertEqual(cat[cid]["ready"], modelinfo.ready(mid),
                                 "%s 的组件就绪判定与 %s 的模型就绪判定不一致" % (cid, mid))
        self.assertGreater(checked, 0, "没有任何组件声明 model_id（映射丢了？）")
        self.assertTrue(paths.models_root())


if __name__ == "__main__":
    unittest.main()
