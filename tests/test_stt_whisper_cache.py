# -*- coding: utf-8 -*-
"""whisper「面板说已就绪」与「引擎真能加载」必须是**同一份判据**（2026-09-26 修复）。

现场（用户实测）：`models/faster-whisper/<档>` 里没有权重，权重躺在 **ModelScope 缓存**
（面板的下载按钮对 whisper 走 `_snapshot(ms_id=…)` = ModelScope 优先，落的就是那里）。
而 `stt._whisper_dir()` 当时只认 `models/faster-whisper/<档>` → 返回空 → 调用方按
**模型名**交给 faster-whisper → 它去 HF hub 找 → 离线时 `LocalEntryNotFoundError`，
联网时**静默重新下载 461 MB**。面板上写着「✅ 已就绪」，引擎却在偷偷重下。

这里用临时目录造出三种落点的"形状"，不依赖真实缓存、不联网、不加载真模型：
每个形状都同时问两处 —— `stt._whisper_dir()`（能不能加载）与
`modelinfo._ready_whisper()`（面板说什么）—— 两者必须一致。
"""
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from app import modelinfo
from app.audio import stt


class _WhisperLandingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-whisper-cache-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.models = os.path.join(self.tmp, "models")
        self.ms_cache = os.path.join(self.tmp, "mscache")
        os.makedirs(self.models)
        os.makedirs(self.ms_cache)
        self._patches = [
            patch.object(stt, "models_dir", lambda: self.models),
            patch.object(modelinfo, "models_dir", lambda: self.models),
            patch.object(modelinfo, "MS_CACHE", self.ms_cache),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _weight(self, *parts, name="model.bin"):
        path = os.path.join(*parts, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"weights")
        return os.path.dirname(path)

    def _assert_agree(self, tier, note):
        """两处判据一致：能加载 ⇔ 面板说就绪。"""
        found = stt._whisper_dir(tier)
        ready = modelinfo._ready_whisper(tier)
        self.assertEqual(bool(found), bool(ready),
                         "%s：面板说就绪=%s，能加载=%s —— 两者必须是同一份判据"
                         % (note, ready, bool(found)))
        return found


class WhisperLandingTests(_WhisperLandingTestCase):
    def test_local_faster_whisper_dir_is_used(self):
        d = self._weight(self.models, "faster-whisper", "small")
        self.assertEqual(self._assert_agree("small", "models/faster-whisper/<档>"), d)

    def test_hf_hub_cache_is_recognised(self):
        """HF hub 缓存（`HF_HOME` 指向 models → `models/hub`）。"""
        d = self._weight(self.models, "hub", "models--Systran--faster-whisper-tiny",
                         "snapshots", "abc123")
        self.assertEqual(self._assert_agree("tiny", "models/hub HF 缓存"), d)

    def test_modelscope_cache_is_recognised(self):
        """**这条就是那个 bug**：ModelScope 缓存必须认，否则会静默重下 461 MB。"""
        d = self._weight(self.ms_cache, "Systran--faster-whisper-base",
                         "snapshots", "master")
        self.assertEqual(self._assert_agree("base", "ModelScope 缓存"), d)

    def test_empty_snapshot_dir_is_not_ready(self):
        """空目录（下载中断留下的）**不算就绪** —— 加载器拿到它照样当场抛异常。"""
        os.makedirs(os.path.join(self.ms_cache, "Systran--faster-whisper-base",
                                 "snapshots", "master"))
        self.assertEqual(self._assert_agree("base", "空的快照目录"), "")
        self.assertFalse(modelinfo._ready_whisper("base"))

    def test_local_dir_without_model_bin_falls_through(self):
        """`models/faster-whisper/<档>` 光有目录不算，得**落到真有权重的那个位置**。"""
        os.makedirs(os.path.join(self.models, "faster-whisper", "tiny"))
        d = self._weight(self.ms_cache, "Systran--faster-whisper-tiny",
                         "snapshots", "master")
        self.assertEqual(self._assert_agree("tiny", "本地空目录 + MS 缓存有货"), d)

    def test_large_alias_maps_to_large_v3(self):
        d = self._weight(self.ms_cache, "Systran--faster-whisper-large-v3",
                         "snapshots", "master")
        self.assertEqual(stt._whisper_dir("large"), d, "large 应当折算成 large-v3")


class LoaderUsesTheResolvedPathTests(_WhisperLandingTestCase):
    """`_get_whisper()` 必须把**解析出来的本地目录**交给 faster-whisper，而不是模型名。"""

    def test_whisper_model_receives_the_local_path(self):
        d = self._weight(self.ms_cache, "Systran--faster-whisper-tiny",
                         "snapshots", "master")
        seen = {}

        class _FakeWhisperModel:
            def __init__(self, ref, device=None, compute_type=None):
                seen["ref"] = ref

        fake = types.SimpleNamespace(WhisperModel=_FakeWhisperModel)
        with patch.dict(sys.modules, {"faster_whisper": fake}), \
                patch.object(stt, "resolve_device", lambda choice: ("cpu", "int8")), \
                patch.object(stt, "_cache_gpu_name", lambda: None), \
                patch.dict(stt._ENGINES, {}, clear=True):
            stt._get_whisper("tiny", "auto")

        self.assertEqual(seen["ref"], d,
                         "交给引擎的必须是本地目录；给模型名会让它去联网重下")


if __name__ == "__main__":
    unittest.main()
