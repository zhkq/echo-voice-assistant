from pathlib import Path
import importlib.util
import tempfile
import unittest
from unittest.mock import patch

from scripts import install_pyannote as installer
from app import modelinfo


class PyannoteInstallTests(unittest.TestCase):
    def test_copy_only_catalog_rejects_background_download(self):
        """copy 类（没有稳定公开源）仍要拒绝后台下载，只给"从源机拷贝"的说明。

        ⚠ pyannote **2026-09-21 起不再是这一类**：ModelScope 上有同名仓库且匿名可下，
        所以它和别的引擎一样能一键下载（见下面 test_pyannote_is_now_downloadable）。
        """
        entry = modelinfo._by_id("kws")
        self.assertEqual("copy", entry["source"])
        with patch.object(modelinfo.threading, "Thread") as thread:
            ok, message = modelinfo.start_download("kws")
            self.assertFalse(ok)
            self.assertIn("拷贝", message)
            thread.assert_not_called()

    def test_pyannote_is_now_downloadable_from_modelscope(self):
        entry = modelinfo._by_id("pyannote")
        self.assertIsNot(entry.get("downloadable"), False,
                         "downloadable=False 会让面板与技能都下不了它")
        self.assertEqual("modelscope", entry["source"])
        self.assertTrue(modelinfo.PYANNOTE_ASSETS, "要有明确的文件映射")

    def test_empty_or_partial_directories_are_not_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "pyannote"
            for _, directory, filename in installer.ASSETS:
                path = root / directory / filename
                path.parent.mkdir(parents=True, exist_ok=True)
            with patch.object(modelinfo, "models_dir", lambda: folder):
                self.assertFalse(modelinfo.ready_pyannote())
                for _, directory, filename in installer.ASSETS:
                    (root / directory / filename).write_bytes(b"test model")
                self.assertTrue(modelinfo.ready_pyannote())
                (root / installer.ASSETS[-1][1] / installer.ASSETS[-1][2]).write_bytes(b"")
                self.assertFalse(modelinfo.ready_pyannote())

    def test_no_token_stops_before_network(self):
        """ModelScope 那条路不通时落到 HF；没有 Token 就必须**在联网前**停下并给出指引。"""
        with patch("modelscope.snapshot_download", side_effect=RuntimeError("ms down")), \
                patch("huggingface_hub.get_token", return_value=None), \
                patch.object(Path, "is_file", return_value=False), \
                patch("huggingface_hub.hf_hub_download") as download:
            with self.assertRaisesRegex(RuntimeError, "hf auth login"):
                installer.download()
            download.assert_not_called()

    def test_modelscope_is_tried_first(self):
        """默认走 ModelScope：不用 Token、不碰 HF 的证书闸门。"""
        if importlib.util.find_spec("modelscope") is None:      # pragma: no cover - CI 轻装
            self.skipTest("这个环境没装 modelscope")
        with patch("modelscope.snapshot_download") as ms, \
                patch("huggingface_hub.hf_hub_download") as hf, \
                patch.object(installer, "complete", return_value=True):
            installer.download()
        self.assertEqual(ms.call_count, len(installer.ASSETS))
        hf.assert_not_called()
        self.assertEqual(ms.call_args_list[0].args[0], installer.ASSETS[0][0])

    def test_hf_fallback_uses_the_official_endpoint(self):
        """ModelScope 失败才回落 HF，且必须打官方 endpoint（镜像另说）。"""
        with patch("modelscope.snapshot_download", side_effect=RuntimeError("ms down")), \
                patch("huggingface_hub.get_token", return_value="test-token"), \
                patch("huggingface_hub.hf_hub_download") as download, \
                patch.object(installer, "complete", return_value=True):
            installer.download()
        self.assertEqual(download.call_count, 4)
        for call, (repo, folder, filename) in zip(download.call_args_list, installer.ASSETS):
            self.assertEqual(call.kwargs["endpoint"], "https://huggingface.co")
            self.assertEqual(call.kwargs["repo_id"], repo)
            self.assertEqual(call.kwargs["filename"], filename)
            self.assertEqual(call.kwargs["local_dir"], str(installer.MODEL_ROOT / folder))


if __name__ == "__main__":
    unittest.main()
