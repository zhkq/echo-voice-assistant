import os
import tempfile
import unittest

from app.netguard import is_loopback_host, is_loopback_origin
from app.pathutil import safe_under
from app.audio.wake import _syl_match


class PathSafetyTests(unittest.TestCase):
    def test_accepts_child_path(self):
        with tempfile.TemporaryDirectory() as root:
            expected = os.path.join(root, "asset.js")
            self.assertEqual(safe_under(root, "asset.js"), os.path.realpath(expected))

    def test_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "web")
            os.mkdir(root)
            self.assertIsNone(safe_under(root, "..", "secret.txt"))

    def test_rejects_sibling_with_same_prefix(self):
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "web")
            os.mkdir(root)
            self.assertIsNone(safe_under(root, "..", "web-backup", "secret.txt"))

    # 以下为 §13.8 安全网补充：这些边界是 P3 动路径层时最容易被"顺手改坏"的地方
    def test_accepts_nested_parts(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "sub"))
            self.assertEqual(safe_under(root, "sub", "f.txt"),
                             os.path.realpath(os.path.join(root, "sub", "f.txt")))

    def test_absolute_path_inside_root_is_allowed(self):
        """绝对路径只要落在根内就合法（os.path.join 会用后一个参数整段替换）。"""
        with tempfile.TemporaryDirectory() as root:
            inner = os.path.join(root, "sub")
            os.makedirs(inner)
            self.assertEqual(safe_under(root, inner), os.path.realpath(inner))

    def test_base_itself_is_rejected_unless_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(safe_under(root))
            self.assertEqual(safe_under(root, allow_base=True), os.path.realpath(root))

    def test_empty_part_resolves_to_base_and_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(safe_under(root, ""))

    @unittest.skipUnless(os.name == "nt", "跨盘符没有共同路径（ValueError）只在 Windows 上出现")
    def test_different_drive_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(safe_under(root, "D:\\echo-elsewhere\\x.txt"))


class LoopbackGuardTests(unittest.TestCase):
    def test_accepts_loopback_hosts(self):
        for host in ("127.0.0.1:8970", "localhost", "[::1]:8970"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))

    def test_rejects_non_loopback_hosts(self):
        for host in ("example.com", "192.168.1.10:8970", "", "127.0.0.1.evil.test"):
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))

    def test_origin_rules(self):
        self.assertTrue(is_loopback_origin("http://localhost:8970"))
        self.assertTrue(is_loopback_origin("https://[::1]"))
        self.assertFalse(is_loopback_origin("null"))
        self.assertFalse(is_loopback_origin("https://example.com"))


class WakeMatchingTests(unittest.TestCase):
    def test_single_insert_or_delete_is_tolerated(self):
        self.assertTrue(_syl_match("yuan", "yun"))
        self.assertTrue(_syl_match("abc", "axbc"))
        self.assertTrue(_syl_match("axbc", "abc"))
        self.assertFalse(_syl_match("abc", "axybc"))


if __name__ == "__main__":
    unittest.main()
