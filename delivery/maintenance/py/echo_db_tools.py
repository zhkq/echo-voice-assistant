#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ECHO database helper for the maintenance scripts (backup / verify / selective merge).

Called by:
  1-备份ECHO会议数据.ps1   -> backup, counts
  3-还原ECHO会议数据.ps1   -> merge, counts

Subcommands
-----------
backup  --db SRC --out-db DST --summary JSON [--tables-dir DIR] [--include-lines]
        Online consistent snapshot of the live SQLite DB (safe while ECHO runs),
        integrity check, row counts, and optional per-table JSONL dumps.

counts  --db PATH --summary JSON
        Row counts + integrity + schema_version of a database (used to verify).

merge   --backup-db SRC --target-db DST --summary JSON [--replace]
        Copy ONLY meeting-related tables from the backup into the freshly
        installed DB. Every row keeps its original id, so meeting folders,
        speakers and transcript lines stay consistent. Settings / commands /
        logs of the old install are intentionally NOT copied.

All messages are ASCII on purpose: the PowerShell side prints the Chinese text,
this tool only needs to be readable in a log file.
"""
import argparse
import json
import os
import sqlite3
import sys

MEETING_TABLES = [
    "meetings",
    "speakers",
    "lines",
    "summary_runs",
    "meeting_sessions",
    "voiceprints",
    "speaker_embeddings",
]

# Big / operational tables: present in the snapshot DB but not dumped to JSONL
# unless --include-lines is given.
SKIP_DUMP = {"lines", "events", "logs"}


def q(name):
    return '"' + name.replace('"', '""') + '"'


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=_json_default)
    os.replace(tmp, path)


def _json_default(obj):
    """BLOB columns (speaker_embeddings / voiceprints) are not JSON types."""
    if isinstance(obj, (bytes, bytearray, memoryview)):
        import base64
        return {"__b64__": base64.b64encode(bytes(obj)).decode("ascii")}
    return str(obj)


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows if not str(r[0]).startswith("sqlite_")]


def columns(conn, table):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % q(table))]


def count_rows(conn, table):
    return conn.execute("SELECT count(*) FROM %s" % q(table)).fetchone()[0]


def schema_version(conn):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return row[0] if row else ""
    except sqlite3.Error:
        return ""


def open_ro(path):
    import urllib.parse
    p = urllib.parse.quote(path.replace("\\", "/"), safe="/:")
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=30)


def snapshot(src_path, dst_path):
    if os.path.exists(dst_path):
        os.remove(dst_path)
    src = open_ro(src_path)
    try:
        src.execute("PRAGMA busy_timeout=30000")
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)                     # online backup: consistent, no service stop
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def db_summary(conn):
    tables = table_names(conn)
    return {
        "schema_version": schema_version(conn),
        "tables": {t: count_rows(conn, t) for t in tables},
    }


def cmd_backup(args):
    if not os.path.exists(args.db):
        raise SystemExit("source db not found: %s" % args.db)
    os.makedirs(os.path.dirname(os.path.abspath(args.out_db)), exist_ok=True)
    snapshot(args.db, args.out_db)

    conn = sqlite3.connect(args.out_db)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        info = db_summary(conn)
        info["integrity"] = integrity
        info["bytes"] = os.path.getsize(args.out_db)

        if args.tables_dir:
            os.makedirs(args.tables_dir, exist_ok=True)
            dumped = {}
            for t in table_names(conn):
                if t in SKIP_DUMP and not args.include_lines:
                    continue
                cols = columns(conn, t)
                path = os.path.join(args.tables_dir, t + ".jsonl")
                n = 0
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    cur = conn.execute("SELECT * FROM %s" % q(t))
                    names = [d[0] for d in cur.description]
                    for row in cur:
                        f.write(json.dumps(dict(zip(names, row)), ensure_ascii=False,
                                           default=_json_default) + "\n")
                        n += 1
                dumped[t] = n
            info["dumped"] = dumped
            # settings as a readable snapshot too
            try:
                rows = conn.execute(
                    "SELECT key, value, namespace FROM settings ORDER BY namespace, key"
                ).fetchall()
                with open(os.path.join(args.tables_dir, "settings.json"), "w",
                          encoding="utf-8") as f:
                    json.dump([{"key": r[0], "value": r[1], "namespace": r[2]} for r in rows],
                              f, ensure_ascii=False, indent=1)
            except sqlite3.Error:
                pass
    finally:
        conn.close()

    write_json(args.summary, {"ok": True, **info})
    print("backup ok: meetings=%s lines=%s integrity=%s"
          % (info["tables"].get("meetings"), info["tables"].get("lines"), integrity))


def cmd_counts(args):
    conn = open_ro(args.db)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        info = db_summary(conn)
        info["integrity"] = integrity
        info["bytes"] = os.path.getsize(args.db)
    finally:
        conn.close()
    write_json(args.summary, {"ok": True, **info})
    print("counts ok: meetings=%s lines=%s"
          % (info["tables"].get("meetings"), info["tables"].get("lines")))


def cmd_merge(args):
    if not os.path.exists(args.backup_db):
        raise SystemExit("backup db not found: %s" % args.backup_db)
    if not os.path.exists(args.target_db):
        raise SystemExit("target db not found (start ECHO once first): %s" % args.target_db)

    src = open_ro(args.backup_db)
    tgt = sqlite3.connect(args.target_db, timeout=30)
    report = {}
    try:
        tgt.execute("PRAGMA busy_timeout=30000")
        tgt.execute("PRAGMA foreign_keys=OFF")
        tgt_tables = set(table_names(tgt))
        src_tables = set(table_names(src))

        # ---- conflict pre-check: refuse a merge into a non-empty fresh DB ----
        conflicts = {}
        for t in MEETING_TABLES:
            if t not in tgt_tables or t not in src_tables:
                continue
            n = count_rows(tgt, t)
            if n:
                conflicts[t] = n
        if conflicts and not args.replace:
            write_json(args.summary, {"ok": False, "error": "target-not-empty",
                                      "conflicts": conflicts})
            print("target already has data: %s (use --replace to overwrite)"
                  % ", ".join("%s=%s" % kv for kv in conflicts.items()))
            raise SystemExit(3)

        tgt.execute("BEGIN IMMEDIATE")
        for t in MEETING_TABLES:
            if t not in tgt_tables:
                report[t] = {"skipped": "target has no such table"}
                continue
            if t not in src_tables:
                report[t] = {"skipped": "backup has no such table"}
                continue
            src_cols = set(columns(src, t))
            cols = [c for c in columns(tgt, t) if c in src_cols]
            before = count_rows(tgt, t)
            if before and args.replace:
                tgt.execute("DELETE FROM %s" % q(t))
            if not cols:
                report[t] = {"skipped": "no common columns"}
                continue
            sel = "SELECT %s FROM %s" % (", ".join(q(c) for c in cols), q(t))
            rows = src.execute(sel).fetchall()
            if rows:
                sql = "INSERT INTO %s (%s) VALUES (%s)" % (
                    q(t), ", ".join(q(c) for c in cols), ", ".join("?" * len(cols)))
                for i in range(0, len(rows), 400):
                    tgt.executemany(sql, rows[i:i + 400])
            report[t] = {"columns": len(cols), "rows": len(rows), "before": before}
        tgt.commit()
        integrity = tgt.execute("PRAGMA integrity_check").fetchone()[0]
        fk = tgt.execute("PRAGMA foreign_key_check").fetchall()
        counts = {t: count_rows(tgt, t) for t in table_names(tgt)}
    except Exception:
        tgt.rollback()
        raise
    finally:
        src.close()
        tgt.close()

    write_json(args.summary, {
        "ok": True,
        "mode": "meetings-merge",
        "integrity": integrity,
        "foreign_key_issues": len(fk),
        "schema_version": "",
        "tables": counts,
        "merged": report,
    })
    print("merge ok: meetings=%s lines=%s integrity=%s fk_issues=%d"
          % (counts.get("meetings"), counts.get("lines"), integrity, len(fk)))


def main():
    ap = argparse.ArgumentParser(description="ECHO db maintenance helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup")
    b.add_argument("--db", required=True)
    b.add_argument("--out-db", required=True)
    b.add_argument("--summary", required=True)
    b.add_argument("--tables-dir", default="")
    b.add_argument("--include-lines", action="store_true")
    b.set_defaults(func=cmd_backup)

    c = sub.add_parser("counts")
    c.add_argument("--db", required=True)
    c.add_argument("--summary", required=True)
    c.set_defaults(func=cmd_counts)

    m = sub.add_parser("merge")
    m.add_argument("--backup-db", required=True)
    m.add_argument("--target-db", required=True)
    m.add_argument("--summary", required=True)
    m.add_argument("--replace", action="store_true")
    m.set_defaults(func=cmd_merge)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
