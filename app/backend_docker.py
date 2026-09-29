# -*- coding: utf-8 -*-
"""容器路：**生成/改造 compose**（客户端简化第 3 步 · 批 4）。

这一层只做**一件可验证的事**：把"本机形态的容器后端"渲染成一份 compose 文件，
并把"怎么起、怎么验"说清楚。它**不**替用户拉镜像、不 `docker load`、不 `compose up`
（那属于"一键装好"的编排，等 §7-1 的取向定了再接起来；在此之前，人拿着这份文件两条命令就能起）。

## 为什么"只绑回环"在这条路上是**两处**，而且不是同一个写法

这是这一层最容易写错、也最容易"看着对其实对网段开着"的地方（实施方案 §0 点过这个坑）。
两份东西必须分开看：

| | 写什么 | 为什么 |
|---|---|---|
| **容器里**（`ECHO_LISTEN` / `ECHO_ADMIN_LISTEN`） | `0.0.0.0:8900` / `0.0.0.0:8901` | 容器有**自己的**网络命名空间。发布端口是 DNAT/端口转发到**容器 IP** 的，所以进程必须听在容器自己的通配地址上；听容器内的 `127.0.0.1` 会让发布出去的那个端口**连不上**（docker-proxy 连的是容器 IP，不是容器的回环） |
| **宿主侧**（`ports:`） | `127.0.0.1:8900:8900`、`127.0.0.1:8901:8901` | **可达范围由这里决定**：只发布到宿主回环 ⇒ 网段里谁都连不上，而宿主自己（浏览器、`ssh -L` 隧道）进得去 |

⚠️ 所以实施方案 §0 那句"只绑回环要改两处：`listen: 127.0.0.1:8900` **和** 端口发布
`127.0.0.1:8900:8900`"里的**第一处只适用于"后端以本机进程形态跑"（扩展包路 / 非容器）**；
在容器里照抄会把端口焊死（`server/compose.yaml` 里管理面早就用的是"容器内 0.0.0.0 +
宿主只发布回环"这一套，见那份文件开头的说明 —— 两处口径现在统一了）。
**这一条我在这台机器上没法用 Docker 实测**（本机没有 docker），所以：
写法取"容器内通配 + 宿主只发布回环"（与 `server/compose.yaml` 已验证过的管理面写法一致），
并在下面 `verify_notes()` 里给出**必须在另一台机器上验**的那一步 —— 那一句才是真正的判据。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Sequence

from app import backend_proc, backend_setup, paths

#: 容器里的固定落点（compose 的卷映射与 `ECHO_*` 环境变量都按它写）。
CONTAINER_MODELS = "/opt/echo/models"
CONTAINER_STATE = "/var/echo/state"
CONTAINER_TMP = "/var/echo/tmp"
CONTAINER_CACHE = "/var/echo/cache"

#: 本机形态的镜像 tag（与交付包里那份 `compose.yaml` 同一个默认值）。
DEFAULT_IMAGE = "echo-backend:0.1.0"

#: compose 文件名（放在后端自己的家里，与 `server.yaml` 并排）。
COMPOSE_FILENAME = "compose.yaml"
BACKUP_STAMP = "%Y%m%d-%H%M"


def compose_path() -> str:
    return os.path.join(backend_setup.backend_root(), COMPOSE_FILENAME)


def _yaml_str(value) -> str:
    """YAML 标量：字符串走 `json.dumps`（**反斜杠安全**，Windows 路径含 `\\`）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def render_compose(*, image: str = DEFAULT_IMAGE,
                   port: int = backend_proc.DEFAULT_PORT,
                   admin_port: int = backend_proc.DEFAULT_ADMIN_PORT,
                   models_dir: str = "", state_dir: str = "", tmp_dir: str = "",
                   cache_dir: str = "", jwt_secret: str = "",
                   max_concurrent: int = 6, platform: str = "") -> str:
    """渲染本机形态的 `compose.yaml`（**纯函数**：不读不写盘，方便用例逐字比）。

    ``jwt_secret`` 给了就一起把鉴权打开（`ECHO_AUTH_ENABLED/MODE/JWT_SECRET`）——
    这三项**必须一起开**，只开一半的症状很误导人：服务端照常起、`/v1/health` 照常 200，
    而客户端配对成功后 `POST /v1/token` 回 503 `auth_misconfigured`
    （`server/compose.yaml` 里那段注释记的就是这个坑）。
    """
    root = backend_setup.backend_root()
    models = models_dir or _default_models_dir()
    state = state_dir or backend_setup.state_root()
    tmp = tmp_dir or backend_setup.tmp_root()
    cache = cache_dir or backend_setup.cache_root()
    lines: List[str] = [
        "# ECHO 能力后端（**本机形态**，容器）—— 由客户端「帮我起本机后端」的容器路生成。",
        "#",
        "# 只服务这台机器：",
        "#   * 宿主侧只把两个端口发布到 **127.0.0.1**（网段里连不上）；",
        "#   * 容器里绑通配（`0.0.0.0`）是**必需**的 —— 发布是转发到容器 IP 的，",
        "#     容器内听回环会让发布出去的端口连不上。可达范围由上面的发布规则决定。",
        "#",
        "# 起它：  docker compose -f %s up -d" % compose_path(),
        "# 看日志：docker compose -f %s logs -f" % compose_path(),
        "# 停它：  docker compose -f %s down" % compose_path(),
        "",
        "services:",
        "  echo-backend:",
        "    image: %s" % _yaml_str(image),
        "    container_name: %s" % _yaml_str("echo-backend"),
        "    restart: %s" % _yaml_str("unless-stopped"),
    ]
    if platform:
        lines.append("    platform: %s" % _yaml_str(platform))
    lines += [
        "    # 只发布到**宿主回环**：这一段就是「只服务本机」的全部依据",
        "    ports:",
        "      - %s" % _yaml_str("127.0.0.1:%d:8900" % int(port)),
        "      - %s" % _yaml_str("127.0.0.1:%d:8901" % int(admin_port)),
        "    environment:",
        "      # 容器里必须绑通配（见文件头那张表）——绑回环 = 发布出去也没人应答",
        "      ECHO_LISTEN: %s" % _yaml_str("0.0.0.0:8900"),
        "      ECHO_ADMIN_LISTEN: %s" % _yaml_str("0.0.0.0:8901"),
        "      ECHO_MODELS_ROOT: %s" % _yaml_str(CONTAINER_MODELS),
        "      ECHO_TMP_ROOT: %s" % _yaml_str(CONTAINER_TMP),
        "      ECHO_STATE_ROOT: %s" % _yaml_str(CONTAINER_STATE),
        "      # 本机自配对：同机不该让人抄配对码；客户端读 state 卷里那份 local-pair.json",
        "      ECHO_LOCAL_PAIR: %s" % _yaml_str("true"),
        "      ECHO_MAX_CONCURRENT: %s" % _yaml_str(str(int(max_concurrent))),
        "      # 缓存与家目录都要**可写**：根文件系统是只读的，而 funasr 会现拉 vad 模型",
        "      MODELSCOPE_CACHE: %s" % _yaml_str(CONTAINER_CACHE + "/modelscope"),
        "      HOME: %s" % _yaml_str(CONTAINER_CACHE),
    ]
    if jwt_secret:
        lines += [
            "      # 鉴权三件套**要么一起开、要么都别开**（只开一半：/v1/health 照常 200，",
            "      # 而 /v1/token 回 503 auth_misconfigured —— 看着像客户端坏了）",
            "      ECHO_AUTH_ENABLED: %s" % _yaml_str("true"),
            "      ECHO_AUTH_MODE: %s" % _yaml_str("jwt"),
            "      ECHO_JWT_SECRET: %s" % _yaml_str(jwt_secret),
        ]
    lines += [
        "    volumes:",
        "      # 权重只读挂进来（几 GB，不该跟着镜像走）",
        "      - %s" % _yaml_str("%s:%s:ro" % (models, CONTAINER_MODELS)),
        "      # 耐久状态（auth.db + local-pair.json）：**换镜像/升级都别动它**",
        "      - %s" % _yaml_str("%s:%s" % (state, CONTAINER_STATE)),
        "      - %s" % _yaml_str("%s:%s" % (tmp, CONTAINER_TMP)),
        "      - %s" % _yaml_str("%s:%s" % (cache, CONTAINER_CACHE)),
        "    # 根只读：`docker cp` 到容器里必然失败，送文件要走上面那几个可写卷",
        "    read_only: true",
        "    tmpfs:",
        "      - %s" % _yaml_str("/tmp"),
        "",
    ]
    return "\n".join(lines)


