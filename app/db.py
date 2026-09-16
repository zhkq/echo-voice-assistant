# -*- coding: utf-8 -*-
"""db.py — ECHO 统一数据层（SQLite，单库 data/echo.db）

设计原则（区别于早期实现的渐进式堆叠）：
  * 单一事实来源：配置、命令历史、DSH 会话、会议、说话人、转写行、
    纪要记录、组件状态、日志、事件、API 密钥全部在同一个库。
  * WAL 模式 + 每次操作短连接，Windows 下稳定且支持多线程读写。
  * 版本化迁移：schema_version 记录当前版本，MIGRATIONS 顺序执行，
    未来加字段/加表只需追加一条迁移。
  * 时间统一用本地时间 ISO 字符串（datetime('now','localtime')）。
  * 所有写接口幂等、可重入；行级数据带 updated_at 便于未来同步/审计。

表概览：
  meta              schema 版本等元信息
  settings          配置（键值 + 面板渲染元数据：分组/类型/选项）
  commands          命令历史（生命周期：pending→sent→running→done/failed）
  dsh_sessions      ECHO 登记的 DSH 会话（命令会话/纪要会话/聊天会话）
  meetings          会议（一场一行，文件夹/时长/状态/转写配置）
  speakers          会议说话人（可改名/合并，UNIQUE(meeting_id,label)）
  speaker_embeddings 会议说话人的平均声纹（转写时留存；改名入库/「识别本场」据此）
  voiceprints       常用联系人声纹库（联系人名 + 说话人嵌入样本）
  lines             转写行（句级时间戳、说话人、修订标记；kind 预留扩展）
  summary_runs      纪要生成记录（可追加要求重新生成）
  component_states  组件运行状态快照（面板轮询展示）
  logs              运行日志（面板可查，替代散落 .log 文件）
  events            事件流（审计/统计/未来移动端推送）
  api_keys          外部触点（手机 App 等）的访问密钥（预留，默认关闭）
"""
import hashlib
import hmac
import json
import os
import sqlite3
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(BASE_DIR), "data")
DB_FILE = os.path.join(DATA_DIR, "echo.db")


def _hash_token(token: str) -> str:
    """API 密钥的存储形态：sha256(token)。

    为什么 sha256 就够：token 是 `secrets.token_hex(24)` = 192 位随机，没有字典/爆破空间，
    慢哈希（bcrypt/argon2）只会给每个带鉴权的请求白白加延迟。这里要防的是"库被读走后
    直接拿到可用凭据"，而不是猜出低熵口令。
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _migrate_api_keys_hash(conn):
    """v1 → v2：api_keys 由「明文 token」改为「sha256(token)」存储（2026-09-13 MEDIUM-2）。

    老库里已经发出去的 token 会被就地哈希后**原样保留**（还是同一把钥匙，客户端不用换），
    但明文不再留在库里。SQLite 没有 sha256 函数，所以这一步只能放在 Python 里做。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "token" not in cols:
        return                      # 已经是新结构（新库直接就是 token_hash）
    old = conn.execute(
        "SELECT id,name,token,scopes,enabled,created_at,last_used_at FROM api_keys").fetchall()
    conn.execute("DROP TABLE api_keys")
    conn.execute("""
    CREATE TABLE api_keys (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      name         TEXT DEFAULT '',
      token_hash   TEXT NOT NULL UNIQUE,
      scopes       TEXT DEFAULT '["read"]',
      enabled      INTEGER DEFAULT 1,
      created_at   TEXT DEFAULT (datetime('now','localtime')),
      last_used_at TEXT DEFAULT ''
    )""")
    for r in old:
        if r["token"]:
            conn.execute(
                "INSERT INTO api_keys(id,name,token_hash,scopes,enabled,created_at,last_used_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (r["id"], r["name"], _hash_token(r["token"]), r["scopes"],
                 r["enabled"], r["created_at"], r["last_used_at"]))
    if old:
        print(f"[db] api_keys 迁移完成：{len(old)} 把密钥从明文改为 sha256 存储")


