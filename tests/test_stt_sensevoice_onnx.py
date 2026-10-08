# -*- coding: utf-8 -*-
"""`sensevoice-onnx` 引擎（2026-10-08 起语音指令的默认引擎）。

为什么单独钉一批：它**取代了 sherpa** 成为出厂默认，而 sherpa 在中文上的"叠字"
是用户明确抱怨的问题（实测同段音频叠字分 852 vs 73）。这条默认值以后很容易被
"顺手改回去"，而改回去**不会报错**，只是识别质量悄悄变差 —— 所以判据要留在门禁里。

同时钉住三件容易漏的事：
  1. **不依赖 torch/funasr** —— 走 sherpa_onnx 的离线识别器。这是它体积/依赖优势的全部意义，
     一旦有人改成 funasr 那条路，客户端就要多装 2.9 GB。
  2. **缺模型时回落 sherpa**，而不是哑掉（老装机上 `sttModel` 的出厂值已经是它）。
  3. **VAD 切段**：SenseVoice 是离线整段模型，10 分钟音频不切段只能出几个字（实测过）。
"""
import io
import os
import re
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.audio import stt                                          # noqa: E402
from app import modelinfo, components                              # noqa: E402
from app.config import DEFAULTS                                    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with io.open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class DefaultEngineTests(unittest.TestCase):
    def test_factory_default_is_the_onnx_one(self):
        self.assertEqual(DEFAULTS["sttModel"]["value"], "sensevoice-onnx",
                         "语音指令的出厂默认必须是 sensevoice-onnx（中文叠字远少于流式 sherpa）")

    def test_resolve_engine_accepts_it_and_none(self):
        self.assertEqual(stt.resolve_engine(None), ("sensevoice-onnx", ""))
        self.assertEqual(stt.resolve_engine("sensevoice-onnx"), ("sensevoice-onnx", ""))
        # 空串也是"没选" —— 老库/新装都可能给空
        self.assertEqual(stt.resolve_engine(""), ("sensevoice-onnx", ""))

    def test_it_is_in_the_panel_options(self):
        self.assertIn("sensevoice-onnx", DEFAULTS["sttModel"]["options"])
        # sherpa 必须**留在选项里**：唤醒用它，且是缺模型时的兜底
        self.assertIn("sherpa", DEFAULTS["sttModel"]["options"])

    def test_every_engine_option_is_known_to_the_panel(self):
        """**`sttModel` 的每个取值都必须在面板的两张表里**（2026-10-08 测试机实测的 bug）。

        现场：包里客户端默认已经是 `sensevoice-onnx`，但测试机面板显示"当前 = sherpa 流式"。
        根因是 `web/app.js` 里两张**手写映射表**都漏了它：
          * `_ENGINE_MODEL_ID` 漏 → `engineModelId("sensevoice-onnx")` 返回空 →
            `capAsrLocal()` 的「配置为要用的」算不出该项 → 那张卡**掉进折叠的「其余本地引擎」**，
            于是用户看到别的引擎像是"当前"；
          * `friendlyOption("sttModel", …)` 漏 → 名称回落到 `"Whisper " + s`（认不出来）。
        **新增任何引擎取值都要同步加这两处** —— 这个 bug 是静默的（不报错、只显示错）。
        """
        js = io.open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8").read()
        # ① _ENGINE_MODEL_ID 表里要有每一个取值
        m = re.search(r"_ENGINE_MODEL_ID\s*=\s*\{(.*?)\}", js, re.S)
        self.assertIsNotNone(m, "找不到 _ENGINE_MODEL_ID 定义")
        table = m.group(1)
        for opt in DEFAULTS["sttModel"]["options"]:
            self.assertIn('"%s"' % opt, table,
                          "面板 _ENGINE_MODEL_ID 缺 %r —— 它的卡片会被折进「其余本地引擎」" % opt)
        # ② friendlyOption 的名称表里也要有（避免回落到 "Whisper …"）
        k = js.find('if (key === "sttModel")')
        self.assertGreater(k, 0, "找不到 friendlyOption 的 sttModel 分支")
        branch = js[k:k + 500]
        for opt in DEFAULTS["sttModel"]["options"]:
            self.assertTrue(('"%s"' % opt) in branch or ("%s:" % opt) in branch,
                            "面板显示名表缺 %r（会显示成 'Whisper %s'）" % (opt, opt))

    def test_engine_key_is_stable(self):
        self.assertEqual(stt.engine_key("sensevoice-onnx", ""), "sensevoice-onnx")