def _default_models_dir() -> str:
    """容器路默认挂哪份模型库：**客户端那份**（不复制；实施方案 §1A）。"""
    try:
        return paths.models_root()
    except Exception:                                             # pragma: no cover - 兜底
        return ""


def write_compose(**kw) -> Dict[str, Any]:
    """写 `{backend}/compose.yaml`（幂等；内容变了先备份）→ ``{"ok","path","detail","backup"}``。"""
    text = render_compose(**kw)
    path = compose_path()
    info: Dict[str, Any] = {"ok": True, "path": path, "detail": "", "backup": ""}
    for d in (backend_setup.backend_root(), backend_setup.state_root(),
              backend_setup.tmp_root(), backend_setup.cache_root()):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            info.update({"ok": False, "detail": "建不了目录 %s：%s" % (d, e)})
            return info
    old = ""
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                old = fh.read()
        except Exception:
            old = ""
        if old != text:
            backup = "%s.bak-%s" % (path, time.strftime(BACKUP_STAMP))
            try:
                with open(backup, "w", encoding="utf-8") as fh:
                    fh.write(old)
                info["backup"] = backup
            except Exception:
                pass
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception as e:
        info.update({"ok": False, "detail": "写不了 %s：%s" % (path, e)})
        return info
    detail = "compose 已写好：%s" % path
    if info["backup"]:
        detail += "；原文件已备份为 %s" % info["backup"]
    info["detail"] = detail
    return info


