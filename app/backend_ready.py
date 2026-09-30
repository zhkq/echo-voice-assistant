# -*- coding: utf-8 -*-
"""「这台后端到底能不能干活」——**三层就绪**（客户端简化第 3 步 · 批 3）。

为什么要三层，而不是"`/v1/health` 绿了就算好"
--------------------------------------------
`/v1/health` 是**进程层**的"我还活着"：它 200 只说明 HTTP 服务在跑。
而这个项目真踩过的坑是"**health 全绿，但每个 `/v1/asr` 都 503 `model_failed`**"
（权重缺失 / torch 与 torchaudio 的 CUDA ABI 不符，见 `AGENTS.md`）—— 那种状态下面板会说
"后端可用"，用户等到开会才发现一个字都转不出来。所以三层缺一不可：

    L1  ``GET /v1/health``  进程在（免鉴权）
    L2  ``GET /v1/ready``   服务端自己说"声明的模型都加载好了"（503 时把 ``failed[]`` 原样报出）
    L3  ``POST /v1/asr``    **一次真音频自测**：客户端现场生成 1 秒 16k 单声道 wav，
                            走真的鉴权、真的推理，拿回非空文本才算数

只有 L3 能挡住上面那个坑 —— 它是这一层存在的理由（实施方案 §2 的"三层就绪（缺一不可）"）。

L4（说话人分离）**是可选的、而且"做不了"不算失败**：老卡（sm_<7.5）本来就没有这一档，
如实说"这块卡做不了分离"即可（实施方案 §2 / §4）。

顺序与开销
----------
L2 绿了再跑 L3：`/v1/ready` 说模型没就绪时，L3 必然 503（而且会等一次加载超时），
那是白等。反过来 L2 绿 ⇒ 权重已加载 ⇒ L3 只是一次短推理（1 秒音频），快。
所以 ``probe()`` 里 L3 只在 L2 通过时才跑，并在结果里写明"为什么没跑 L3"。

只读保证
--------
这一层**不改任何东西**：它生成的 wav 落在系统临时目录（用完即删），配对/配置一概不碰。
"""
from __future__ import annotations

import json
import os
import struct
import tempfile
import time
import wave
from typing import Any, Dict, List, Optional, Tuple

#: 三层各自的超时（秒）。L1/L2 是"问一句"，L3 是"真跑一次小推理"。
L1_TIMEOUT_S = 5.0
L2_TIMEOUT_S = 10.0
L3_TIMEOUT_S = 120.0
#: 等能力端口**真的开始应答**的上限（秒）与轮询间隔。
#:
#: 为什么必须有它（2026-09-30 真机实测逮到）：uvicorn **先跑 lifespan、后绑端口**，
#: 而本机配对文件正是在 lifespan 里写的 —— 所以"配对文件在了"之后的一小段里
#: `/v1/health` 是**连接被拒**。实测那次表现为 L1/L2 报失败、几秒后 L3 却 200（自相矛盾），
#: 而用户读到的是"后端没在应答"这种**误报**。同一类窗口在 1c 的配对那一步已经有过
#: （`backend_setup.PAIR_READY_TIMEOUT`），这里是它的第二处。
WAIT_HEALTH_S = 20.0
WAIT_HEALTH_POLL_S = 0.5
#: 自测音频：1 秒 / 16 kHz / 单声道（小到不占带宽，又足以让模型真的跑一遍）。
SELFTEST_SECONDS = 1.0
SELFTEST_RATE = 16000


