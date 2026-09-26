# -*- coding: utf-8 -*-
"""清理（`app/model_cleanup.py` + `/api/models/cleanup*`）的用例。

用户要求这一块"**默认只给建议，不自动删**；先预览 → 用户确认后才删；如实回报"。
所以用例的重点不是"能删掉"，而是**这些边界**：

* 预览**一个字节都不动盘**；
* 「保留」钉子生效（永久不列入建议）；
* **在用的一律不删**（当前配置选中的 / 本进程已加载的），并且拒绝时给的是**真原因**；
* 删除**覆盖两个缓存位置**（`models\\` 与 `~/.cache/modelscope/models`）；
* 释放的字节数**对得上账**；
* 删不动时**如实报**失败原因，不许吞成一句"失败"；
* `qwen3asr` 是**两个模型一个 item**（ASR + 强制对齐器）—— 同生共死，绝不留下半份。

测试隔离：临时库（`db.DATA_DIR/DB_FILE`）、临时模型目录（`modelinfo.models_dir`）、
临时 ModelScope 缓存（`modelinfo.MS_CACHE`）。**不碰真实的 models 目录与用户数据。**
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import app.db as db
from app import model_cleanup, model_usage, modelinfo


def _write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)
    return size


class _CleanupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-cleanup-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.models = os.path.join(self.tmp, "models")
        self.ms_cache = os.path.join(self.tmp, "mscache")
        os.makedirs(self.models)
        os.makedirs(self.ms_cache)

        self._patches = [
            patch.object(db, "DATA_DIR", self.tmp),
            patch.object(db, "DB_FILE", os.path.join(self.tmp, "echo.db")),
            patch.object(modelinfo, "models_dir", lambda: self.models),
            patch.object(modelinfo, "MS_CACHE", self.ms_cache),
            patch.object(model_usage, "ENABLED", True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        db.init()

    # ---- 造现场的小工具（形状与真实落点一致）----
    def _ms(self, repo, size=1000):
        return _write(os.path.join(self.ms_cache, repo.replace("/", "--"),
                                   "snapshots", "master", "model.bin"), size)

    def _local(self, *parts, size=1000):
        return _write(os.path.join(self.models, *parts), size)

    def _cache_settings(self, **values):
        from app import config as cfg
        return patch.object(cfg.settings, "_cache", dict(values))


class PreviewTests(_CleanupTestCase):
    def test_preview_does_not_touch_disk(self):
        """预览**只读**：跑完之后每个目录、每个文件都还在，一个字节都没变。"""
        self._ms("Systran/faster-whisper-tiny", 2048)
        before = []
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                p = os.path.join(root, name)
                before.append((p, os.path.getsize(p)))

        out = model_cleanup.preview(days=90)

        after = []
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                p = os.path.join(root, name)
                after.append((p, os.path.getsize(p)))
        self.assertEqual(sorted(before), sorted(after), "预览不许改盘")
        self.assertIn("whisper-tiny", [it["id"] for it in out["items"]])
        self.assertEqual(out["days"], 90)

    def test_retired_whisper_tiers_are_suggested_even_when_files_are_new(self):
        """whisper 三档：面板里已不再提供（退役），文件却是刚下的 —— 仍然该进建议。"""
        self._ms("Systran/faster-whisper-tiny", 75)
        self._ms("Systran/faster-whisper-base", 141)
        self._ms("Systran/faster-whisper-small", 464)
        out = model_cleanup.preview()
        rows = {it["id"]: it for it in out["items"]}
        for mid in ("whisper-tiny", "whisper-base", "whisper-small"):
            with self.subTest(mid=mid):
                self.assertTrue(rows[mid]["retired"], "whisper 档位应当被判成退役")
                self.assertTrue(rows[mid]["suggested"], "退役 + 无使用记录 = 建议清理")
                self.assertIn("退役", rows[mid]["reason"])
        self.assertEqual(set(out["suggested"]),
                         {"whisper-tiny", "whisper-base", "whisper-small"})

    def test_selected_engine_is_never_suggested(self):
        self._ms("Qwen/Qwen3-ASR-0.6B", 1800)
        self._ms("Qwen/Qwen3-ForcedAligner-0.6B", 1700)
        with self._cache_settings(sttModel="qwen3asr", meetingSttModel="qwen3asr"):
            out = model_cleanup.preview()
        row = {it["id"]: it for it in out["items"]}["qwen3asr"]
        self.assertFalse(row["suggested"])
        self.assertTrue(row["inUse"])
        self.assertIn("meetingSttModel", row["protectReason"])

    def test_never_used_with_fresh_files_is_not_suggested(self):
        """可选的模型 + 刚下到盘上 + 没有使用记录 → **先别动**（这是"别瞎删"的那条）。"""
        self._local("sherpa-onnx-streaming", "encoder.onnx", size=500)
        with self._cache_settings(sttModel="sensevoice", meetingSttModel="qwen3asr",
                                  wakeEnabled=False):
            out = model_cleanup.preview()
        row = {it["id"]: it for it in out["items"]}["sherpa"]
        self.assertFalse(row["suggested"])
        self.assertEqual(row["lastUsedBasis"], "file-time")
        self.assertIn("文件很新", row["reason"])

    def test_threshold_setting_is_honoured(self):
        """「近期」默认 90 天，阈值可配（`modelCleanupDays`）。"""
        self._local("sherpa-onnx-streaming", "encoder.onnx", size=500)
        model_usage.note_used("sherpa", when="2026-08-01 10:00:00")   # 约 56 天前
        self.assertEqual(model_cleanup.preview()["days"], model_cleanup.DEFAULT_DAYS)
        with self._cache_settings(modelCleanupDays=30):
            out30 = model_cleanup.preview()
        with self._cache_settings(modelCleanupDays=90):
            out90 = model_cleanup.preview()
        self.assertEqual(out30["days"], 30)
        self.assertIn("sherpa", out30["suggested"], "阈值 30 天时应当建议清理")
        self.assertNotIn("sherpa", out90["suggested"], "阈值 90 天时不该建议清理")

    def test_pin_keeps_it_out_of_suggestions(self):
        """「保留」钉子：打上就不再列入建议；摘掉又回来。"""
        self._ms("Systran/faster-whisper-tiny", 75)
        self.assertIn("whisper-tiny", model_cleanup.preview()["suggested"])
        self.assertTrue(model_cleanup.pin("whisper-tiny", True)["ok"])
        out = model_cleanup.preview()
        row = {it["id"]: it for it in out["items"]}["whisper-tiny"]
        self.assertNotIn("whisper-tiny", out["suggested"])
        self.assertTrue(row["pinned"])
        self.assertIn("保留", row["protectReason"])
        model_cleanup.pin("whisper-tiny", False)
        self.assertIn("whisper-tiny", model_cleanup.preview()["suggested"])


class ExecuteTests(_CleanupTestCase):
    def test_freed_bytes_match_the_files_that_were_there(self):
        size = self._ms("Systran/faster-whisper-tiny", 1234) + \
            self._local("faster-whisper", "tiny", "model.bin", size=2000)
        out = model_cleanup.execute(["whisper-tiny"])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["freedBytes"], size, "释放的字节数必须对得上")
        self.assertEqual(out["freedMb"], round(size / 1048576))
        self.assertEqual([r["id"] for r in out["removed"]], ["whisper-tiny"])
        self.assertFalse(os.path.isdir(os.path.join(self.ms_cache,
                                                    "Systran--faster-whisper-tiny")))
        self.assertFalse(os.path.isdir(os.path.join(self.models, "faster-whisper", "tiny")))

    def test_delete_covers_both_cache_locations(self):
        """两个缓存位置都要覆盖：`models\\` 与本机 ModelScope 缓存。"""
        one = 141 * 1024
        self._ms("Systran/faster-whisper-base", one)
        self._local("faster-whisper", "base", "model.bin", size=one)
        self._local("hub", "models--Systran--faster-whisper-base", "snapshots", "rev",
                    "model.bin", size=one)
        out = model_cleanup.execute(["whisper-base"])
        self.assertEqual(out["freedBytes"], one * 3)
        for gone in (os.path.join(self.ms_cache, "Systran--faster-whisper-base"),
                     os.path.join(self.models, "faster-whisper", "base"),
                     os.path.join(self.models, "hub",
                                  "models--Systran--faster-whisper-base")):
            self.assertFalse(os.path.exists(gone), "还留着 %s" % gone)

    def test_in_use_is_refused_and_the_files_stay(self):
        self._ms("Systran/faster-whisper-small", 464)
        with self._cache_settings(sttModel="small", meetingSttModel="qwen3asr"):
            out = model_cleanup.execute(["whisper-small"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["removed"], [])
        self.assertEqual(out["failed"][0]["id"], "whisper-small")
        self.assertIn("拒绝删除", out["failed"][0]["reason"])
        self.assertIn("sttModel", out["failed"][0]["reason"])
        self.assertTrue(os.path.isdir(os.path.join(self.ms_cache,
                                                   "Systran--faster-whisper-small")),
                        "在用的一律不删（文件必须还在）")

    def test_pinned_is_refused_with_the_real_reason(self):
        self._ms("Systran/faster-whisper-tiny", 75)
        model_usage.pin("whisper-tiny")
        out = model_cleanup.execute(["whisper-tiny"])
        self.assertIn("保留", out["failed"][0]["reason"])
        self.assertTrue(os.path.isdir(os.path.join(self.ms_cache,
                                                   "Systran--faster-whisper-tiny")))

    def test_failure_is_reported_with_the_real_reason(self):
        """删不动（权限/占用）时要报**真原因**，不许吞成一句失败。"""
        self._ms("Systran/faster-whisper-tiny", 75)
        with patch.object(model_cleanup.shutil, "rmtree",
                          side_effect=OSError("拒绝访问")):
            out = model_cleanup.execute(["whisper-tiny"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["freedBytes"], 0)
        self.assertIn("拒绝访问", out["failed"][0]["reason"])
        self.assertTrue(os.path.isdir(os.path.join(self.ms_cache,
                                                   "Systran--faster-whisper-tiny")))

    def test_unknown_and_empty_requests_are_refused(self):
        empty = model_cleanup.execute([])
        self.assertFalse(empty["ok"])
        self.assertIn("没有选中", empty["message"])
        unknown = model_cleanup.execute(["no-such-model"])
        self.assertFalse(unknown["ok"])
        self.assertIn("未知模型", unknown["failed"][0]["reason"])

    def test_ledger_row_is_dropped_after_deletion(self):
        self._ms("Systran/faster-whisper-tiny", 75)
        model_usage.note_used("whisper-tiny")
        model_cleanup.execute(["whisper-tiny"])
        self.assertEqual(db.model_usage("whisper-tiny"), None,
                         "模型删了，账本里不该留幽灵行")

    def test_qwen3asr_bundle_is_deleted_as_a_whole(self):
        """`qwen3asr` = ASR + 强制对齐器（两个模型一个 item）——**同生共死**。

        缺了强制对齐器整体就不算可用（时间戳一个都没有，2026-09-26 刚修好的判据），
        所以允许的只有两种结局：整项保留，或整项删除。绝不留下半份。
        """
        self._ms("Qwen/Qwen3-ASR-0.6B", 1800)
        self._ms("Qwen/Qwen3-ForcedAligner-0.6B", 1700)
        asr_dir = os.path.join(self.ms_cache, "Qwen--Qwen3-ASR-0.6B")
        ali_dir = os.path.join(self.ms_cache, "Qwen--Qwen3-ForcedAligner-0.6B")

        # ① 被选中 → 两个都不在建议里
        with self._cache_settings(meetingSttModel="qwen3asr"):
            row = {it["id"]: it for it in model_cleanup.preview()["items"]}["qwen3asr"]
        self.assertFalse(row["suggested"])
        self.assertEqual({p["path"] for p in row["paths"]}, {asr_dir, ali_dir},
                         "这一项的落点必须**同时**包含 ASR 与对齐器")

        # ② 没被选中且很久没用 → 建议里出现，删除时两个目录一起走
        model_usage.note_used("qwen3asr", when="2020-01-01 00:00:00")
        with self._cache_settings(meetingSttModel="sensevoice", sttModel="sherpa"):
            out = model_cleanup.execute(["qwen3asr"])
        self.assertTrue(out["ok"], out)
        self.assertFalse(os.path.exists(asr_dir))
        self.assertFalse(os.path.exists(ali_dir), "对齐器没跟着删 = 留下一份不可用的权重")

    def test_second_delete_is_reported_as_no_weights_left(self):
        """重复删同一个（已经删掉了）要说"本机没有它的权重"，而不是假成功。"""
        self._ms("Systran/faster-whisper-tiny", 75)
        self.assertTrue(model_cleanup.execute(["whisper-tiny"])["ok"])
        again = model_cleanup.execute(["whisper-tiny"])
        self.assertFalse(again["ok"])
        self.assertIn("没有它的权重", again["failed"][0]["reason"])

    def test_every_item_reports_the_dirs_it_really_occupies(self):
        """落点必须是**并集**：下载会下到的 + 面板量占用时看的（2026-09-26 实测两处漏网）。

        两个真实漏掉的例子：
          * `kws` 只在 `models/wakeword/kws-zh-en-3m`（`_watch_paths` 里根本没有它）——
            漏了就变成"面板说有 39 MB、清理说本机没有"；
          * `sensevoice` 既可能在 `models/sensevoice`（拷进来的）也可能在 ModelScope 缓存
            （下载的），只认一边就会留下半份权重。
        """
        self._local("wakeword", "kws-zh-en-3m", "encoder.onnx", size=1000)
        self._local("sensevoice", "model.pt", size=2000)
        self._ms("iic/SenseVoiceSmall", 3000)
        self._ms("Systran/faster-whisper-tiny", 500)
        self._local("hub", "models--Systran--faster-whisper-tiny", "snapshots", "r",
                    "model.bin", size=700)

        kws = modelinfo.model_paths("kws")
        self.assertTrue(any(p.endswith(os.path.join("wakeword", "kws-zh-en-3m")) for p in kws),
                        "kws 的落点漏了：%r" % (kws,))
        sv = modelinfo.model_paths("sensevoice")
        self.assertTrue(any(p.endswith("sensevoice") for p in sv), "sensevoice 本地目录漏了")
        self.assertTrue(any(p.endswith("iic--SenseVoiceSmall") for p in sv), "MS 缓存漏了")
        wh = modelinfo.model_paths("whisper-tiny")
        self.assertEqual(len(wh), 3, "whisper 三个落点都要在：%r" % (wh,))

        # 删 kws 要真的把它删掉（此前会报"本机没有它的权重"）
        with self._cache_settings(wakeEngine="sherpa", wakeEnabled=True, sttModel="sensevoice",
                                  meetingSttModel="qwen3asr",
                                  capabilityDiarizeBackend="echo-server",
                                  capabilityEmbedBackend="echo-server"):
            out = model_cleanup.execute(["kws"])
        self.assertTrue(out["ok"], out)
        self.assertFalse(os.path.isdir(os.path.join(self.models, "wakeword", "kws-zh-en-3m")))


if __name__ == "__main__":
    unittest.main()