class NoTorchDependencyTests(unittest.TestCase):
    """它是**不许**碰 torch/funasr 的 —— 否则客户端要多装 2.9 GB，这个方案就没意义了。"""

    def test_loader_uses_sherpa_onnx_not_funasr(self):
        src = _read("app", "audio", "stt.py")
        i = src.find("def _get_sensevoice_onnx")
        self.assertGreater(i, -1)
        seg = src[i:src.find("\ndef ", i + 10)]
        self.assertIn("sherpa_onnx", seg, "加载器必须走 sherpa_onnx")
        self.assertNotIn("AutoModel", seg, "不许用 funasr 的 AutoModel（那会拖进 torch）")
        self.assertIn("from_sense_voice", seg, "要用 sherpa_onnx 的 SenseVoice 工厂方法")

    def test_onnx_files_are_the_three_we_documented(self):
        """三份文件名要与下载条目、文档一致（改名会让"下好了却认不出"）。"""
        src = _read("app", "audio", "stt.py")
        i = src.find("def sensevoice_onnx_files")
        seg = src[i:src.find("\ndef ", i + 10)]
        self.assertIn("sensevoice-onnx", seg)
        self.assertIn("tokens.txt", seg)
        self.assertIn("silero_vad.onnx", seg)
        self.assertIn('f.startswith("model")', seg, "要认 model*.onnx（int8/fp32 都行）")


class MissingModelFallbackTests(unittest.TestCase):
    """默认引擎但模型没下 → 必须回落 sherpa，不能哑掉。"""

    def test_falls_back_when_onnx_files_are_absent(self):
        src = _read("app", "audio", "stt.py")
        i = src.find('if engine == "sensevoice-onnx"')
        seg = src[i:i + 1800]
        self.assertIn("sensevoice_onnx_files()", seg, "没有检查模型是否存在")
        self.assertIn('engine = "sherpa"', seg, "缺模型时没有回落 sherpa")
        self.assertIn("_sherpa_files()", seg, "回落前要确认 sherpa 在")

    def test_reports_clearly_when_both_are_missing(self):
        src = _read("app", "audio", "stt.py")
        i = src.find('if engine == "sensevoice-onnx"')
        seg = src[i:i + 1800]
        self.assertIn("TRANSCRIBE_ERROR", seg)
        self.assertIn("都未就绪", seg, "两个都没有时要给人话原因，不能返回空串了事")


class VadSegmentationTests(unittest.TestCase):
    """VAD 是长音频的必需件：不切段 → 10 分钟音频只出几个字（实测）。"""

    def test_transcribe_uses_vad_when_available(self):
        src = _read("app", "audio", "stt.py")
        i = src.find("def _sensevoice_onnx_transcribe")
        seg = src[i:src.find("\ndef ", i + 10)]
        self.assertIn("VoiceActivityDetector", _read("app", "audio", "stt.py"))
        self.assertIn("accept_waveform", seg, "没有把音频喂给 VAD")
        self.assertIn("flush", seg, "结尾那段（未触发静音判定）要收尾识别")
        # 没有 VAD 时仍要能整段识别（降级，不是失败）
        self.assertIn("if vad_cfg is None:", seg)

    def test_vad_failure_is_not_fatal(self):
        src = _read("app", "audio", "stt.py")
        i = src.find("def _get_sensevoice_onnx")
        seg = src[i:src.find("\ndef ", i + 10)]
        self.assertIn("cfg = None", seg, "VAD 起不来时要退回整段识别，而不是抛出去")


