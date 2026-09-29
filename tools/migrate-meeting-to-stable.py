# -*- coding: utf-8 -*-
"""把 dev 树里某场会议迁移到稳定版（D:\\ECHO）——拷目录 + 拷数据行。

口径与以前迁过的那几场一致（如 D:\\ECHO 的 id=103 ↔ dev 的 2026-09-22_14-31-46）：
  * 目录整个拷过去（transcript.md / summary.md / topics.md / meta.json / 各段 wav 都在里面）
  * 往稳定版 echo.db 的 meetings 表插一行，字段值照抄；**只插两边都有的列**
    （dev 多一列 `error`，稳定版没有）
  * 先备份稳定版库；不删 dev 侧原物（"迁移"先做成"复制"，确认无误再由用户决定删）
"""
import os
import shutil
import sqlite3
import sys
import time

DEV_DB = r"C:\echo-dev\data\echo.db"
DEV_MEET = r"C:\echo-dev\data\meetings"
STB_DB = r"D:\ECHO\data\echo.db"
STB_MEET = r"D:\ECHO\data\meetings"
NAME = sys.argv[1] if len(sys.argv) > 1 else "2026-09-28_10-00-43"

src = os.path.join(DEV_MEET, NAME)
dst = os.path.join(STB_MEET, NAME)
if not os.path.isdir(src):
    print("  [fail] dev 侧没有这场会议目录：%s" % src)
    raise SystemExit(2)

size = sum(os.path.getsize(os.path.join(dp, f))
           for dp, _dn, fn in os.walk(src) for f in fn)
free = shutil.disk_usage("D:\\").free
print("  源目录 %.1f MB / D 盘可用 %.1f GB" % (size / 1048576.0, free / 1073741824.0))
if free < size * 1.5 + (1 << 30):
    print("  [fail] D 盘空间不够（需要约 %.1f GB）" % (size * 1.5 / 1073741824.0))
    raise SystemExit(3)

con = sqlite3.connect(DEV_DB)
con.row_factory = sqlite3.Row
dev = con.execute("select * from meetings where name=?", (NAME,)).fetchone()
con.close()
if dev is None:
    print("  [fail] dev 库里没有 name=%s 的会议行" % NAME)
    raise SystemExit(4)
dev = dict(dev)

stb = sqlite3.connect(STB_DB)
stb.row_factory = sqlite3.Row
cols = [r[1] for r in stb.execute("pragma table_info(meetings)")]
if stb.execute("select 1 from meetings where name=?", (NAME,)).fetchone():
    print("  [skip] 稳定版里已经有 name=%s 的会议行" % NAME)
else:
    bak = STB_DB + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    stb.close()
    shutil.copy2(STB_DB, bak)
    print("  已备份稳定版库 → %s" % os.path.basename(bak))
    stb = sqlite3.connect(STB_DB)

if not os.path.isdir(dst):
    shutil.copytree(src, dst)
    print("  已拷目录 → %s（%.1f MB）" % (dst, size / 1048576.0))
else:
    print("  [skip] 目录已存在：%s" % dst)

keys = [c for c in cols if c != "id" and c in dev]
sql = "insert into meetings (%s) values (%s)" % (",".join(keys), ",".join("?" * len(keys)))
stb.execute(sql, [dev[k] for k in keys])
stb.commit()
new_id = stb.execute("select id from meetings where name=?", (NAME,)).fetchone()[0]
print("  已插行 → 稳定版 id=%s（迁移字段：%s）" % (new_id, ",".join(keys)))

print("  稳定版现有最近 5 场：")
for r in stb.execute("select id,name,started_at,duration_seconds,status,segments "
                     "from meetings order by id desc limit 5"):
    print("    id=%-4s %-24s %s %6.0fs %-11s seg=%s" % tuple(r))
stb.close()
print("  落盘文件：%s" % ", ".join(sorted(os.listdir(dst))))