# (version, sql) —— 顺序执行；新变更 append 即可
MIGRATIONS = [
    (1, """
    CREATE TABLE IF NOT EXISTS meta (
      key   TEXT PRIMARY KEY,
      value TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS settings (
      key         TEXT PRIMARY KEY,
      value       TEXT DEFAULT 'null',        -- JSON 编码的任意值
      grp         TEXT DEFAULT 'general',     -- 分组：general/voice/meeting/dsh/panel
      label       TEXT DEFAULT '',
      description TEXT DEFAULT '',
      value_type  TEXT DEFAULT 'str',         -- str/int/float/bool/json/list
      options     TEXT DEFAULT '[]',          -- JSON 候选值（面板下拉用）
      updated_at  TEXT DEFAULT (datetime('now','localtime'))
    );

    CREATE TABLE IF NOT EXISTS commands (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      ts          TEXT DEFAULT (datetime('now','localtime')),
      source      TEXT DEFAULT 'api',         -- hotkey|wake|web|skill|api|mobile|test
      text        TEXT DEFAULT '',
      status      TEXT DEFAULT 'pending',     -- pending|sent|running|done|failed
      session_id  TEXT DEFAULT '',
      reply       TEXT DEFAULT '',            -- DSH 最终回复全文
      brief       TEXT DEFAULT '',            -- 语音简报（精简文本）
      duration_ms INTEGER DEFAULT 0,
      error       TEXT DEFAULT '',
      meta        TEXT DEFAULT '{}'           -- JSON 扩展（可放标记、会议关联等）
    );
    CREATE INDEX IF NOT EXISTS idx_commands_ts ON commands(ts DESC);

    CREATE TABLE IF NOT EXISTS dsh_sessions (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      name         TEXT DEFAULT '',           -- 用途名：command / summary / chat-xxx
      kind         TEXT DEFAULT 'chat',       -- command|summary|chat
      session_id   TEXT DEFAULT '',           -- DSH 侧 sessionId
      created_at   TEXT DEFAULT (datetime('now','localtime')),
      last_used_at TEXT DEFAULT ''
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_kind ON dsh_sessions(kind);

    CREATE TABLE IF NOT EXISTS meetings (
      id               INTEGER PRIMARY KEY AUTOINCREMENT,
      name             TEXT UNIQUE,           -- 文件夹名 2026-08-21_10-00-00
      title            TEXT DEFAULT '',       -- 可编辑标题
      started_at       TEXT DEFAULT '',
      ended_at         TEXT DEFAULT '',
      duration_seconds REAL DEFAULT 0,
      status           TEXT DEFAULT '',       -- recording|transcribing|done|error
      stt_model        TEXT DEFAULT 'small',
      stt_device       TEXT DEFAULT 'auto',
      diarize          INTEGER DEFAULT 0,
      segments         INTEGER DEFAULT 0,     -- 音频分段数
      audio_bytes      INTEGER DEFAULT 0,
      notes            TEXT DEFAULT '{}',     -- JSON 备注/标签（未来扩展）
      created_at       TEXT DEFAULT (datetime('now','localtime'))
    );

    CREATE TABLE IF NOT EXISTS speakers (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
      label      TEXT NOT NULL,               -- S1/S2/SPEAKER_00…
      name       TEXT DEFAULT '',             -- 显示名：说话人1 / 张三
      color      TEXT DEFAULT '',             -- 面板颜色（预留）
      UNIQUE(meeting_id, label)
    );

    CREATE TABLE IF NOT EXISTS lines (
      id             INTEGER PRIMARY KEY AUTOINCREMENT,
      meeting_id     INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
      seg_index      INTEGER DEFAULT 1,       -- 第几段音频
      start          REAL DEFAULT 0,          -- 段内相对秒
      end            REAL DEFAULT 0,
      speaker_label  TEXT DEFAULT '',         -- 冗余存 label，避免 join 顺序问题
      text           TEXT DEFAULT '',
      kind           TEXT DEFAULT 'speech',   -- speech/note/action…（预留扩展）
      revised        INTEGER DEFAULT 0,
      updated_at     TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_lines_meeting ON lines(meeting_id, seg_index, start);

    CREATE TABLE IF NOT EXISTS summary_runs (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      meeting_id   INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
      extra        TEXT DEFAULT '',           -- 追加要求
      status       TEXT DEFAULT 'pending',    -- pending|done|failed
      requested_at TEXT DEFAULT (datetime('now','localtime')),
      finished_at  TEXT DEFAULT '',
      result_path  TEXT DEFAULT ''            -- summary.md 路径（预留）
    );

    CREATE TABLE IF NOT EXISTS component_states (
      name       TEXT PRIMARY KEY,            -- dsh|server|stt|tts|wake|hotkey|meeting|diarize
      status     TEXT DEFAULT 'unknown',      -- online|offline|active|idle|error|disabled
      detail     TEXT DEFAULT '',
      pid        INTEGER DEFAULT 0,
      updated_at TEXT DEFAULT (datetime('now','localtime'))
    );

    CREATE TABLE IF NOT EXISTS logs (
      id      INTEGER PRIMARY KEY AUTOINCREMENT,
      ts      TEXT DEFAULT (datetime('now','localtime')),
      level   TEXT DEFAULT 'info',            -- debug|info|warn|error
      source  TEXT DEFAULT '',                -- assistant|meeting|dsh|hotkey|wake|api…
      message TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(id DESC);

    CREATE TABLE IF NOT EXISTS events (
      id      INTEGER PRIMARY KEY AUTOINCREMENT,
      ts      TEXT DEFAULT (datetime('now','localtime')),
      type    TEXT DEFAULT '',                -- command_sent|meeting_started|wake_fired…
      payload TEXT DEFAULT '{}'               -- JSON
    );

    CREATE TABLE IF NOT EXISTS api_keys (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      name         TEXT DEFAULT '',
      token_hash   TEXT NOT NULL UNIQUE,      -- sha256(token) 十六进制；明文永不落库
      scopes       TEXT DEFAULT '["read"]',   -- JSON 权限列表
      enabled      INTEGER DEFAULT 1,
      created_at   TEXT DEFAULT (datetime('now','localtime')),
      last_used_at TEXT DEFAULT ''
    );
    """),
    # 2: api_keys 从「明文 token」改为「sha256(token)」。SQL 部分留空，转换由
    #    _migrate_api_keys_hash 在 Python 里做（SQLite 没有 sha256 函数）。
    (2, ""),
    # 3: 会议 ↔ DSH 会话映射。一场会议共用一个会话（纪要/分段/语义分段/归档
    #    都发进它），下一场会议新建。落库是为了让归档环节（worklog）也能复用
    #    同一会话，并且 ECHO 重启后映射不丢。
    (3, """
    CREATE TABLE IF NOT EXISTS meeting_sessions (
      meeting_id   TEXT PRIMARY KEY,          -- meetings.name（如 2026-09-15_10-07-25）
      session_id   TEXT DEFAULT '',           -- DSH 侧 sessionId
      workspace_id TEXT DEFAULT '',           -- 所属 DSH 工作区 id（归档/诊断用）
      created_at   TEXT DEFAULT (datetime('now','localtime')),
      last_used_at TEXT DEFAULT ''
    );
    """),
    # 4: 常用联系人声纹（issue #6：会议转写已有说话人分离+重命名，再让 ECHO 记住联系人）。
    #    voiceprints        —— 声纹库：联系人名 + 一条说话人嵌入样本。
    #                          meeting_name/source_label 记录样本来源，是**弱关联**：
    #                          删除历史会议不连带删声纹库（否则清理旧会议＝丢联系人），
    #                          但要删某条样本可以按 id 删；同一会议同一说话人只留最新一条。
    #    speaker_embeddings —— 每场会议各说话人的平均嵌入：转写时留存，
    #                          改名为联系人时据此入库，「识别本场」也用它（不必重新分离）。
    (4, """
    CREATE TABLE IF NOT EXISTS voiceprints (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      name         TEXT NOT NULL,              -- 联系人名（如 张总）
      embedding    BLOB NOT NULL,              -- 归一化后的说话人嵌入（float32）
      dim          INTEGER DEFAULT 256,
      meeting_name TEXT DEFAULT '',            -- 来源会议文件夹名（弱关联）
      source_label TEXT DEFAULT '',            -- 来源会议内的说话人标签（S1/S2…）
      created_at   TEXT DEFAULT (datetime('now','localtime')),
      updated_at   TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_voiceprints_name ON voiceprints(name);

    CREATE TABLE IF NOT EXISTS speaker_embeddings (
      meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
      label      TEXT NOT NULL,                -- S1/S2…
      embedding  BLOB NOT NULL,                -- 该说话人在本场的平均嵌入（float32，已归一化）
      dim        INTEGER DEFAULT 256,
      segments   INTEGER DEFAULT 0,            -- 参与聚合的音频片段数
      updated_at TEXT DEFAULT '',
      PRIMARY KEY (meeting_id, label)
    );
    """),
]

