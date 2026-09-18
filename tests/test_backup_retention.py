# -*- coding: utf-8 -*-
"""自动备份保留策略测试（app/llm_router.py 的 prune_backups）。

背景：ECHO 每次把模型组写进 `~/.dsh/settings.yaml` 或 `dsh-failover/config.json`
之前，都会先复制一份同名备份，但历史上**从不清理**：2026-09-18 实测 `~/.dsh`
堆了 100 份 `settings.yaml.bak-echo-auto-*`、两棵树的 `dsh-failover` 各 18 份
`config.json.bak-*`，其中一份 `.credentials.yaml.bak-echo-auto-*` 还长期保留着
内网网关令牌的旧明文副本 —— 备份从"保险"变成了"泄漏面"。

这里把"只保留最近 N 份"的语义钉住：

  1. 超过 `BACKUP_KEEP` 份时删掉最旧的，保留**最新**的 N 份（按 mtime，不按名字，
     因为历史上存在 `.bak-maxtokens-*` 这类人工命名）；
  2. 不足 N 份时一份都不删；
  3. 只动自己那个前缀的备份，同目录其它文件、别的前缀的备份都不受影响；
  4. 当前生效的那个文件永远不会被删。

用临时目录构造，绝不碰真实的 `~/.dsh`。
"""
import os
import tempfile
import time
import unittest
from pathlib import Path

from app import llm_router as lr


def seed_backups(folder: Path, name: str, prefix: str, count: int):
    """造 count 份备份，mtime 递增（越靠后越新），返回按新旧排序的路径列表。"""
    made = []
    base = time.time() - 100000
    for i in range(count):
        p = folder / ("%s%s2026010%d-0000%02d" % (name, prefix, i, i))
        p.write_text("v%d" % i, encoding="utf-8")
        stamp = base + i * 60
        os.utime(p, (stamp, stamp))
        made.append(p)
    return made


class BackupRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="echo-bak-"))
        self.live = self.tmp / "settings.yaml"
        self.live.write_text("current", encoding="utf-8")

    def test_keeps_newest_n_and_deletes_oldest(self):
        made = seed_backups(self.tmp, "settings.yaml", ".bak-echo-auto-", 8)
        lr.prune_backups(self.live, ".bak-echo-auto-")

        left = sorted(p.name for p in self.tmp.glob("settings.yaml.bak-echo-auto-*"))
        self.assertEqual(len(left), lr.BACKUP_KEEP,
                         "应只保留 %d 份，实际 %d 份" % (lr.BACKUP_KEEP, len(left)))
        expected = sorted(p.name for p in made[len(made) - lr.BACKUP_KEEP:])
        self.assertEqual(left, expected, "留下的必须是最新的 N 份，不能删错方向")
        self.assertTrue(self.live.is_file(), "当前生效的 settings.yaml 绝不能被删")

    def test_under_limit_deletes_nothing(self):
        seed_backups(self.tmp, "settings.yaml", ".bak-echo-auto-", lr.BACKUP_KEEP - 2)
        lr.prune_backups(self.live, ".bak-echo-auto-")
        left = list(self.tmp.glob("settings.yaml.bak-echo-auto-*"))
        self.assertEqual(len(left), lr.BACKUP_KEEP - 2, "未超限时不应删任何备份")

    def test_only_matching_prefix_is_touched(self):
        seed_backups(self.tmp, "settings.yaml", ".bak-echo-auto-", 7)
        bystanders = [
            self.tmp / "settings.yaml.bak-other-20260101-000000",
            self.tmp / "other.yaml.bak-echo-auto-20260101-000000",
            self.tmp / "config.json.bak-20260101-000000",
            self.tmp / "notes.txt",
        ]
        for b in bystanders:
            b.write_text("keep me", encoding="utf-8")

        lr.prune_backups(self.live, ".bak-echo-auto-")
        for b in bystanders:
            self.assertTrue(b.is_file(), "误删了不相干的文件：%s" % b.name)

    def test_backup_helper_creates_one_and_prunes(self):
        """_backup 每次写盘前留一份，同时把总量压回上限。"""
        seed_backups(self.tmp, "settings.yaml", ".bak-echo-auto-", lr.BACKUP_KEEP + 3)
        lr._backup(self.live)

        left = sorted(self.tmp.glob("settings.yaml.bak-echo-auto-*"),
                      key=lambda p: p.stat().st_mtime)
        self.assertEqual(len(left), lr.BACKUP_KEEP,
                         "_backup 之后应仍只有 %d 份" % lr.BACKUP_KEEP)
        self.assertIn("settings.yaml.bak-echo-auto-", left[-1].name,
                      "刚写的那一份必须是最新的、不能被自己裁掉")


if __name__ == "__main__":
    unittest.main()
