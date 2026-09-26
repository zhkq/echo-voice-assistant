import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import modelinfo


def _write_weights(root, *parts, filename="model.bin", size=1024):
    """造一份"权重在这儿"的形状：`<root>/<parts…>/<filename>`。

    `snapshots/master/` 这一层是照着真实缓存写的：HF 缓存与 ModelScope 缓存都把
    权重放在 `<repo 目录>/snapshots/<rev>/` 下（2026-09-26 在本机实测过两种布局）。
    """
    d = Path(root).joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_bytes(b"\0" * size)
    return d


class _ModelDirsCase(unittest.TestCase):
    """把"本机模型目录"与"ModelScope 缓存"都换成临时目录的基类。

    **不许依赖本机真实缓存**：这台机器上恰好有/没有哪份权重，不该决定用例的结果
    （真实缓存也不许被测试写坏）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.models_root = Path(self._tmp.name) / "models"
        self.models_root.mkdir()
        self.ms_cache = Path(self._tmp.name) / "mscache"
        self._stub(models_dir=lambda: str(self.models_root),
                   MS_CACHE=str(self.ms_cache),
                   _pkg_available=lambda name: True)

    def _stub(self, **attrs):
        for name, value in attrs.items():
            p = patch.object(modelinfo, name, value)
            p.start()
            self.addCleanup(p.stop)


class WhisperLandingTests(_ModelDirsCase):
    """whisper 档位的「已下载」有**三个**落点，每一个都要被就绪判据认出来。

    2026-09-26 报的那个 bug 就是第三个：权重落在 ModelScope 缓存里（面板点下载的
    **真实落点** —— `_snapshot(ms_id=ms_ref, hf_id=ref)` 是 ModelScope 优先），
    而 `_ready_whisper` 只查前两个 → 真下完了 72 MB，面板仍写「未安装」。
    """

    def test_weights_in_the_modelscope_cache_are_ready(self):
        """③ ModelScope 缓存 —— 那条 bug 的落点。"""
        _write_weights(self.ms_cache, "Systran--faster-whisper-small",
                       "snapshots", "master", filename="model.bin")
        self.assertTrue(modelinfo._ready_whisper("small"))

    def test_weights_in_the_hf_hub_cache_are_ready(self):
        """② HF 缓存（`HF_HOME` 指向 models，所以落点是 `models/hub`）。"""
        _write_weights(self.models_root, "hub", "models--Systran--faster-whisper-small",
                       "snapshots", "abc123", filename="model.bin")
        self.assertTrue(modelinfo._ready_whisper("small"))

    def test_weights_in_the_local_dir_are_ready(self):
        """① `stt._whisper_dir()` 认的本地目录（从源机拷过来的那种）。"""
        _write_weights(self.models_root, "faster-whisper", "small", filename="model.bin")
        self.assertTrue(modelinfo._ready_whisper("small"))

    def test_no_weights_anywhere_is_not_ready(self):
        self.assertFalse(modelinfo._ready_whisper("small"))

    def test_an_empty_landing_dir_is_not_ready(self):
        """空目录（下载中断留下的）不算 —— 与 `WhisperModel()` 的行为对齐（它会当场抛异常）。"""
        (self.ms_cache / "Systran--faster-whisper-small" / "snapshots" / "master").mkdir(
            parents=True)
        (self.models_root / "faster-whisper" / "small").mkdir(parents=True)
        self.assertFalse(modelinfo._ready_whisper("small"))

    def test_local_usage_counts_the_modelscope_landing(self):
        """「本机占用」也要跟着落点走：否则面板上一个已就绪的档显示 `0 MB`。"""
        _write_weights(self.ms_cache, "Systran--faster-whisper-small",
                       "snapshots", "master", filename="model.bin", size=3 * 1048576)
        entry = modelinfo._by_id("whisper-small")
        self.assertEqual(modelinfo._target_path(entry),
                         str(self.ms_cache / "Systran--faster-whisper-small"))


class CacheWeightsAreRequiredTests(_ModelDirsCase):
    """「目录在」不等于「装好了」：ModelScope 缓存里光有目录不算就绪。

    原来 `_ready_qwen` 只看 `os.path.isdir` —— 下砸了留下的空目录会被报成「已就绪」，
    而 funasr 一加载就炸。pyannote 那条判据早就写着"空目录不能代表模型已下载"，
    这里把同一份纪律补齐到 qwen3asr / 对齐器 / SenseVoice 上。
    """

    def test_qwen3asr_empty_cache_dir_is_not_ready(self):
        (self.ms_cache / "Qwen--Qwen3-ASR-0.6B" / "snapshots" / "master").mkdir(parents=True)
        self.assertFalse(modelinfo._ready_qwen("Qwen/Qwen3-ASR-0.6B"))

    def test_qwen3asr_weights_in_the_cache_dir_are_ready(self):
        _write_weights(self.ms_cache, "Qwen--Qwen3-ASR-0.6B", "snapshots", "master",
                       filename="model.safetensors")
        self.assertTrue(modelinfo._ready_qwen("Qwen/Qwen3-ASR-0.6B"))

    def test_the_forced_aligner_is_part_of_qwen3asr_readiness(self):
        """ASR 到位、**对齐器没到** → 整体不算就绪（判据里两个模型都要）。

        句子时间戳全靠它；只看 ASR 会把"转出来没有时间轴"报成「已就绪」。
        """
        _write_weights(self.ms_cache, "Qwen--Qwen3-ASR-0.6B", "snapshots", "master",
                       filename="model.safetensors")
        self.assertFalse(modelinfo._PROBES["qwen3asr"]())
        _write_weights(self.ms_cache, "Qwen--Qwen3-ForcedAligner-0.6B", "snapshots", "master",
                       filename="model.safetensors")
        self.assertTrue(modelinfo._PROBES["qwen3asr"]())

    def test_sensevoice_empty_cache_dir_is_not_ready(self):
        (self.ms_cache / "iic--SenseVoiceSmall" / "snapshots" / "master").mkdir(parents=True)
        self.assertFalse(modelinfo._ready_sensevoice())

    def test_sensevoice_weights_in_the_cache_dir_are_ready(self):
        _write_weights(self.ms_cache, "iic--SenseVoiceSmall", "snapshots", "master",
                       filename="model.pt")
        self.assertTrue(modelinfo._ready_sensevoice())


class LocalUsageTests(_ModelDirsCase):
    """「本机占用」要如实：qwen3asr 这一档由**两个**模型组成。"""

    def test_qwen3asr_usage_counts_the_forced_aligner(self):
        """ASR + 对齐器都要算（只算一个会把 3.6 GB 报成 1.8 GB，看着像下了一半）。"""
        _write_weights(self.ms_cache, "Qwen--Qwen3-ASR-0.6B", "snapshots", "master",
                       filename="model.safetensors", size=2 * 1048576)
        _write_weights(self.ms_cache, "Qwen--Qwen3-ForcedAligner-0.6B", "snapshots", "master",
                       filename="model.safetensors", size=2 * 1048576)
        row = {i["id"]: i for i in modelinfo.inventory()}["qwen3asr"]
        self.assertEqual(row["local_mb"], 4, "两个模型的占用都要算进来：%s" % row)

    def test_the_measure_scope_names_both_qwen_models(self):
        self.assertEqual(
            [os.path.basename(p) for p in modelinfo._measure_paths(modelinfo._by_id("qwen3asr"))],
            ["Qwen--Qwen3-ASR-0.6B", "Qwen--Qwen3-ForcedAligner-0.6B"])


class DownloadedBytesHonestyTests(_ModelDirsCase):
    """下载状态**不许拿 0 冒充「未知」**。

    原始症状：whisper 一次真下了 72 MB，`/api/models` 的 `jobs` 里 `downloaded_mb` 是
    `0`，而 `message` 写着"下载完成" —— 两个字段自相矛盾，面板据此画出来的进度条
    从头到尾停在 0%。
    """

    def test_real_bytes_come_from_the_modelscope_landing(self):
        _write_weights(self.ms_cache, "Systran--faster-whisper-tiny", "snapshots", "master",
                       filename="model.bin", size=3 * 1048576)
        self.assertEqual(modelinfo._downloaded_mb("whisper-tiny"), 3)

    def test_pyannote_bytes_ignore_the_neighbours_in_the_same_folder(self):
        """pyannote 的进度只算**真正要用的三个 -local 目录**。

        `models/pyannote/` 那一层里还躺着别的模型的冗余副本（本机实测：一个 189 MB 的
        sherpa 副本就在旁边）—— 整目录算进来会把 31 MB 的模型报成 221 MB，
        与面板「本机占用」那个数字对不上（两个数字都在说"这个模型占多大"）。
        """
        _write_weights(self.models_root, "pyannote", "pyannote-wespeaker-local",
                       filename="pytorch_model.bin", size=2 * 1048576)
        _write_weights(self.models_root, "pyannote",
                       "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
                       filename="encoder.onnx", size=9 * 1048576)
        self.assertEqual(modelinfo._downloaded_mb("pyannote"), 2,
                         "旁边那个 9 MB 的副本不算 pyannote 的下载量")
        self.assertEqual(modelinfo._downloaded_mb("pyannote"),
                         sum(modelinfo._dir_mb(p)
                             for p in modelinfo._measure_paths(modelinfo._by_id("pyannote"))),
                         "「下载了多少」与「本机占用」必须是同一个范围")

    def test_unknown_landing_reports_none_not_zero(self):
        """一个落点目录都不存在 = **算不出来** → `None`（"未知"），不是 `0`。"""
        self.assertIsNone(modelinfo._downloaded_mb("whisper-tiny"))
        self.assertIsNone(modelinfo._downloaded_mb("sherpa"))
        self.assertIsNone(modelinfo._downloaded_mb("qwen3asr"))

    def test_a_completed_download_reports_the_real_number(self):
        """**那条原始症状的护栏**：完成时按真实落点回报（3 MB），绝不是 0。

        不联网：`_snapshot` 换成"把权重写进 ModelScope 缓存"的替身 —— 这正是真实
        下载的落点。
        """
        def fake_snapshot(ms_id="", hf_id="", local_dir=None, allow=None):
            _write_weights(self.ms_cache, "Systran--faster-whisper-tiny",
                           "snapshots", "master", filename="model.bin", size=3 * 1048576)
            return "modelscope"

        self._stub(_snapshot=fake_snapshot)
        entry = modelinfo._by_id("whisper-tiny")
        with patch.dict(modelinfo._JOBS, {}, clear=True), \
                patch.dict(modelinfo._ACTIVE, {"id": None}):
            modelinfo._JOBS["whisper-tiny"] = {
                "id": "whisper-tiny", "status": "running", "percent": None,
                "downloaded_mb": None, "message": "下载中…", "started_at": "", "done_at": ""}
            modelinfo._download_worker(entry)
            job = modelinfo.jobs()["items"]["whisper-tiny"]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["downloaded_mb"], 3, "要从真实落点算出 3 MB，不许报 0")
        self.assertIn("modelscope", job["message"])

    def test_a_fresh_job_says_unknown_instead_of_zero(self):
        """刚点下下载的那一刻我们**还没量过盘** → `downloaded_mb` 是 None，不是 0。"""
        self._stub(_download_worker=lambda entry: None,
                   dependency_problem=lambda mid: {})
        with patch.dict(modelinfo._JOBS, {}, clear=True), \
                patch.dict(modelinfo._ACTIVE, {"id": None}):
            ok, _msg = modelinfo.start_download("whisper-tiny", force=True)
            self.assertTrue(ok)
            job = modelinfo.jobs()["items"]["whisper-tiny"]
            self.assertIsNone(job["downloaded_mb"], "不许用 0 冒充「未知」：%s" % job)
            self.assertIsNone(job["percent"], "百分比同理：这一刻是「未知」：%s" % job)


class SenseVoiceReadyTests(unittest.TestCase):
    """SenseVoice 就绪 = 模型落地 + funasr/torch 运行时可用，缺一都不算。"""

    def _local_model(self, root):
        d = Path(root) / "sensevoice"
        d.mkdir(parents=True, exist_ok=True)
        (d / "model.pt").write_bytes(b"weights")

    def _patched(self, root, available):
        return (
            patch.object(modelinfo, "models_dir", lambda: root),
            patch.object(modelinfo, "_ms_dir", return_value=str(Path(root) / "cache-miss")),
            patch.object(modelinfo, "_pkg_available", side_effect=available),
        )

    def test_model_without_runtime_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._local_model(tmp)
            patches = self._patched(tmp, lambda name: False)
            for p in patches:
                p.start()
            try:
                self.assertFalse(modelinfo._ready_sensevoice())
            finally:
                for p in reversed(patches):
                    p.stop()

    def test_model_with_runtime_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._local_model(tmp)
            patches = self._patched(tmp, lambda name: True)
            for p in patches:
                p.start()
            try:
                self.assertTrue(modelinfo._ready_sensevoice())
            finally:
                for p in reversed(patches):
                    p.stop()

    def test_missing_torch_still_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._local_model(tmp)
            patches = self._patched(tmp, lambda name: name == "funasr")
            for p in patches:
                p.start()
            try:
                self.assertFalse(modelinfo._ready_sensevoice())
            finally:
                for p in reversed(patches):
                    p.stop()

    def test_no_model_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            patches = self._patched(tmp, lambda name: True)
            for p in patches:
                p.start()
            try:
                self.assertFalse(modelinfo._ready_sensevoice())
            finally:
                for p in reversed(patches):
                    p.stop()


if __name__ == "__main__":
    unittest.main()
