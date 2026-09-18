from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import install_pyannote as installer
from app import modelinfo


class PyannoteInstallTests(unittest.TestCase):
    def test_copy_only_catalog_rejects_background_download(self):
        entry = modelinfo._by_id("pyannote")
        self.assertFalse(entry["downloadable"])
        self.assertIn("download", entry["cmd"])
        with patch.object(modelinfo.threading, "Thread") as thread:
            ok, message = modelinfo.start_download("pyannote")
            self.assertFalse(ok)
            self.assertIn("复制下载命令", message)
            thread.assert_not_called()

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
        with patch("huggingface_hub.get_token", return_value=None), \
                patch.object(Path, "is_file", return_value=False), \
                patch("huggingface_hub.hf_hub_download") as download:
            with self.assertRaisesRegex(RuntimeError, "hf auth login"):
                installer.download()
            download.assert_not_called()

    def test_official_endpoint_and_local_mapping(self):
        with patch("huggingface_hub.get_token", return_value="test-token"), \
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