# 需要 Python 参与的迁移：版本号 → callable(conn)，在对应版本的 SQL 之后执行
PY_MIGRATIONS = {2: _migrate_api_keys_hash}


# ---------------------------------------------------------------- 连接管理

def get_conn():
    """打开一个短连接（WAL + Row 工厂）。调用方负责 close/commit。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_write_lock = threading.Lock()


def _exec(sql, params=()):
    """写辅助：加锁 + 自动 commit/close。"""
    with _write_lock:
        conn = get_conn()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def _query(sql, params=()):
    conn = get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _query_one(sql, params=()):
    conn = get_conn()
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------- 初始化/迁移

def init():
    """建库 + 跑迁移。幂等，可反复调用。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with _write_lock:
        conn = get_conn()
        try:
            conn.executescript(MIGRATIONS[0][1])
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            cur = int(row["value"]) if row else 0
            for version, sql in MIGRATIONS:
                if version > cur:
                    if sql:
                        conn.executescript(sql)
                    # 少数迁移纯 SQL 干不了（例如 SQLite 没有 sha256），用 Python 补
                    py = PY_MIGRATIONS.get(version)
                    if py:
                        py(conn)
                    conn.execute(
                        "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(version),))
            conn.commit()
        finally:
            conn.close()
    return DB_FILE


# ---------------------------------------------------------------- 工具

def _json_dumps(v):
    return json.dumps(v, ensure_ascii=False)


def _json_loads(s, default=None):
    try:
        return json.loads(s)
    except Exception:
        return default if default is not None else {}


# ---------------------------------------------------------------- settings

def get_setting(key, default=None):
    row = _query_one("SELECT value FROM settings WHERE key=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def set_setting(key, value, grp="general", label="", description="", value_type="str", options=None):
    options = options or []
    _exec(
        "INSERT INTO settings(key,value,grp,label,description,value_type,options,updated_at) "
        "VALUES(?,?,?,?,?,?,?,datetime('now','localtime')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now','localtime')",
        (key, _json_dumps(value), grp, label, description, value_type, _json_dumps(options)))


def sync_setting_meta(key, grp, label, description, value_type, options):
    """仅同步设置的面板元数据（分组/说明/选项），保留用户已存的 value。"""
    _exec(
        "UPDATE settings SET grp=?, label=?, description=?, value_type=?, options=? WHERE key=?",
        (grp, label, description, value_type, _json_dumps(options or []), key))


def all_settings():
    rows = _query("SELECT * FROM settings ORDER BY grp, key")
    for r in rows:
        try:
            r["value"] = json.loads(r["value"])
        except Exception:
            pass
        try:
            r["options"] = json.loads(r["options"])
        except Exception:
            r["options"] = []
    return rows


def upsert_settings(mapping):
    """批量更新设置值（保留既有元数据；缺失的行补建）。

    为什么不能只写 UPDATE：`Settings.update()` 走这里，而面板可能在
    `seed_defaults()` 之前就写入某个键（新增配置项、或在旧库里改一项从未落库的
    配置）——只 UPDATE 时库里没有该行，更新会**静默丢失**，表现为"保存了但没生效"
    （2026-09-15 实测踩到：新增 commandWorkspace 后写入无效）。
    因此这里改成 INSERT ... ON CONFLICT，行不存在时补建，且**不覆盖已有元数据**
    （grp/label/description/value_type/options 只在插入时给默认值）。
    """
    with _write_lock:
        conn = get_conn()
        try:
            for k, v in mapping.items():
                conn.execute(
                    "INSERT INTO settings(key,value,grp,label,description,value_type,options,updated_at) "
                    "VALUES(?,?,'general','','','str','[]',datetime('now','localtime')) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=datetime('now','localtime')",
                    (k, _json_dumps(v)))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------- commands

def add_command(text, source="api", status="pending", session_id="", meta=None):
    return _exec(
        "INSERT INTO commands(ts,source,text,status,session_id,meta) "
        "VALUES(datetime('now','localtime'),?,?,?,?,?)",
        (source, text, status, session_id, _json_dumps(meta or {})))


def update_command(cmd_id, **fields):
    """更新命令：status/reply/brief/duration_ms/error/session_id。"""
    allowed = {"status", "reply", "brief", "duration_ms", "error", "session_id"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(cmd_id)
    _exec(f"UPDATE commands SET {', '.join(sets)} WHERE id=?", params)


def list_commands(limit=100, offset=0):
    return _query(
        "SELECT * FROM commands ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))


def get_command(cmd_id):
    return _query_one("SELECT * FROM commands WHERE id=?", (cmd_id,))


def count_commands():
    return _query_one("SELECT COUNT(*) AS n FROM commands")["n"]


def clear_commands():
    _exec("DELETE FROM commands")


# ---------------------------------------------------------------- dsh_sessions

def upsert_session(kind, session_id, name=""):
    """每个 kind 至多一条（command/summary）。chat 会话不入此表。"""
    _exec(
        "INSERT INTO dsh_sessions(name,kind,session_id) VALUES(?,?,?) "
        "ON CONFLICT(kind) DO UPDATE SET session_id=excluded.session_id, name=excluded.name, "
        "last_used_at=datetime('now','localtime')",
        (name, kind, session_id))


def get_session(kind):
    return _query_one("SELECT * FROM dsh_sessions WHERE kind=?", (kind,))


def list_sessions():
    return _query("SELECT * FROM dsh_sessions ORDER BY kind")


def touch_session(kind):
    _exec("UPDATE dsh_sessions SET last_used_at=datetime('now','localtime') WHERE kind=?", (kind,))


# ------------------------------------------------------- meeting_sessions
# 一场会议一个 DSH 会话（纪要/分段/语义分段/归档共用），下一场会议新建。

def upsert_meeting_session(meeting_id, session_id, workspace_id=""):
    _exec(
        "INSERT INTO meeting_sessions(meeting_id,session_id,workspace_id,last_used_at) "
        "VALUES(?,?,?,datetime('now','localtime')) "
        "ON CONFLICT(meeting_id) DO UPDATE SET session_id=excluded.session_id, "
        "workspace_id=excluded.workspace_id, last_used_at=datetime('now','localtime')",
        (meeting_id, session_id, workspace_id))


def get_meeting_session(meeting_id):
    return _query_one("SELECT * FROM meeting_sessions WHERE meeting_id=?", (meeting_id,))


def touch_meeting_session(meeting_id):
    _exec("UPDATE meeting_sessions SET last_used_at=datetime('now','localtime') "
          "WHERE meeting_id=?", (meeting_id,))


def delete_meeting_session(meeting_id):
    _exec("DELETE FROM meeting_sessions WHERE meeting_id=?", (meeting_id,))


# ---------------------------------------------------------------- meetings

def create_meeting(name, started_at="", stt_model="small", stt_device="auto", diarize=0):
    return _exec(
        "INSERT INTO meetings(name,started_at,stt_model,stt_device,diarize,status) "
        "VALUES(?,?,?,?,?,?)",
        (name, started_at, stt_model, stt_device, 1 if diarize else 0, "recording"))


def update_meeting(meeting_id, **fields):
    allowed = {"title", "ended_at", "duration_seconds", "status",
               "stt_model", "stt_device", "diarize", "segments", "audio_bytes", "notes"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(meeting_id)
    _exec(f"UPDATE meetings SET {', '.join(sets)} WHERE id=?", params)


def get_meeting_by_name(name):
    return _query_one("SELECT * FROM meetings WHERE name=?", (name,))


def get_meeting(meeting_id):
    return _query_one("SELECT * FROM meetings WHERE id=?", (meeting_id,))


def list_meetings(limit=100, offset=0):
    return _query("SELECT * FROM meetings ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))


def delete_meeting(meeting_id):
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
            conn.commit()
        finally:
            conn.close()


def set_meeting_status_by_name(name, status):
    _exec("UPDATE meetings SET status=? WHERE name=?", (status, name))


def clear_meeting_lines(meeting_id):
    """清空说话人+转写行（重新转写前调用；保留纪要记录）。

    说话人声纹样本一并清掉：重新转写会重新分离、说话人编号可能整体变化，
    旧样本留着会让「识别本场」认错人（转写结束会写入本轮的新样本）。
    """
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("DELETE FROM speakers WHERE meeting_id=?", (meeting_id,))
            conn.execute("DELETE FROM speaker_embeddings WHERE meeting_id=?", (meeting_id,))
            conn.execute("DELETE FROM lines WHERE meeting_id=?", (meeting_id,))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------- speakers

def replace_speakers(meeting_id, speaker_map):
    """speaker_map: {label: 显示名}。保留用户改过名的条目。"""
    with _write_lock:
        conn = get_conn()
        try:
            existing = {r["label"]: r["name"] for r in conn.execute(
                "SELECT label,name FROM speakers WHERE meeting_id=?", (meeting_id,)).fetchall()}
            conn.execute("DELETE FROM speakers WHERE meeting_id=?", (meeting_id,))
            for label, default_name in speaker_map.items():
                name = existing.get(label, "") or default_name
                conn.execute(
                    "INSERT OR IGNORE INTO speakers(meeting_id,label,name) VALUES(?,?,?)",
                    (meeting_id, label, name))
            conn.commit()
        finally:
            conn.close()


def get_speakers(meeting_id):
    return _query("SELECT * FROM speakers WHERE meeting_id=? ORDER BY id", (meeting_id,))


def rename_speaker(meeting_id, label, new_name):
    _exec("UPDATE speakers SET name=? WHERE meeting_id=? AND label=?",
          (new_name.strip(), meeting_id, label))


def merge_speakers(meeting_id, source_label, target_label):
    """把 source_label 合并进 target_label：所有行改标 target，删除 source。"""
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("UPDATE lines SET speaker_label=? WHERE meeting_id=? AND speaker_label=?",
                         (target_label, meeting_id, source_label))
            conn.execute("DELETE FROM speakers WHERE meeting_id=? AND label=?",
                         (meeting_id, source_label))
            conn.commit()
        finally:
            conn.close()


def cleanup_empty_speakers(meeting_id):
    _exec("""DELETE FROM speakers WHERE meeting_id=? AND label NOT IN
             (SELECT DISTINCT speaker_label FROM lines
              WHERE meeting_id=? AND speaker_label != '')""",
          (meeting_id, meeting_id))


# ------------------------------------------------------- speaker_embeddings
# 每场会议各说话人的平均声纹（pyannote/ wespeaker 256 维嵌入，转写时留存）。
# 用途：① 改名为联系人时据此把样本存进声纹库；② 「识别本场」不重新分离也能认人。

def replace_speaker_embeddings(meeting_id, mapping):
    """整场替换说话人声纹：mapping {label: (blob, dim, segments)}。

    整场替换（而不是 upsert）：重新转写时说话人编号可能整体变化，
    残留旧样本会让「识别本场」用错人。
    """
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("DELETE FROM speaker_embeddings WHERE meeting_id=?", (meeting_id,))
            for label, (blob, dim, segments) in (mapping or {}).items():
                conn.execute(
                    "INSERT INTO speaker_embeddings(meeting_id,label,embedding,dim,segments,updated_at) "
                    "VALUES(?,?,?,?,?,datetime('now','localtime'))",
                    (meeting_id, label, blob, int(dim), int(segments)))
            conn.commit()
        finally:
            conn.close()


def get_speaker_embeddings(meeting_id):
    """{label: {label,dim,segments,embedding}}（embedding 为 BLOB，解码在 voiceprint 层）。"""
    rows = _query("SELECT label,embedding,dim,segments FROM speaker_embeddings "
                  "WHERE meeting_id=? ORDER BY label", (meeting_id,))
    return {r["label"]: r for r in rows}


def get_speaker_embedding(meeting_id, label):
    return _query_one("SELECT * FROM speaker_embeddings WHERE meeting_id=? AND label=?",
                      (meeting_id, label))


# ---------------------------------------------------------------- voiceprints
# 常用联系人声纹库：会议里把说话人改名为联系人后，把声音存成样本；
# 新会议转写时按余弦相似度自动把说话人认成联系人（匹配逻辑在 app/voiceprint.py）。

def replace_voiceprint_sample(name, embedding, dim=256, meeting_name="", source_label=""):
    """写入一条声纹样本；同一会议同一说话人先删旧样本再写。

    这样「改错名再改回来」不会留下旧名字的脏样本（一个人一场会议只对应一个名字）；
    换一场会议则各算一条样本，样本越多识别越稳。
    """
    with _write_lock:
        conn = get_conn()
        try:
            if meeting_name and source_label:
                conn.execute("DELETE FROM voiceprints WHERE meeting_name=? AND source_label=?",
                             (meeting_name, source_label))
            conn.execute(
                "INSERT INTO voiceprints(name,embedding,dim,meeting_name,source_label,updated_at) "
                "VALUES(?,?,?,?,?,datetime('now','localtime'))",
                (str(name).strip(), embedding, int(dim), meeting_name, source_label))
            conn.commit()
        finally:
            conn.close()


def list_voiceprints(with_embedding=False):
    cols = ("id,name,embedding,dim,meeting_name,source_label,created_at,updated_at"
            if with_embedding else
            "id,name,dim,meeting_name,source_label,created_at,updated_at")
    return _query(f"SELECT {cols} FROM voiceprints ORDER BY id")


def get_voiceprint_samples():
    """匹配用：全部样本（含 BLOB）。"""
    return _query("SELECT id,name,embedding,dim,meeting_name FROM voiceprints ORDER BY id")


def get_voiceprint(vid):
    return _query_one("SELECT * FROM voiceprints WHERE id=?", (vid,))


def count_voiceprints(name=None):
    if name is None:
        return _query_one("SELECT COUNT(*) AS n FROM voiceprints")["n"]
    return _query_one("SELECT COUNT(*) AS n FROM voiceprints WHERE name=?",
                      (str(name).strip(),))["n"]


def count_voiceprint_contacts():
    return _query_one("SELECT COUNT(DISTINCT name) AS n FROM voiceprints")["n"]


def delete_voiceprint(vid):
    _exec("DELETE FROM voiceprints WHERE id=?", (vid,))


def delete_voiceprints_by_name(name):
    _exec("DELETE FROM voiceprints WHERE name=?", (str(name).strip(),))


# 注：曾有 delete_voiceprints_by_source(meeting_name, source_label)——无调用方，已于 2026-09-15 删除。
# 删会议保留声纹库是刻意的设计（弱关联），所以不需要它。


# ---------------------------------------------------------------- lines

def add_lines(meeting_id, rows):
    """rows: [(seg_index, start, end, speaker_label, text), ...]"""
    if not rows:
        return
    with _write_lock:
        conn = get_conn()
        try:
            conn.executemany(
                "INSERT INTO lines(meeting_id,seg_index,start,end,speaker_label,text) "
                "VALUES(?,?,?,?,?,?)",
                [(meeting_id, seg, s, e, spk, txt) for seg, s, e, spk, txt in rows])
            conn.commit()
        finally:
            conn.close()


def get_lines(meeting_id):
    return _query(
        "SELECT * FROM lines WHERE meeting_id=? ORDER BY seg_index, start, id", (meeting_id,))


def update_line(line_id, new_text):
    _exec("UPDATE lines SET text=?, revised=1, updated_at=datetime('now','localtime') WHERE id=?",
          (new_text.strip(), line_id))


def set_line_kind(line_id, kind):
    _exec("UPDATE lines SET kind=? WHERE id=?", (kind, line_id))


# ---------------------------------------------------------------- summary_runs

def add_summary_run(meeting_id, extra=""):
    return _exec("INSERT INTO summary_runs(meeting_id,extra) VALUES(?,?)", (meeting_id, extra))


def finish_summary_run(run_id, status="done", result_path=""):
    _exec("UPDATE summary_runs SET status=?, finished_at=datetime('now','localtime'), "
          "result_path=? WHERE id=?", (status, result_path, run_id))


def get_summary_runs(meeting_id):
    return _query("SELECT * FROM summary_runs WHERE meeting_id=? ORDER BY id DESC", (meeting_id,))


# ---------------------------------------------------------------- component_states

def set_component_state(name, status, detail="", pid=0):
    _exec(
        "INSERT INTO component_states(name,status,detail,pid,updated_at) "
        "VALUES(?,?,?,?,datetime('now','localtime')) "
        "ON CONFLICT(name) DO UPDATE SET status=excluded.status, detail=excluded.detail, "
        "pid=excluded.pid, updated_at=datetime('now','localtime')",
        (name, status, detail, pid))


def get_component_states():
    return _query("SELECT * FROM component_states ORDER BY name")


def get_component_state(name):
    return _query_one("SELECT * FROM component_states WHERE name=?", (name,))


# ---------------------------------------------------------------- logs / events

def add_log(level, source, message):
    try:
        _exec("INSERT INTO logs(level,source,message) VALUES(?,?,?)",
              (level, source, str(message)[:2000]))
    except Exception:
        pass


def list_logs(limit=200, level="", source=""):
    conds, params = [], []
    if level:
        conds.append("level=?")
        params.append(level)
    if source:
        conds.append("source=?")
        params.append(source)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    return _query(f"SELECT * FROM logs{where} ORDER BY id DESC LIMIT ?", tuple(params))


def clear_logs():
    _exec("DELETE FROM logs")


def add_event(type_, payload=None):
    try:
        _exec("INSERT INTO events(type,payload) VALUES(?,?)",
              (type_, _json_dumps(payload or {})))
    except Exception:
        pass


def list_events(limit=200):
    return _query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------- api_keys
# 安全（2026-09-13 MEDIUM-2）：库里只存 sha256(token)，明文永不落库；
#   * 明文只在 POST /api/keys 创建时返回一次，之后无从取回（丢了就删掉重建）；
#   * GET /api/keys 只回元数据，不回 token 也不回哈希；
#   * 校验用 hmac.compare_digest 逐行恒定时比较（不把 token 交给 SQL 比较，
#     也不让命中行数/比较时长成为侧信道）。

def add_api_key(name, scopes=None):
    import secrets
    token = "echo_" + secrets.token_hex(24)
    _exec("INSERT INTO api_keys(name,token_hash,scopes) VALUES(?,?,?)",
          (name, _hash_token(token), _json_dumps(scopes or ["read"])))
    return token


def list_api_keys():
    """只回元数据。token / token_hash 一律不出库。"""
    rows = _query("SELECT id,name,scopes,enabled,created_at,last_used_at FROM api_keys")
    for r in rows:
        r["scopes"] = _json_loads(r["scopes"], [])
    return rows


def verify_api_key(token):
    if not token:
        return None
    h = _hash_token(token)
    for row in _query("SELECT * FROM api_keys WHERE enabled=1"):
        if hmac.compare_digest(str(row.get("token_hash") or ""), h):
            _exec("UPDATE api_keys SET last_used_at=datetime('now','localtime') WHERE id=?", (row["id"],))
            row["scopes"] = _json_loads(row["scopes"], [])
            row.pop("token_hash", None)      # 哈希也不往上传
            return row
    return None


def delete_api_key(key_id):
    _exec("DELETE FROM api_keys WHERE id=?", (key_id,))
