# -*- coding: utf-8 -*-
"""扬声器（播放设备）也进**同一个设备池**：有序候选 + 在位判定 + 回退不静默。

## 为什么要有它

采集那一侧早就有了"按用途挑设备"（`recorder.resolve_input_device`：指令用耳机、
会议用全向麦）—— 2026-09-23 用户实测出来的需求。而**播放这一侧一直是
"系统默认发声"**，于是同一台机器上会出现：

    麦克风选了耳机（为了不把会议室的声音全录进来）
    播报却从会议室音箱出去（把"已发送"念给全场听）

而且两个方向的需求**本来就是对称的**：指令的确认想只让自己听见，
会议的播报想让全场听见。所以设备池不能只做采集。

## 与采集那套的关系：**同一套形状，不是同一个实现**

刻意**不**去改 `recorder.py`（它刚经过 D1/D2 两轮加固、有一整套用例钉着，
为了"对称好看"去重构它的风险远大于收益）。这里照它的形状做一遍：

| 形状 | 采集 | 播放（本模块） |
|---|---|---|
| 用途键 | `INPUT_DEVICE_KEYS` | `OUTPUT_DEVICE_KEYS` |
| 名字当稳定键 | `find_input_device` | `find_output_device` |
| 同一个设备的多个 API | `rank_hostapi` | **复用** `recorder.rank_hostapi` |
| 不在位 | warn + 回退默认 | warn + 回退默认 |

**唯一的实质差别是"池"**：采集那边每个用途只有一个值（历史上只有一个
`inputDeviceId`，迁移过来的），而这里直接做成**有序候选**（`outputDeviceIds`），
取**第一个在位的**。这正是设计 `统一路由` §2 说的那套
"有序候选 / 排除判据 / 健康状态" —— 播放这边先从最小的一步开始：
有顺序、有在位判定、有降级原因（回退时写日志）。

## 一个必须守住的铁律

**绝不用 `sd.default.device[1]` 硬取输出索引。** AGENTS.md 记过一次真实事故：
`sd.default.device` 是 `(输入, 输出)` 二元组，早期把 `[1]` 当输入用，
于是跳过了真正的默认麦克风去打开虚拟设备，在 macOS 上把 CoreAudio HAL 锁死。
输出侧同理：那个下标**会随设备增减平移**，硬取等于把"哪台扬声器"交给运气。
所以本模块的"系统默认"一律表示成 **-1**，调用方**不传 `device` 参数**，
让 PortAudio 自己挑。
"""
from __future__ import annotations

import re
import time

#: 用途 → 它自己的**输出**设备设置键（与 `INPUT_DEVICE_KEYS` 对称）。
OUTPUT_DEVICE_KEYS = {
    "command": "commandOutputDeviceId",   # 指令的语音复述 / 提示音：通常想只让自己听见
    "meeting": "meetingOutputDeviceId",   # 会议相关播报：通常想让全场听见
}

#: 设备清单缓存（给"按名字找设备"用；播放路径上不该每次都查一遍 PortAudio）
_CACHE = {"at": 0.0, "items": []}

#: 系统默认的表示法（**不是** PortAudio 的某个索引 —— 调用方据此决定"传不传 device"）
SYSTEM_DEFAULT = -1


def list_output_devices():
    """输出设备清单。形状与 `recorder.list_input_devices()` 一致，便于面板复用同一套渲染。

    同一个硬件会按 host API 各出现一次（WASAPI / DirectSound / MME …），
    所以带上 `hostapi` 与 `samplerate`；`virtual=True` 表示映射/回环这类**不该选**的端点
    （对输出而言同样成立：把播报送进"立体声混音"等于没人听见）。
    """
    import sounddevice as sd
    from app.audio.recorder import _is_virtual_device
    try:
        apis = {i: a["name"] for i, a in enumerate(sd.query_hostapis())}
    except Exception:
        apis = {}
    out = []
    for d in sd.query_devices():
        if d["max_output_channels"] <= 0:
            continue
        out.append({
            "index": d["index"],
            "name": d["name"],
            "channels": d["max_output_channels"],
            "hostapi": apis.get(d["hostapi"], ""),
            "samplerate": int(d["default_samplerate"] or 0),
            "virtual": bool(_is_virtual_device(d.get("name"))),
        })
    return out


def list_output_devices_cached(max_age=30.0):
    now = time.time()
    if _CACHE["items"] and (now - _CACHE["at"]) < max_age:
        return _CACHE["items"]
    try:
        items = list_output_devices()
    except Exception:
        items = []
    _CACHE["at"] = now
    _CACHE["items"] = items
    return items


