import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import modelinfo


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
