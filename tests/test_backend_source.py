# -*- coding: utf-8 -*-
"""后端源码同步（`app/backend_source.py`，2026-10-06）。

**为什么要它**（用户实测暴露的部署缺口）：ECHO 的能力后端跑的是**部署副本**
（`{backend}/server/`，来自薄包构建那一刻的快照），所以在仓库里改后端代码
**不会**自动生效。实测差距：`auth.py`/`store.py`/`ops.py`/`localpair.py`/`admin.py`
5 个文件全是当天改的，一个都没进过运行中的进程 —— 症状是"明明修好了却还是老样子"，
而证据全指向仓库那份（我据此误判过一轮 `last_seen`）。

这几条用例钉的是**边界**，不是实现细节：
  * 装好的机器（没有源码树）→ **跳过**，绝不自己覆盖自己；
  * 薄包还没解开（部署副本没有 `server/`）→ **跳过**，那是 `ensure_package` 的活；
  * 没有差异 → **不备份、不写盘**（否则每次启动都堆一份）；
  * 有差异 → 覆盖过去、**先备份**、返回拷了哪些；
  * 只同步 `server/`，不碰 `app/`。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backend_source  # noqa: E402


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-src-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        self.backend = os.path.join(self.tmp, "backend")
        os.makedirs(os.path.join(self.repo, "server"))
        os.makedirs(os.path.join(self.repo, "app"))
        os.makedirs(os.path.join(self.backend, "server"))

    def _write(self, root, rel, text):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def test_a_finished_install_without_a_source_tree_is_skipped(self):
        """装好的机器上没有源码树 → **跳过**（这正是"不该同步"的正常情形）。

        不跳的话就会拿"不存在的 src"去做事，或者在更糟的实现里把部署副本
        当成源码再抄一遍 —— 前者报错、后者把代码搞乱。
        """
        other = os.path.join(self.tmp, "no-such-repo")
        info = backend_source.plan(self.backend, source_dir=other)
        self.assertEqual(info["changed"], [])
        self.assertIn("没有源码树", info["reason"])
        res = backend_source.sync(self.backend, source_dir=other)
        self.assertTrue(res["ok"])
        self.assertEqual(res["copied"], [])

    def test_a_backend_without_server_dir_is_skipped(self):
        """薄包还没解开（部署副本没有 `server/`）→ 跳过，那是 `ensure_package` 的活。"""
        empty_backend = os.path.join(self.tmp, "fresh")
        os.makedirs(empty_backend)
        self._write(self.repo, "server/x.py", "print(1)\n")
        info = backend_source.plan(empty_backend, source_dir=self.repo)
        self.assertEqual(info["changed"], [])
        self.assertIn("薄包未解", info["reason"])

    def test_no_difference_means_no_backup_and_no_write(self):
        """完全一致时**连备份都不做** —— 否则每次启动都堆一份 `server.bak-*`。"""
        self._write(self.repo, "server/a.py", "same\n")
        self._write(self.backend, "server/a.py", "same\n")
        info = backend_source.plan(self.backend, source_dir=self.repo)
        self.assertEqual(info["changed"], [])
        self.assertIn("已是最新", info["reason"])
        res = backend_source.sync(self.backend, source_dir=self.repo)
        self.assertEqual(res["copied"], [])
        baks = [d for d in os.listdir(self.backend) if d.startswith("server.bak-")]
        self.assertEqual(baks, [], "没有差异却建了备份")

    def test_newer_source_files_are_copied_with_a_backup(self):
        """有差异 → 覆盖 + **先备份** + 报出拷了哪些。"""
        self._write(self.repo, "server/a.py", "NEW\n")
        self._write(self.repo, "server/sub/b.py", "NEW-B\n")
        self._write(self.backend, "server/a.py", "old\n")
        self._write(self.backend, "server/gone.py", "deploy-only\n{")
        info = backend_source.plan(self.backend, source_dir=self.repo)
        self.assertEqual(sorted(info["changed"]), ["a.py", "sub/b.py"],
                         "只该报差异文件（新增的 b.py 也算）")

        res = backend_source.sync(self.backend, source_dir=self.repo)
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted(res["copied"]), ["a.py", "sub/b.py"])
        with open(os.path.join(self.backend, "server", "a.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "NEW\n")
        # 备份里有旧内容
        baks = [d for d in os.listdir(self.backend) if d.startswith("server.bak-")]
        self.assertEqual(len(baks), 1, "同步前没有备份：%s" % baks)
        with open(os.path.join(self.backend, baks[0], "a.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "old\n", "备份不是同步前的样子")

    def test_only_the_server_tree_is_synced(self):
        """只同步 `server/`，**不碰 `app/`**（客户端本来就从自己的树跑）。"""
        self.assertEqual(backend_source.SYNC_ITEMS, ("server",))
        self._write(self.repo, "app/client.py", "REPO-CLIENT\n")
        self._write(self.backend, "app/client.py", "DEPLOYED-CLIENT\n")
        os.makedirs(os.path.join(self.backend, "app"), exist_ok=True)
        self._write(self.backend, "app/client.py", "DEPLOYED-CLIENT\n")
        backend_source.sync(self.backend, source_dir=self.repo)
        with open(os.path.join(self.backend, "app", "client.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "DEPLOYED-CLIENT\n", "app/ 被同步了")

    def test_pycache_is_not_synced(self):
        """`__pycache__` 不同步（口径与出包脚本的 `EXCLUDE_DIRS_TREE` 一致）。"""
        self._write(self.repo, "server/__pycache__/a.cpython-311.pyc", "junk")
        self._write(self.repo, "server/keep.py", "KEEP\n")
        self._write(self.backend, "server/keep.py", "old\n")
        res = backend_source.sync(self.backend, source_dir=self.repo)
        self.assertEqual(res["copied"], ["keep.py"], "把 __pycache__ 也同步了")