def commands(port: int = backend_proc.DEFAULT_PORT) -> List[Dict[str, str]]:
    """起它 / 看日志 / 停它 —— 面板与文档共用同一份（别在两处各写一遍命令）。"""
    path = compose_path()
    return [
        {"label": "起", "command": "docker compose -f %s up -d" % path},
        {"label": "看日志", "command": "docker compose -f %s logs -f" % path},
        {"label": "停", "command": "docker compose -f %s down" % path},
    ]


def verify_notes(port: int = backend_proc.DEFAULT_PORT,
                 admin_port: int = backend_proc.DEFAULT_ADMIN_PORT) -> List[str]:
    """起完**必须**验的那几步。**"只绑回环"的假安全只有从另一台机器上才测得出来。**

    （实施方案 §5 的批 4 验收写的就是这一条："另一台机器 curl 宿主 IP:8900 必须连不上"。）
    """
    return [
        "本机：`docker compose -f %s config` 要过（渲染有没有问题，它先说话）" % compose_path(),
        "本机：curl http://127.0.0.1:%d/v1/health 应当 200" % int(port),
        "**另一台机器**：curl http://<这台机器的 IP>:%d/v1/health 必须**连不上** —— "
        "连得上说明端口被发布到网段了（那正是「以为只绑了回环」的假安全）" % int(port),
        "另一台机器：curl http://<这台机器的 IP>:%d/v1/health 也必须连不上（管理面同理）"
        % int(admin_port),
    ]


def available() -> Dict[str, Any]:
    """本机能不能用这条容器路（复用批 2 的探测，**只读**）。"""
    try:
        from app import backend_env
        info = backend_env.probe()["docker"]
    except Exception as e:                                        # pragma: no cover - 兜底
        return {"ok": False, "detail": "探测 Docker 失败：%s" % e}
    ok = bool(info.get("installed") and info.get("daemon"))
    detail = ""
    if not ok:
        detail = info.get("error") or "本机没有可用的 Docker"
    return {"ok": ok, "detail": detail, "version": info.get("version") or "",
            "compose": info.get("compose") or "", "gpuRuntime": bool(info.get("gpuRuntime"))}
