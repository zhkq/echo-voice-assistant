# -*- coding: utf-8 -*-
"""真实库夹具 + 升级不丢数据的 characterization 测试（§13.8 安全网第 3 项，P3 前置）

要解决什么
----------
2.0 会把数据根改成**分平台**（D18：macOS = `~/Library/Application Support/ECHO`），
还可能引入新的 schema 版本。最要命的失败模式是"升级后老库读不出来/声纹样本没了"。
所以：

  1. `tests/fixtures/echo-1x-structure.json` —— **只含结构**（表/列/index 签名 +
     schema_version），从真实 1.x 库 `C:\\echo1.0\\data\\echo.db` 只读抓下来。
     结构里没有会议正文、没有声纹嵌入、没有任何个人数据，可以入库；
  2. `SchemaSnapshotTests` —— 代码建出来的库，结构必须与真实 1.x 库**逐列相同**。
     两份链表一旦分叉（比如有人给 meetings 改列），这里立刻红；
  3. `V1UpgradeTests` —— 用**代码里的 v1 建表语句**造一个 1.x 形状的库、塞入合成数据，
     再跑完整 `db.init()`：旧数据一条不少、老的明文 API 密钥迁移后**仍是同一把钥匙**；
  4. `RealDatabaseOptInTests` —— 只有显式给 `ECHO_REAL_DB`（**指向一份副本**）时才跑：
     对真实库做"升级前 vs 升级后"的全表计数比对。默认跳过，因为它依赖本机文件。

⚠️ 永远不要把真实 `echo.db` 提交进仓库：里面有会议转写全文与声纹（生物特征）。
   本文件的 fixture 是**结构快照**；要跑真库验证，请先复制一份副本再设 `ECHO_REAL_DB`：

       copy C:\\echo1.0\\data\\echo.db %TEMP%\\echo-real.db
       set ECHO_REAL_DB=%TEMP%\\echo-real.db
       python -m unittest tests.test_db_upgrade
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "echo-1x-structure.json")

#: 1.x 最后一个 schema 版本（= 真库里的 meta.schema_version）
V1X_SCHEMA_VERSION = 4

#: 代码当前的 schema 版本（跟着 MIGRATIONS 走，别写死 —— 2.0 会继续加版本）
CURRENT_SCHEMA_VERSION = max(v for v, _ in db.MIGRATIONS)

#: 1.x **之后有意新增**的列（每次加列都要在这里登记：哪张表 + 哪个列 + 为什么）。
#: 结构快照测试的用意是"抓到手滑改旧表"，不是"永远不许演进" —— 所以有意的演进登记在案，
#: 其余任何列/表的分叉仍然会红。
#:   * dsh_sessions.agent / meeting_sessions.agent（v5，2026-09-19）：会话归属哪个智能体后端，
#:     换后端（DSH Desktop ↔ 独立 harness ↔ CodeBuddy）后旧会话必须失效 —— 详见 app/db.py
#:     的 _migrate_session_owner 与 PROGRESS §47。
#:   * meetings.error（v6，2026-09-25）：这一场**为什么失败**的人话（面板直接显示）。
#:     在此之前多处只写 status=error、原因一个字都不落库，面板只能自己编
#:     "麦克风没打开" —— 而真实原因是转写引擎驱动不了。用 notes 装不行（那是用户的地盘）。
POST_1X_COLUMNS = {
    ("dsh_sessions", "agent"),
    ("meeting_sessions", "agent"),
    ("meetings", "error"),
}

#: 1.x **之后有意新增的表**（每加一张都在这里登记：哪张表 + 为什么）。
#: 结构快照测试的用意是"抓到改旧表"，不是"永远不许加表" —— 但新表必须是有意的：
#:   * model_usage（v7，2026-09-26）：本地模型的"用没用过"账本（上次使用 / 次数 /
#:     「保留」钉子）—— 清理"近期没再使用的模型"的**唯一判据**；在那之前只能看文件
#:     修改时间，那等于瞎删。见 app/model_usage.py 与 app/model_cleanup.py。
POST_1X_TABLES = {"model_usage"}


def _without_post_1x_columns(tables):
    """把"1.x 之后有意新增的表与列"从结构里摘掉，剩下的应当与 1.x 真库逐列相同。"""
    out = {}
    for table, cols in tables.items():
        if table in POST_1X_TABLES:
            continue
        drop = {col for (t, col) in POST_1X_COLUMNS if t == table}
        out[table] = [c for c in cols if c[0] not in drop]
    return out


# ---------------------------------------------------------------- 结构抓取

def _table_names(conn):
    return sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall())


def capture_structure(db_path):
    """把一个库的**结构签名**抓成可比较的 dict（不含任何行数据）。"""
    conn = sqlite3.connect(db_path)
    try:
        ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        out = {"schema_version": ver[0] if ver else None, "tables": {}, "indexes": {}}
        for name in _table_names(conn):
            cols = conn.execute("PRAGMA table_info(%s)" % name).fetchall()
            out["tables"][name] = [
                [c[1], (c[2] or "").upper(), int(c[3]), c[4], int(c[5])] for c in cols]
        for r in conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            out["indexes"][r[0]] = r[1]
        return out
    finally:
        conn.close()


def _row_counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {name: conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
                for name in _table_names(conn)}
    finally:
        conn.close()


class _TempDbTestCase(unittest.TestCase):
    """给用例一套临时目录 + 可用的 db 模块路径。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-db-upgrade-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_file = os.path.join(self.tmp, "echo.db")
        self._p1 = patch.object(db, "DATA_DIR", self.tmp)
        self._p2 = patch.object(db, "DB_FILE", self.db_file)
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)


