# -*- coding: utf-8 -*-
"""后端凭据的落盘边界（设计 §7.5 ⑥：**客户端侧的安全边界**）。

配对换来的 `client_id` + `secret` 存在 `{DATA}/backend.json`。这个文件是
**这套方案里最值得保护的一件东西** —— 拿到它就能以这台机器的身份去用那台 GPU。

## 两条保护，按平台各用一条

| 平台 | 手段 | 为什么 |
|---|---|---|
| Windows | **DPAPI**（`CryptProtectData`，用户作用域） | 绑到**用户账户**：换个账户/把文件拷走都解不开。用 `ctypes` 调系统 API —— 不引新依赖 |
| Linux / macOS | 文件权限 `0600` | POSIX 上没有等价的"绑用户"系统服务，权限就是那道墙 |

Windows 上**同时也**把权限收紧（`icacls` 太吵，这里只依赖 DPAPI）——
但文件仍落在用户目录下，且不写进 `settings` 表：

> **为什么不跟 provider 密钥一样存 SQLite？** 那条路今天能用，但它是**明文**的
> （只在接口层遮罩）。后端密钥比 provider 密钥更值钱：provider 泄露=花你的额度，
> 后端密钥泄露=**用你的显卡 + 以你的身份出现在审计里**。所以它单独一条更硬的路。

## 三条纪律

1. **明文 secret 绝不落盘**（Windows 上落的是 DPAPI 密文；POSIX 上落明文但 0600）——
   由用例直接读文件字节来验，不靠注释保证。
2. **读坏了不许炸**：文件损坏/权限不对 → 返回 `None`（= 没配对），
   让上层走"重新配对"，而不是让整个面板起不来。
3. **secret 不进日志、不进错误信息**：`BackendCredentials.__repr__` 只给前后各 3 位。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

#: DPAPI 的密文信封标记（写进 JSON，读的时候据此选解密路径）。
_ENC_DPAPI = "dpapi"
_ENC_PLAIN = "plain"

#: 配对凭据的文件名（设计 §7.5 ⑥ 定的 `{echoBase}/data/backend.json`）
FILENAME = "backend.json"


def credentials_path() -> str:
    """凭据文件路径。跟随 `paths.data_root()`（用户改过数据目录就跟着走）。"""
    try:
        from app import paths
        root = paths.data_root()
    except Exception:
        root = os.path.join(os.path.expanduser("~"), ".echo", "data")
    return os.path.join(root, FILENAME)


# ---------------------------------------------------------------- DPAPI

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi(protect: bool, data: bytes) -> bytes:
    """`CryptProtectData` / `CryptUnprotectData`（**用户作用域**，不弹 UI）。

    `CRYPTPROTECT_UI_FORBIDDEN` 是必须的：ECHO 是后台进程，
    少了它某些情况下会弹一个**没人能点**的系统对话框把调用挂住。

    `argtypes` 全部显式声明：x64 下不声明的话，结构体指针会被按 32 位截断，
    表现为"偶尔解密失败"这种极难查的错。
    """
    from ctypes import wintypes
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    buf = ctypes.create_string_buffer(bytes(data), len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    UI_FORBIDDEN = 0x1
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), "ECHO backend credential",
                                      None, None, None, UI_FORBIDDEN,
                                      ctypes.byref(blob_out))
    else:
        descr = ctypes.c_wchar_p()
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), ctypes.byref(descr),
                                        None, None, None, UI_FORBIDDEN,
                                        ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _seal(plaintext: str) -> Dict[str, str]:
    """secret → 落盘用的信封。Windows 上是 DPAPI 密文，别处是明文 + 0600。"""
    raw = plaintext.encode("utf-8")
    if _dpapi_available():
        return {"enc": _ENC_DPAPI,
                "value": base64.b64encode(_dpapi(True, raw)).decode("ascii")}
    return {"enc": _ENC_PLAIN, "value": base64.b64encode(raw).decode("ascii")}


def _open(envelope: Any) -> str:
    if not isinstance(envelope, dict):
        return ""
    enc = str(envelope.get("enc") or "")
    try:
        blob = base64.b64decode(str(envelope.get("value") or ""))
    except Exception:
        return ""
    if enc == _ENC_DPAPI:
        if not _dpapi_available():
            # 把 Windows 上配的文件拷到了 Linux：**解不开就说解不开**，
            # 不要退回明文路径去猜
            return ""
        try:
            return _dpapi(False, blob).decode("utf-8")
        except Exception:
            return ""
    if enc == _ENC_PLAIN:
        try:
            return blob.decode("utf-8")
        except Exception:
            return ""
    return ""


# ---------------------------------------------------------------- 数据

@dataclass
class BackendCredentials:
    """一台后端 + 这台机器的身份。`secret` 只在内存里；落盘时被 `_seal` 包起来。"""
    base_url: str = ""
    client_id: str = ""
    secret: str = ""
    server_name: str = ""
    #: 配对时记下的服务端证书指纹（设计 §7.5 ①：配对顺带交换信任，防中间人）
    cert_fingerprint: str = ""
    paired_at: float = field(default_factory=time.time)
    #: 最近一次换到的短期令牌（**不落盘**：它是短命的，重启后重新换即可）
    access_token: str = ""
    token_expires_at: float = 0.0

    def token_fresh(self, skew_s: float = 60.0) -> bool:
        """手上的令牌还能用吗（留 `skew_s` 余量 —— 内网时钟未必准）。"""
        return bool(self.access_token) and time.time() < (self.token_expires_at - skew_s)

    def __repr__(self) -> str:                                  # pragma: no cover
        # **secret 绝不原样出现**，连 repr 都不行（它会进日志、进异常栈）
        masked = ("%s…%s" % (self.secret[:3], self.secret[-3:])) if len(self.secret) > 8 else "***"
        return ("BackendCredentials(base_url=%r, client_id=%r, secret=%s, server_name=%r)"
                % (self.base_url, self.client_id, masked, self.server_name))

    __str__ = __repr__


# ---------------------------------------------------------------- 读写

def save(creds: BackendCredentials) -> str:
    """落盘。返回文件路径。`secret` 与 `access_token` 分别处理（后者**不落**）。"""
    path = credentials_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    payload = asdict(creds)
    payload.pop("access_token", None)          # 短命令牌不落盘：重启后重新换
    payload.pop("token_expires_at", None)
    payload["secret"] = _seal(creds.secret)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    _restrict(tmp)
    os.replace(tmp, path)                      # 原子替换：别让半截文件留在那儿
    _restrict(path)
    return path


def _restrict(path: str) -> None:
    """POSIX 上收紧到 0600。Windows 上靠 DPAPI（文件本身在用户目录内）。"""
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def load() -> Optional[BackendCredentials]:
    """读凭据。**没有/坏了都返回 None**（= 没配对），绝不抛 —— 面板不该因此起不来。"""
    path = credentials_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    secret = _open(data.get("secret"))
    if not (data.get("client_id") and secret):
        return None
    return BackendCredentials(
        base_url=str(data.get("base_url") or ""),
        client_id=str(data.get("client_id") or ""),
        secret=secret,
        server_name=str(data.get("server_name") or ""),
        cert_fingerprint=str(data.get("cert_fingerprint") or ""),
        paired_at=float(data.get("paired_at") or 0.0),
    )


def clear() -> bool:
    """忘掉配对（用户点"解除配对"）。文件不存在也算成功。"""
    path = credentials_path()
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False
