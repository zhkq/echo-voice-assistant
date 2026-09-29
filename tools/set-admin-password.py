# -*- coding: utf-8 -*-
"""改 ECHO 能力后端的管理员口令。

为什么要这么改：管理员账号**只能从命令行建/改**（管理面刻意不提供改账号功能 ——
"改谁能进这扇门"的动作不该在门里面做）。而 `--new-admin` 只会随机生成一个只打印
一次的口令。这个脚本用同一套底层（`settings.load` → `auth.open_store` →
`admin.hash_password`，scrypt），把你**自己指定的口令**写进去，并就地验证一次登录。

## 怎么用（改下面 USERNAME / NEW_PASSWORD 两行，然后跑一条命令）

**A. 本机（这台开发机）的后端：**
    cd C:\\echo-dev
    .\\venv\\Scripts\\python.exe tools\\set-admin-password.py

**B. .30 那台（容器里的新后端）：**（口令要改成你自己的）
    cd C:\\echo-dev
    type tools\\set-admin-password.py | ssh zhkq@192.168.1.30 "sudo docker exec -i echo-backend python -"

跑完会打印两行：`已设置 … 的口令` 与 `登录验证 -> 200 OK`。
（若登录验证失败，说明写库与读库不是同一个 —— 把输出发我。）

## 注意
* 口令是**明文写在这个文件里**的。改完记得别再提交这个文件（它在 `tools/` 下，
  不是交付内容）；也可以把 `NEW_PASSWORD` 留在空串、让它从环境变量读。
* 改的是**管理面账号**（用于登录 /admin/ ），与客户端配对码、JWT secret 无关。
"""
import json
import os
import sys
import urllib.request

# ------------------------------------------------------------------ 改这两行
USERNAME = "zhkq"
NEW_PASSWORD = "Zhoukq711@0502"
# -----------------------------------------------------------------------------

# 容器里是 /etc/echo/server.yaml；开发机是 data\backend-dev\server.yaml（按 cwd 解析）
CANDIDATES = ["/etc/echo/server.yaml", os.path.join("data", "backend-dev", "server.yaml")]

sys.path.insert(0, os.getcwd())
try:
    from server import settings as S, auth as A, admin as M       # noqa: E402
except Exception as e:                                            # pragma: no cover
    print("导入 server 失败（要在仓库根目录下跑，或用管道进容器）：%s" % e)
    raise SystemExit(2)

if not NEW_PASSWORD:
    NEW_PASSWORD = os.environ.get("ECHO_NEW_PASSWORD", "")
if not NEW_PASSWORD:
    print("没给口令：改脚本里的 NEW_PASSWORD，或设环境变量 ECHO_NEW_PASSWORD")
    raise SystemExit(2)

cfg_path = next((p for p in CANDIDATES if os.path.exists(p)), None)
if not cfg_path:
    print("找不到配置文件，试过：%s" % CANDIDATES)
    raise SystemExit(2)

cfg = S.load(cfg_path)
store = A.open_store(cfg)
store.upsert_admin(USERNAME, M.hash_password(NEW_PASSWORD))
print("已设置 %s 的口令（配置：%s，库：%s）"
      % (USERNAME, cfg_path, getattr(store, "path", "?")))

# 就地验一次登录：管理面是明文 http，绑 0.0.0.0 时按本机回环打
listen = str(cfg.get("server.admin_listen", "") or "127.0.0.1:8901")
host, _, port = listen.rpartition(":")
host = host.strip("[]") or "127.0.0.1"
if host in ("0.0.0.0", "::", "*", ""):
    host = "127.0.0.1"
url = "http://%s:%s/admin/api/login" % (host, port)
req = urllib.request.Request(
    url, data=json.dumps({"username": USERNAME, "password": NEW_PASSWORD}).encode("utf-8"),
    headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=8) as r:
        print("登录验证 -> %s OK  (%s)" % (r.status, url))
except Exception as e:
    print("登录验证失败：%s %s  (%s)" % (type(e).__name__, e, url))
    print("提示：这通常意味着写入的库与运行中的服务读的不是同一个 —— 把上面这行输出发我。")