# ---------------------------------------------------------------- ① 结构快照

class SchemaSnapshotTests(_TempDbTestCase):
    """代码建出来的库 == 真实 1.x 库的结构（防"改列没迁移"）。"""

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, encoding="utf-8") as fh:
            cls.expected = json.load(fh)

    def test_fixture_contains_structure_only(self):
        """夹具里不许出现数据（防止有人把真库内容误抓进来）。"""
        self.assertEqual(set(self.expected), {"schema_version", "tables", "indexes", "note"})
        self.assertNotIn("rows", self.expected)
        # 结构里只该有列名/类型/默认值/index 名这类短串；出现长串就说明混进了数据
        structure = json.dumps({k: v for k, v in self.expected.items() if k != "note"},
                               ensure_ascii=False)
        longest = max((len(s) for s in re.findall(r'"([^"]*)"', structure)), default=0)
        self.assertLess(longest, 60, "结构快照里出现了异常长的字符串，检查是否混入数据")
        self.assertNotIn("2026-", structure, "结构快照里不该有会议目录名/时间戳")
        self.assertLess(len(structure), 20000, "结构快照应该很小；变大说明混进了数据")

    def test_fresh_database_matches_the_real_1x_structure(self):
        db.init()
        actual = capture_structure(self.db_file)
        # 有意新增的列先摘掉再比；同时要求它们**确实存在**（免得登记表烂掉）
        for table, col in sorted(POST_1X_COLUMNS):
            self.assertIn(col, [c[0] for c in actual["tables"].get(table, [])],
                          "登记了 %s.%s 是 1.x 之后新增的列，但代码里没有它" % (table, col))
        for table in sorted(POST_1X_TABLES):
            self.assertIn(table, actual["tables"],
                          "登记了 %s 是 1.x 之后新增的表，但代码里没有它" % table)
        self.assertEqual(_without_post_1x_columns(actual["tables"]), self.expected["tables"],
                         "表/列定义与真实 1.x 库分叉了（要 append 迁移，别改旧表；"
                         "确实是有意新增的列/表就登记到 POST_1X_COLUMNS / POST_1X_TABLES）")
        self.assertEqual(actual["indexes"], self.expected["indexes"])

    def test_schema_version_matches(self):
        db.init()
        fresh = capture_structure(self.db_file)["schema_version"]
        self.assertEqual(int(fresh), CURRENT_SCHEMA_VERSION,
                         "新建库应当是代码当前版本（%d）" % CURRENT_SCHEMA_VERSION)
        self.assertLessEqual(int(self.expected["schema_version"]), int(fresh),
                             "1.x 夹具的版本不该比代码还新")