def default_output_device():
    """系统默认输出设备（**只用来展示**；不要拿它去 `sd.play(device=…)`）。

    真正的"用系统默认"是**不传 `device`**，见模块注释里那条铁律。
    """
    import sounddevice as sd
    try:
        return sd.default.device[1]     # pair=(输入, 输出)
    except Exception:
        return None


def find_output_device(value):
    """把一个设置值解析成当前的 PortAudio 输出设备索引；找不到返回 None。

    值的两种形态与采集侧一致：
      * 纯数字（含 -1）—— 老配置里的索引，原样返回（负数 = 系统默认）；
      * 设备名 —— **稳定键**：名字对同一台物理设备是稳的，而索引会随在位设备增减平移。
        同名出现在多套 API 时按 `rank_hostapi()` 挑（WASAPI 优先）。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    from app.audio.recorder import rank_hostapi
    best = None
    try:
        for d in list_output_devices_cached():
            if str(d.get("name") or "") != text:
                continue
            rank = rank_hostapi(d.get("hostapi"))
            if best is None or rank < best[0]:
                best = (rank, int(d["index"]))
    except Exception:
        return None
    return best[1] if best else None


def device_is_present(value):
    """这个候选现在在不在位（能不能解析成一个 `>= 0` 的设备索引）。"""
    idx = find_output_device(value)
    return idx is not None and idx >= 0


def _read_setting(key):
    try:
        from app.config import settings
        return settings.get(key, "")
    except Exception:
        return ""


def _warn(key, wanted, why):
    """回退**绝不静默**（与采集侧同一条纪律：宁可回退，但要说清"你要的那个没找到"）。"""
    try:
        import app.db as db
        db.add_log("warn", "audio",
                   "输出设备 %s：配置的「%s」%s，本次用系统默认扬声器"
                   % (key, wanted, why))
    except Exception:
        pass


def resolve_output_device(purpose="command"):
    """按用途解析要用的**输出**设备索引。`-1` = 交给 PortAudio 选系统默认。

    优先级（与采集侧对称）：

        该用途自己的设置（`commandOutputDeviceId` / `meetingOutputDeviceId`）
          → 优先级池 `outputDeviceIds` 里**第一个在位的**
          → -1（系统默认）

    池的语义正是用户要的那句"**用优先级最高的在线设备**"：顺序由用户排，
    "在线"= 现在能在设备清单里解析出来。不在位的候选**跳过并继续往下找**，
    全都不可用才回退系统默认并写一条 warn。

    任何异常都退回 -1：**播放路径不该因为读配置失败就出不了声**。
    """
    try:
        key = OUTPUT_DEVICE_KEYS.get(purpose, "")
    except Exception:
        key = ""
    if key:
        raw = str(_read_setting(key) or "").strip()
        if raw and raw != "-1":
            idx = find_output_device(raw)
            if idx is not None and idx >= 0:
                return idx
            if idx is not None and idx < 0:
                return SYSTEM_DEFAULT
            _warn(key, raw, "不在位（拔了 / 没连上 / 改名了）")

    pool = _read_setting("outputDeviceIds")
    if isinstance(pool, str):
        pool = [p for p in re.split(r"[,\n]", pool) if p.strip()]
    tried = []
    for raw in (pool or []):
        raw = str(raw or "").strip()
        if not raw or raw == "-1":
            continue
        idx = find_output_device(raw)
        if idx is not None and idx >= 0:
            return idx
        tried.append(raw)
    if tried:
        # 池里一个都不在位：**说出来**（否则用户以为配了池、其实一直在用默认）
        _warn("outputDeviceIds", "／".join(tried), "整个优先级池都不在位")
    return SYSTEM_DEFAULT


def play_device_kwargs(purpose="command"):
    """给 `sd.play(...)` 用的参数字典。

    **在位时给 `device=`，系统默认时给空字典** —— 因为"用默认"的正确写法是
    **不传这个参数**，而不是传 `sd.default.device[1]` 那个会漂的下标（见模块注释）。
    """
    idx = resolve_output_device(purpose)
    return {"device": idx} if idx is not None and idx >= 0 else {}


def pool_status():
    """池里每个候选现在的在位情况。给面板用（"我配的那台现在在不在"）。"""
    pool = _read_setting("outputDeviceIds")
    if isinstance(pool, str):
        pool = [p for p in re.split(r"[,\n]", pool) if p.strip()]
    rows = []
    for raw in (pool or []):
        raw = str(raw or "").strip()
        if not raw:
            continue
        rows.append({"value": raw, "present": device_is_present(raw)})
    return rows