class CatalogAndDeliveryTests(unittest.TestCase):
    def test_every_catalog_entry_has_a_readiness_probe(self):
        """**每个目录条目都要能拿到就绪判据** —— 漏了会表现成"文件明明在、面板说没就位"。

        2026-10-08 部署到稳定版当天就踩了：`_PROBES` 里没有 `sensevoice-onnx`，
        面板报 `ready=False / local_mb=0 / 模型文件还没就位`，而三份文件都在
        `D:\\ECHO\\models\\sensevoice-onnx` —— 用户会去白重下那 228 MB。
        "判据没登记"与"模型真缺失"在界面上长得一模一样，只能靠这条守卫兜住。

        ⚠️ 判据钉在 **`_probe_for(entry)`**（真正的分派点）上，不是 `_PROBES`：
        `_PROBES` 查不到时它还有 whisper 那条按档位拼的兜底，只看 `_PROBES`
        会把 whisper 各档误报成"没有判据"。
        """
        bad = []
        for e in modelinfo.CATALOG:
            p = modelinfo._probe_for(e)
            if not callable(p):
                bad.append(e.get("id"))
        self.assertEqual(bad, [],
                         "这些模型条目拿不到就绪判据（面板会把它们报成未就位）：%s" % bad)

    def test_the_onnx_entry_is_reachable_through_the_real_dispatch(self):
        """新条目要能从**真实分派点**走到它的判据上（而不是只存在于某张表里）。"""
        e = {x.get("id"): x for x in modelinfo.CATALOG}.get("sensevoice-onnx")
        self.assertIsNotNone(e)
        self.assertTrue(modelinfo._probe_for(e) is modelinfo._ready_sensevoice_onnx,
                        "sensevoice-onnx 没有接到它自己的就绪判据上")

    def test_the_probe_does_not_require_torch(self):
        """ONNX 版的就绪判据**不许**依赖 torch/funasr —— 那正是它存在的理由。

        用**行为**验，不用读源码（源码里 docstring 提到 torch 会被误判）：
        把 torch/funasr 的可用性打桩掉，判据仍应为 True。
        """
        with patch.object(modelinfo, "_pkg_available",
                          lambda name: name == "sherpa_onnx"), \
                patch.object(modelinfo, "_ready_sensevoice", lambda: False), \
                patch("app.audio.stt.sensevoice_onnx_files",
                      lambda: ("m.onnx", "tokens.txt", "vad.onnx")):
            self.assertTrue(modelinfo._ready_sensevoice_onnx(),
                            "torch/funasr 缺失时 ONNX 版仍应判就绪 ——"
                            " 这句红了说明判据里混进了 torch/funasr 依赖")

    def test_catalog_entry_exists_with_the_three_files(self):
        entries = {e.get("id"): e for e in modelinfo.CATALOG}
        e = entries.get("sensevoice-onnx")
        self.assertIsNotNone(e, "模型目录里没有 sensevoice-onnx（面板下不到它）")
        self.assertIn("model.int8.onnx", e.get("allow") or [],
                      "只许拉 int8（fp32 是 937 MB，白拖三倍）")
        self.assertIn("tokens.txt", e.get("allow") or [])
        self.assertIn("sensevoice-onnx", e.get("target") or "")

    def test_download_worker_has_the_branch(self):
        src = _read("app", "modelinfo.py")
        self.assertIn('mid == "sensevoice-onnx"', src, "下载分支没接上")
        self.assertIn("silero_vad.onnx", src, "VAD 没被下载（长音频会退化成几个字）")
        self.assertIn('"sensevoice-onnx": 230', src, "进度百分比的分母没登记")

    def test_component_entry_is_required(self):
        src = _read("app", "components.py")
        self.assertIn('id="stt-sensevoice-onnx"', src)
        i = src.find('id="stt-sensevoice-onnx"')
        seg = src[i:i + 700]
        self.assertIn("required=True", seg, "默认引擎必须是必装件")
        self.assertIn("230", seg, "体积要如实写（约 230 MB）")

    def test_sherpa_stays_required_because_wake_uses_it(self):
        """**不能顺手把 sherpa 删掉**：语音唤醒的主路用它（SenseVoice 是离线整段模型）。"""
        src = _read("app", "components.py")
        i = src.find('id="stt-sherpa"')
        self.assertGreater(i, -1, "sherpa 条目没了 —— 唤醒会没得用")
        seg = src[i:i + 700]
        self.assertIn("required=True", seg)
        self.assertIn("唤醒", seg, "要把『唤醒用它』写在 purpose 里，免得后人删")

        wake = _read("app", "audio", "wake.py")
        self.assertIn("_get_sherpa", wake, "唤醒的实现确实依赖 sherpa 流式识别器")
        self.assertIn("_make_stream_detector", wake)


class WakeModelNotBundledTests(unittest.TestCase):
    """用户 2026-10-08 定：**唤醒默认关闭，唤醒模型不进离线包**，启用时再下。"""

    def test_wake_is_off_by_default(self):
        self.assertFalse(DEFAULTS["wakeEnabled"]["value"],
                         "唤醒默认必须是关的（关了才谈得上『唤醒模型不进包』）")

    def test_wakeword_model_is_downloadable_on_demand(self):
        """不在包里 ≠ 没有：启用唤醒时面板要能把它下下来。"""
        entries = {e.get("id"): e for e in modelinfo.CATALOG}
        has_wake = any("wake" in (k or "") for k in entries) or \
            "wakeword" in _read("app", "modelinfo.py")
        self.assertTrue(has_wake, "唤醒模型既不在包里、也没下载入口，启用唤醒时就没得用")


if __name__ == "__main__":
    unittest.main()