# ---------------------------------------------------------------- ② 升级路径

class V1UpgradeTests(_TempDbTestCase):
    """老库（v1 建表 + schema_version=1 的明文 api_keys）走完整迁移链。"""

    PLAINTEXT_TOKEN = "echo_0123456789abcdef0123456789abcdef0123456789abcdef"

    #: 1.x **早期**的 api_keys 形态（明文 token）。这个建表语句已经从
    #: `MIGRATIONS[0]` 里消失了（现在直接是新形态 token_hash），但真实老库里就是它——
    #: `_migrate_api_keys_hash` 专门处理"库里还留着 token 列"的情况。这里如实复刻。
    LEGACY_API_KEYS_V1 = """
    CREATE TABLE api_keys (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      name         TEXT DEFAULT '',
      token        TEXT NOT NULL UNIQUE,
      scopes       TEXT DEFAULT '["read"]',
      enabled      INTEGER DEFAULT 1,
      created_at   TEXT DEFAULT (datetime('now','localtime')),
      last_used_at TEXT DEFAULT ''
    );
    """

    def _build_v1_database(self):
        """用**代码里的 v1 建表语句**造一个 1.x 形状的库，再塞入合成数据。

        为什么用代码里的语句：它们就是当年写进真实库的那份 SQL（见结构快照测试），
        这样"造出来的老库"与真库同形，又完全不含个人数据。
        """
        conn = sqlite3.connect(self.db_file)
        try:
            # 先建**老的** api_keys（明文形态）：MIGRATIONS[0] 是 CREATE TABLE IF NOT EXISTS，
            # 所以随后跑它不会覆盖这张表 —— 正好复刻"老库里已经是明文形态"的现场。
            conn.executescript(self.LEGACY_API_KEYS_V1)
            conn.executescript(db.MIGRATIONS[0][1])
            conn.execute("INSERT INTO meta(key,value) VALUES('schema_version','1')")
            # 明文 api_keys（早期 1.x 的形态；v2 迁移要把它哈希掉）
            conn.execute("INSERT INTO api_keys(name,token,scopes,enabled) VALUES(?,?,?,1)",
                         ("mobile", self.PLAINTEXT_TOKEN, '["read"]'))
            conn.execute("INSERT INTO meetings(name,title,started_at,ended_at,status,segments) "
                         "VALUES('2026-09-01_10-00-00','周会','2026-09-01T10:00:00',"
                         "'2026-09-01T10:30:00','done',1)")
            mid = conn.execute("SELECT id FROM meetings").fetchone()[0]
            conn.execute("INSERT INTO speakers(meeting_id,label,name) VALUES(?,?,?)",
                         (mid, "S1", "张三"))
            conn.execute("INSERT INTO lines(meeting_id,seg_index,start,end,speaker_label,text) "
                         "VALUES(?,1,0.0,2.5,'S1','你好，开始吧')", (mid,))
            conn.execute("INSERT INTO lines(meeting_id,seg_index,start,end,speaker_label,text) "
                         "VALUES(?,1,2.5,5.0,'S1','嗯，我先把进度说一下')", (mid,))
            conn.execute("INSERT INTO settings(key,value,grp,label) VALUES('serverPort','18060',"
                         "'general','ECHO 面板端口')")
            conn.execute("INSERT INTO commands(source,text,status) VALUES('hotkey','现在几点了？','done')")
            conn.execute("INSERT INTO logs(level,source,message) VALUES('info','meeting','开始录音')")
            conn.commit()
        finally:
            conn.close()
        return mid

    def test_v1_database_is_migrated_to_current_version(self):
        self._build_v1_database()
        db.init()
        conn = sqlite3.connect(self.db_file)
        try:
            ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(ver), CURRENT_SCHEMA_VERSION,
                         "老库要升到代码当前版本")
        with open(FIXTURE, encoding="utf-8") as fh:
            expected_tables = json.load(fh)["tables"]
        self.assertEqual(_without_post_1x_columns(capture_structure(self.db_file)["tables"]),
                         expected_tables,
                         "升级后的表结构与 1.x 逐列相同（有意新增的列除外）")

    def test_old_rows_survive_the_migration(self):
        mid = self._build_v1_database()
        db.init()
        meeting = db.get_meeting(mid)
        self.assertIsNotNone(meeting)
        self.assertEqual(meeting["name"], "2026-09-01_10-00-00")
        self.assertEqual(meeting["status"], "done")
        self.assertEqual(len(db.get_lines(mid)), 2, "转写行一条都不能少")
        self.assertEqual([s["name"] for s in db.get_speakers(mid)], ["张三"])
        self.assertEqual(db.get_setting("serverPort"), 18060, "用户改过的配置值不许被覆盖")
        self.assertEqual(db.count_commands(), 1)

    def test_plaintext_api_key_still_works_after_hashing(self):
        """v1→v2 的迁移承诺：同一把钥匙还能用，只是库里不再存明文。"""
        self._build_v1_database()
        db.init()
        conn = sqlite3.connect(self.db_file)
        try:
            stored = conn.execute("SELECT token_hash FROM api_keys").fetchone()[0]
        finally:
            conn.close()
        self.assertNotEqual(stored, self.PLAINTEXT_TOKEN, "明文不许留在库里")
        self.assertIsNotNone(db.verify_api_key(self.PLAINTEXT_TOKEN), "老客户端不用换钥匙")

    def test_new_2x_tables_exist_after_upgrade(self):
        self._build_v1_database()
        db.init()
        conn = sqlite3.connect(self.db_file)
        try:
            names = set(_table_names(conn))
        finally:
            conn.close()
        for table in ("meeting_sessions", "voiceprints", "speaker_embeddings",
                      "model_usage"):
            with self.subTest(table=table):
                self.assertIn(table, names)

    def test_init_is_idempotent_on_an_upgraded_database(self):
        """升级后重复 init（每次启动都会跑）不能动数据 —— 这是最常见的自伤方式。"""
        mid = self._build_v1_database()
        db.init()
        before = _row_counts(self.db_file)
        db.init()
        db.init()
        self.assertEqual(_row_counts(self.db_file), before)
        self.assertEqual(len(db.get_lines(mid)), 2)