def make_selftest_wav(path: str, seconds: float = SELFTEST_SECONDS,
                      rate: int = SELFTEST_RATE) -> str:
    """现场写一个 1 秒 16k 单声道的 wav（**内容是人声频段的低幅正弦**）。

    为什么不用"全零静音"：静音也能过（模型照样回一段空/短文本），但有些实现会把它
    当成"无有效音频"而报错 —— 那时失败原因会被误读成"后端坏了"。给一段稳定的音调，
    测的就是**链路**，不是模型的识别质量（自测不看文本内容，只看"有没有非空结果"）。
    """
    frames = int(rate * max(0.1, float(seconds)))
    amp = 6000
    freq = 440.0
    data = bytearray()
    for i in range(frames):
        value = int(amp * (0.6 * _sin(2.0 * 3.141592653589793 * freq * i / rate)
                           + 0.4 * _sin(2.0 * 3.141592653589793 * 2.0 * freq * i / rate)))
        data += struct.pack("<h", max(-32768, min(32767, value)))
    with wave.open(path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(bytes(data))
    return path


def _sin(x: float) -> float:
    """`math.sin` 的薄包装（本模块不 import math 之外的任何东西，方便被单独引）。"""
    import math
    return math.sin(x)


# ---------------------------------------------------------------- HTTP（回环直连）

def _get_json(url: str, timeout: float) -> Tuple[int, Any, str]:
    """GET 一个 JSON 接口 → ``(状态码, body, 原文/错误)``。**永不抛。**

    走 `app.netlocal`（回环**不经过系统代理**）：这台机器上真发生过"系统代理把
    `127.0.0.1` 的探活劫走"，表现是"服务在跑却显示连不上"（见 `docs/install-feedback`）。
    """
    try:
        from app import netlocal
        with netlocal.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return int(resp.status), json.loads(raw), raw
            except Exception:
                return int(resp.status), None, raw[:500]
    except Exception as e:
        status = 0
        body = None
        # `HTTPError` 也是"服务回答了"（503 就是我们要读的那种回答）
        code = getattr(e, "code", 0)
        if code:
            status = int(code)
            try:
                raw = e.read().decode("utf-8", "replace")
                body = json.loads(raw)
            except Exception:
                body = None
        return status, body, str(e)


def probe_health(base_url: str, timeout: float = L1_TIMEOUT_S) -> Dict[str, Any]:
    """**L1**：`GET /v1/health`（免鉴权）→ ``{"ok", "status", "health", "detail"}``。"""
    status, body, raw = _get_json(base_url.rstrip("/") + "/v1/health", timeout)
    ok = status == 200 and isinstance(body, dict)
    health = body if isinstance(body, dict) else {}
    detail = ""
    if not ok:
        detail = ("后端回答了 HTTP %s" % status) if status else ("连不上 %s（%s）"
                                                              % (base_url, raw))
    return {"ok": ok, "status": status, "health": health, "detail": detail}


def probe_ready(base_url: str, timeout: float = L2_TIMEOUT_S) -> Dict[str, Any]:
    """**L2**：`GET /v1/ready` → ``{"ok", "status", "failed", "detail"}``。

    503 时把服务端给的 ``failed[]`` **原样报出**（哪个模型没就绪、为什么）——
    转述成"模型没就绪"会丢掉"缺哪一棵/加载报错原文"这些真正能动手的信息。
    """
    status, body, raw = _get_json(base_url.rstrip("/") + "/v1/ready", timeout)
    failed: List[Any] = []
    detail = ""
    if isinstance(body, dict):
        for key in ("failed", "models", "notReady"):
            got = body.get(key)
            if isinstance(got, list) and got:
                failed = got
                break
        detail = str(body.get("detail") or body.get("message") or "")[:500]
    ok = status == 200
    if not ok and not detail:
        if status:
            detail = "服务端回了 HTTP %s：%s" % (status, (raw or "")[:300])
        else:
            detail = "连不上 %s（%s）" % (base_url, raw)
    return {"ok": ok, "status": status, "failed": failed, "detail": detail,
            "body": body if isinstance(body, dict) else {}}


def probe_asr(base_url: str, token: str = "", timeout: float = L3_TIMEOUT_S,
              wav_path: str = "") -> Dict[str, Any]:
    """**L3**：拿一段**真的** 1 秒音频打 `POST /v1/asr` → ``{"ok", "status", "text", "detail"}``。

    走真的鉴权（`Authorization: Bearer`）与真的推理 —— 这一层要挡的正是
    "health 全绿但每个 /v1/asr 都 503"（权重缺失 / ABI 不符）。

    **没有令牌时也发这一次**（服务端可能没开鉴权）：先假装"跳过了自测"，
    会让"没配对"看起来像"自测通过"。被鉴权拒绝（401/403）而手里又没有令牌时，
    那句话里要**同时**说出这两件事，否则用户会去查模型。
    """
    if not wav_path:
        tmp = tempfile.mkdtemp(prefix="echo-backend-selftest-")
        wav_path = make_selftest_wav(os.path.join(tmp, "selftest.wav"))
    try:
        with open(wav_path, "rb") as fh:
            audio = fh.read()
    except Exception as e:
        return {"ok": False, "status": 0, "text": "", "token": bool(token),
                "detail": "自测音频读不出来：%s" % e}
    url = base_url.rstrip("/") + "/v1/asr"
    headers = {"Content-Type": "audio/wav"}
    if token:
        headers["Authorization"] = "Bearer " + token
    status, body, raw = 0, None, ""
    try:
        from app import netlocal
        import urllib.request
        req = urllib.request.Request(url, data=audio, headers=headers, method="POST")
        with netlocal.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
            raw = resp.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except Exception:
                body = None
    except Exception as e:
        code = getattr(e, "code", 0)
        raw = str(e)
        if code:
            status = int(code)
            try:
                raw = e.read().decode("utf-8", "replace")
                body = json.loads(raw)
            except Exception:
                body = None
    text = ""
    detail = ""
    model_id = ""
    if isinstance(body, dict):
        text = str(body.get("text") or "").strip()
        model_id = str(body.get("modelId") or body.get("modelVersion") or "")
        detail = str(body.get("detail") or body.get("error") or "")[:500]
    # **判据**（2026-09-30 真机校准）：引擎**真的跑完了**才算过 —— `200 + modelId/modelVersion`
    # 就是"池子解析出了 spec 并跑完了"的凭据（加载失败会在更早的地方变成 503 model_failed）。
    # 文本为空**不算失败**：自测音频是合成音调，模型跑完但没出字是正常的
    # （实测：qwen3asr 对 1 秒纯音调给空文本、SenseVoice 给了个 "Yeah." —— 两者都是"跑完了"）。
    # 第一版把"非空文本"当判据，于是把一个**完全正常**的后端判成失败（用户会去修不存在的问题）。
    ran = bool(status == 200 and (text or model_id))
    ok = bool(ran)
    text_empty = bool(status == 200 and not text)
    if not ok and not detail:
        detail = ("服务端回了 HTTP %s：%s" % (status, (raw or "")[:300])) if status \
            else "连不上 %s（%s）" % (url, raw)
    if status == 200 and not text and not model_id:
        detail = "后端回了 200，但既没有文本也没有 modelId —— 看不出引擎到底跑没跑"
    if text_empty and ok:
        detail = "引擎跑完了（%s），但这段合成音调没有出文本 —— 这是正常的" % (model_id or "?")
    if not ok and not token and status in (401, 403):
        detail = ("被鉴权拒了（HTTP %s），而且**手里没有可用令牌**（还没配对？）—— "
                  "先配对再做自测；这两件事要分开看，不然会去查模型。%s"
                  % (status, ("服务端原话：" + detail) if detail else ""))
    return {"ok": ok, "status": status, "text": text[:200], "detail": detail,
            "textEmpty": text_empty, "modelId": model_id,
            "token": bool(token), "selfTestWav": wav_path}


# ---------------------------------------------------------------- 三层合起来

def _token_for(base_url: str) -> str:
    """拿一枚**这次自测真的能用**的令牌。拿不到就空串（由调用方如实说"没有令牌"）。

    刻意复用能力层那一条**已经验证过**的路，而不是自己再写一遍"配对凭据 → 换令牌"：
      * 手填的静态/手工令牌（`capabilityEchoServerToken` / `…StaticToken`）优先；
      * 否则用本机配对凭据去换短期令牌（`pairing.ensure_token`，会自己缓存/续期）。
    自己写第二套的代价是"自测用的令牌与真实调用用的不是同一枚" —— 那就测不出真问题。
    """
    try:
        from app.capabilities import echo_server
        client = echo_server.EchoServerClient(base_url=base_url)
        header = client._auth_header(slot="asr.text")
        return header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    except Exception:
        return ""


def probe(base_url: str, token: str = "", *, timeout_l3: float = L3_TIMEOUT_S,
          diarize: bool = False, wait_health: float = WAIT_HEALTH_S) -> Dict[str, Any]:
    """三层就绪 + 可选的 L4（分离）→ 面板能直接渲染的一份结论。

    ``ok`` 只在 **L1+L2+L3 都过** 时为真。``state`` 是互斥的一档，面板只渲染一句：

      * ``not-running``  —— L1 就没过（后端没起来 / 地址不对）
      * ``not-ready``    —— 进程在，但服务端说模型没就绪（带 `failed[]`）
      * ``asr-failed``   —— 模型就绪了，真实自测却失败（**这就是那个坑**）
      * ``ok``           —— 三层都过

    ``wait_health``：**先等它开始应答**再判 L1（默认等 `WAIT_HEALTH_S` 秒）。
    不这么做的话，"刚起好、端口还没绑上"会被报成"后端没在应答"（2026-09-30 实测）。
    给 0 = 不等待（用例要确定性的那种场景）。
    """
    out: Dict[str, Any] = {"baseUrl": str(base_url or ""), "at": time.strftime("%H:%M:%S")}
    deadline = time.monotonic() + max(0.0, float(wait_health or 0.0))
    while True:
        l1 = probe_health(base_url)
        if l1["ok"] or time.monotonic() >= deadline:
            break
        time.sleep(WAIT_HEALTH_POLL_S)
    out["l1"] = l1
    out["waitedS"] = round(max(0.0, float(wait_health or 0.0) - max(0.0, deadline - time.monotonic())), 1)
    if not l1["ok"]:
        out.update({"ok": False, "state": "not-running",
                    "headline": "后端没在应答（%s）" % (l1["detail"] or "连不上"),
                    "l2": None, "l3": None})
        return out
    l2 = probe_ready(base_url)
    out["l2"] = l2
    if not l2["ok"]:
        failed = "；".join(str(x) for x in (l2.get("failed") or [])[:5])
        out.update({"ok": False, "state": "not-ready",
                    "headline": "后端在跑，但模型还没就绪：%s" % (failed or l2["detail"]),
                    "l3": None})
        return out
    l3 = probe_asr(base_url, token=token or _token_for(base_url), timeout=timeout_l3)
    out["l3"] = l3
    if not l3["ok"]:
        out.update({"ok": False, "state": "asr-failed",
                    "headline": "模型就绪，但真实自测失败：%s（这一档正是"
                                "「health 全绿、每个 /v1/asr 都 503」那个坑）" % l3["detail"]})
        return out
    ok = True
    out.update({"ok": True, "state": "ok",
                "headline": "三层都过了：/v1/health · /v1/ready · 一次真实转写（%s）"
                            % (l3["text"][:40] or ("引擎跑通，合成音无文本"
                                                  if l3.get("textEmpty") else "有文本"))})
    if diarize:
        out["l4"] = probe_diarize(base_url, token=token or _token_for(base_url),
                                  timeout=timeout_l3,
                                  wav_path=str(l3.get("selfTestWav") or ""))
        # L4 失败**不算整体失败**（老卡本来就没有这一档），只在 headline 里如实带一句
        if not out["l4"]["ok"]:
            out["headline"] += "；分离这一档不可用：%s" % out["l4"]["detail"]
    return out


def probe_diarize(base_url: str, token: str = "", timeout: float = L3_TIMEOUT_S,
                  wav_path: str = "") -> Dict[str, Any]:
    """**L4**：`POST /v1/diarize`（可选档）。**"这块卡做不了分离"不算失败**，如实说即可。"""
    if not token:
        return {"ok": False, "status": 0, "detail": "没有令牌（没配对）"}
    if not wav_path or not os.path.isfile(wav_path):
        return {"ok": False, "status": 0, "detail": "没有自测音频（L3 没跑到）"}
    try:
        with open(wav_path, "rb") as fh:
            audio = fh.read()
    except Exception as e:
        return {"ok": False, "status": 0, "detail": "自测音频读不出来：%s" % e}
    url = base_url.rstrip("/") + "/v1/diarize"
    status, body, raw = 0, None, ""
    try:
        from app import netlocal
        import urllib.request
        req = urllib.request.Request(
            url, data=audio, method="POST",
            headers={"Content-Type": "audio/wav", "Authorization": "Bearer " + token})
        with netlocal.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
            raw = resp.read().decode("utf-8", "replace")
            body = json.loads(raw) if raw else None
    except Exception as e:
        code = getattr(e, "code", 0)
        raw = str(e)
        if code:
            status = int(code)
            try:
                raw = e.read().decode("utf-8", "replace")
                body = json.loads(raw)
            except Exception:
                body = None
    turns = []
    detail = ""
    if isinstance(body, dict):
        turns = body.get("turns") or []
        detail = str(body.get("detail") or body.get("error") or "")[:300]
    ok = status == 200
    if not ok and not detail:
        detail = ("服务端回了 HTTP %s：%s" % (status, (raw or "")[:200])) if status \
            else "连不上 %s（%s）" % (url, raw)
    return {"ok": ok, "status": status, "turns": len(turns), "detail": detail}