# ---------------------------------------------------------------- ③ 合成"升级后"数据

class Synthetic2xDataTests(_TempDbTestCase):
    """新库上的读写往返：声纹/说话人嵌入这类 BLOB 最容易在迁移里被写坏。"""

    def test_voiceprint_blob_roundtrip(self):
        db.init()
        blob = bytes(range(256)) * 4          # 1024 字节，够像一条 256 维 float32 嵌入
        db.replace_voiceprint_sample("张三", blob, dim=256,
                                     meeting_name="2026-09-01_10-00-00", source_label="S1")
        db.replace_voiceprint_sample("李四", blob, dim=256,
                                     meeting_name="2026-09-02_10-00-00", source_label="S1")
        self.assertEqual(db.count_voiceprints(), 2)
        self.assertEqual(db.count_voiceprint_contacts(), 2)
        rows = db.get_voiceprint_samples()
        self.assertEqual([r["embedding"] for r in rows], [blob, blob])

    def test_same_meeting_same_speaker_replaces_instead_of_duplicating(self):
        db.init()
        blob = b"\x01" * 64
        db.replace_voiceprint_sample("张三", blob, meeting_name="m1", source_label="S1")
        db.replace_voiceprint_sample("张三改名", blob, meeting_name="m1", source_label="S1")
        self.assertEqual(db.count_voiceprints(), 1)
        self.assertEqual(db.list_voiceprints()[0]["name"], "张三改名")

    def test_deleting_a_meeting_keeps_the_voiceprint_library(self):
        """刻意设计：删历史会议不该连带丢掉联系人声纹（弱关联）。"""
        db.init()
        mid = db.create_meeting("2026-09-03_10-00-00")
        db.add_lines(mid, [(1, 0.0, 1.0, "S1", "你好")])
        db.replace_voiceprint_sample("张三", b"\x02" * 64, meeting_name="2026-09-03_10-00-00",
                                     source_label="S1")
        db.delete_meeting(mid)
        self.assertEqual(len(db.get_lines(mid)), 0, "级联删除转写行")
        self.assertEqual(db.count_voiceprints(), 1, "声纹库必须留着")

    def test_clear_meeting_lines_also_clears_speaker_embeddings(self):
        """重转写前清场：说话人编号会整体变化，旧嵌入留着会认错人。"""
        db.init()
        mid = db.create_meeting("2026-09-04_10-00-00")
        db.add_lines(mid, [(1, 0.0, 1.0, "S1", "你好")])
        db.replace_speaker_embeddings(mid, {"S1": (b"\x03" * 64, 256, 3)})
        self.assertEqual(list(db.get_speaker_embeddings(mid)), ["S1"])
        db.clear_meeting_lines(mid)
        self.assertEqual(db.get_speaker_embeddings(mid), {})
        self.assertEqual(db.get_speakers(mid), [])


# ---------------------------------------------------------------- ④ 真实库（opt-in）

REAL_DB = os.environ.get("ECHO_REAL_DB", "")


@unittest.skipUnless(REAL_DB and os.path.isfile(REAL_DB),
                     "设置 ECHO_REAL_DB=<真实库的副本> 后运行（见本文件头部说明）")
class RealDatabaseOptInTests(unittest.TestCase):
    """对真实库副本做"升级前 vs 升级后"比对。默认跳过。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-real-db-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.copy = os.path.join(self.tmp, "echo.db")
        shutil.copy2(REAL_DB, self.copy)
        self.before = _row_counts(self.copy)
        self.meetings = self._meeting_ids(self.copy)
        self._p1 = patch.object(db, "DATA_DIR", self.tmp)
        self._p2 = patch.object(db, "DB_FILE", self.copy)
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)

    @staticmethod
    def _meeting_ids(path):
        conn = sqlite3.connect(path)
        try:
            return [r[0] for r in conn.execute("SELECT id FROM meetings ORDER BY id")]
        finally:
            conn.close()

    def test_structure_unchanged_by_init(self):
        structure_before = capture_structure(self.copy)
        db.init()
        self.assertEqual(capture_structure(self.copy), structure_before)

    def test_every_row_count_is_preserved(self):
        db.init()
        self.assertEqual(_row_counts(self.copy), self.before)

    def test_every_meeting_is_still_readable(self):
        db.init()
        for mid in self.meetings:
            with self.subTest(meeting=mid):
                self.assertIsNotNone(db.get_meeting(mid))
        self.assertEqual(db.count_commands(), self.before.get("commands", 0))
        self.assertEqual(db.count_voiceprints(), self.before.get("voiceprints", 0))

    def test_voiceprint_count_matches_the_fixture_baseline(self):
        db.init()
        self.assertEqual(db.count_voiceprints(), self.before.get("voiceprints", 0))
        self.assertEqual(len(db.list_voiceprints()), self.before.get("voiceprints", 0))


# ---------------------------------------------------------------- 夹具再生成

if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--capture":
        src, dst = sys.argv[2], sys.argv[3]
        data = capture_structure(src)
        data["note"] = ("从真实 1.x 库只读抓取的**结构**快照（表/列/index/schema_version），"
                        "不含任何行数据。重新生成："
                        "python tests/test_db_upgrade.py --capture <echo.db> <out.json>")
        with open(dst, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        print("captured %d tables from %s -> %s" % (len(data["tables"]), src, dst))
    else:
        unittest.main()
