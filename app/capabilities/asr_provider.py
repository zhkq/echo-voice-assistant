# -*- coding: utf-8 -*-
"""在线转写后端（会议转写方案 3）：千问AI平台 `qwen-audio-3.1-asr-flash-filetrans`。

**平台是千问AI平台（`https://maas.qianwenaiapi.com`），不是阿里云百炼** ——
两者接口形状几乎一样（同一套 DashScope 血统），但**域名与 API Key 都不通用**。
2026-09-29 对着平台文档逐条核过一遍，本文件里的路径/字段/请求头与文档一致。

设计见 `docs/3.0-设计总览与组件关系.md` §6.6。三句话概括：

* **整场、异步、不切片** —— 说话人分离本质是**全局聚类**。切片会把一场会切成"每片各自
  聚类"，再靠声纹缝合（`SpeakerRegistry`），那正是本地那条路的误差来源。
* **音频不必自备 OSS** —— 平台有"上传本地文件获取临时 URL"通道
  （`GET /api/v1/uploads?action=getPolicy&model=…` → 传到 `upload_host` → `oss://…`，
  48 小时有效、凭证 300 秒、**该接口限流 100 QPS 且不可扩容**）。
* **它只回 `speaker_id`，不回嵌入** —— 所以这一路**认不了联系人**（只能显示"说话人 N"），
  这一点写在 `provides` 里（没有 `speaker.embed`），不靠调用方记得。

## 三道门（都在下面实现，都有用例）

```
GET  /api/v1/uploads?action=getPolicy&model=…        →  {upload_host, oss_access_key_id, …}
POST {upload_host}  (multipart)                      →  oss://<upload_dir>/<文件名>
POST /api/v1/services/audio/asr/transcription        →  task_id        [X-DashScope-Async: enable]
GET  /api/v1/tasks/{task_id}                         →  results[].transcription_url
GET  {transcription_url}                             →  识别结果 JSON
```

**三条必须记住的规矩**（官方文档明写、也都在用例里钉着）：

1. 提交任务时 **`parameters` 必须存在**（哪怕 `{}`）—— 省略会"提交成功但识别失败"。
2. 用 `oss://` 临时 URL 时必须带请求头 **`X-DashScope-OssResourceResolve: enable`**，
   否则服务端解析不了这个链接。
3. 临时 URL 与**模型绑定**：上传凭证要指定 `model`，且必须与随后提交任务用的模型一致；
   文件又与**主账号**绑定。所以"换个模型名重试"会失败得莫名其妙 —— 换模型就得重传。

## 为什么在这一层做"整场"

会议流水线是**按段**（默认 10 分钟一段）逐段调 `transcribe()` / `diarize()` 的，而这一路
的卖点恰恰是"整场一次聚类"。所以这一层把同一目录下的段**拼成一个整场临时文件**提交一次，
再把结果按各段的时间窗切回去（`_MeetingTask`）—— 于是：

* 一场会只上传一次、只提交一个任务（顺带省了 N−1 次上传）；
* `speaker_id` 在**整场**范围内一致 → 跨段不需要声纹缝合（这正是 §6.6 选的形状）；
* 对上层完全透明：`transcribe(第k段)` 还是回第 k 段的东西。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.capabilities.base import (
    AsrResult,
    BACKEND_ASR_PROVIDER,
    CapabilityClient,
    CapabilityError,
    DiarizeResult,
    Provenance,
    SOURCE_WAN,
)

#: 这一路的向量空间。整场一次聚类**自洽**，但与任何本地/后端嵌入**不可比** ——
#: 声明它正是为了让 L5（同一场会议不许混来源）替我们挡住"一半在线一半后端"。
VECTOR_SPACE_ID = "qwen-audio-3.1-asr-filetrans:diarize-v1"

#: 千问AI平台的服务地址。**只有一个域名，没有地域之分**（2026-09-29 对着平台文档核过：
#: 提交 `/api/v1/services/audio/asr/transcription`、查询 `/api/v1/tasks/{id}`、
#: 上传凭证 `/api/v1/uploads` 全在它下面；不带 key 打过去是 401，说明路径存在）。
#: 早先这里写的是 `dashscope.aliyuncs.com`（阿里云百炼）—— 我们实际要连的是千问AI平台，
#: 两个域名不通用，默认值错了会让"配了密钥却一直失败"变成一个谜。
DEFAULT_BASE_URL = "https://maas.qianwenaiapi.com"
DEFAULT_MODEL = "qwen-audio-3.1-asr-flash-filetrans"

UPLOAD_PATH = "/api/v1/uploads"
SUBMIT_PATH = "/api/v1/services/audio/asr/transcription"
TASK_PATH = "/api/v1/tasks/"

UPLOAD_TIMEOUT_S = 600.0        # 2 小时会议 ≈ 230 MB（16k 单声道 PCM），慢网也要传得完
POLL_TIMEOUT_S = 1800.0         # 整场转写的上限（服务端 `limits.max_audio_seconds` 是 1800）
POLL_INTERVAL_S = 5.0

#: 单文件上限（官方：临时存储 1GB；识别侧 filetrans 是 2GB，取小的那个）
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024


# ---------------------------------------------------------------- 配置

def _setting(name: str, default: str = "") -> str:
    """读一个设置项。**打桩点**：用例把 `asr_provider._setting` 换掉就能免掉真实配置。"""
    try:
        from app.config import settings
        return str(settings.get(name, default) or default).strip()
    except Exception:
        return str(default or "")


def config() -> Dict[str, str]:
    """这一路要的三样：地址 / 密钥 / 模型。

    `base_url` 留空时用 `DEFAULT_BASE_URL`（千问AI平台，**只有一个域名**）。
    **不猜密钥**：没填就是没配。
    """
    return {
        "baseUrl": (_setting("capabilityAsrProviderBaseUrl") or DEFAULT_BASE_URL).rstrip("/"),
        "apiKey": _setting("capabilityAsrProviderApiKey"),
        "model": _setting("capabilityAsrProviderModel") or DEFAULT_MODEL,
    }


def configured() -> bool:
    """配没配。**只看"有没有填密钥"** —— 地址有默认值，所以密钥是唯一的判据。"""
    return bool(config()["apiKey"])


# ---------------------------------------------------------------- HTTP 底座

def _request(method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
             data: Optional[bytes] = None, timeout: float = 60.0,
             opener=None) -> Tuple[int, bytes]:
    """发一个请求 → `(status, body_bytes)`。**网络层错误翻成 `CapabilityError`。**

    `opener` 可注入：用例靠它完全免掉网络（这一层的价值全在"请求长什么样"，
    而那用假 opener 就能钉住；真联网只该在集成测试里做一次）。
    非 2xx **不在这里抛** —— 调用方要按 `code` 决定换不换后端（`base.SERVER_CODE_TO_RETRY`）。
    """
    req = urllib.request.Request(url, data=data, method=method.upper(),
                                 headers=dict(headers or {}))
    # `urllib` 的 `add_header` 会 `capitalize()` 头名，于是 `X-DashScope-Async` 会变成
    # `X-dashscope-async`。HTTP 头本来就大小写不敏感，但**这一层唯一能自证的东西就是
    # "发出去的字节"**，而平台文档写的是那个驼峰形式 —— 所以把原样的头名放回去，
    # 免得将来有人对着抓包结果怀疑"是不是没按文档发"。
    if headers:
        req.headers = dict(headers)
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200), resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return int(getattr(e, "code", 0) or 0), body
    except urllib.error.URLError as e:
        raise CapabilityError("offline", "连不上在线转写服务：%s" % (getattr(e, "reason", e),),
                              code="offline", backend_id=BACKEND_ASR_PROVIDER) from None
    except Exception as e:                                  # pragma: no cover - 兜底
        raise CapabilityError("error", "在线转写请求失败：%s: %s" % (type(e).__name__, e),
                              backend_id=BACKEND_ASR_PROVIDER) from None


def _json_or_error(status: int, raw: bytes, what: str) -> Dict[str, Any]:
    """把响应当 JSON 读；读不出/非 2xx → 带分类的 `CapabilityError`。

    `code` 用官方那几个：`InvalidParameter` / `Throttling` / `Arrearage`（欠费）等。
    认不出来的归 `error` —— 与 `base.error_from_server` 同一条纪律：
    **宁可信息少一点，也不按猜出来的原因做"换后端/不重试"这种有副作用的决定**。
    """
    text = (raw or b"").decode("utf-8", "replace")
    try:
        body = json.loads(text) if text.strip() else {}
    except Exception:
        body = {}
    if 200 <= status < 300:
        return body if isinstance(body, dict) else {}
    code = str((body or {}).get("code") or "").strip()
    message = str((body or {}).get("message") or (body or {}).get("detail") or "").strip()
    reason = {
        "Throttling": "quota", "Throttling.RateQuota": "quota", "Throttling.AllocationQuota": "quota",
        "Arrearage": "blocked", "InvalidApiKey": "blocked", "Unauthorized": "blocked",
        "InvalidParameter": "unsupported", "ModelNotExist": "unsupported",
        "DataInspectionFailed": "unsupported",
    }.get(code, "error")
    raise CapabilityError(reason,
                          "%s 失败（HTTP %s%s）：%s" % (what, status,
                                                     (" " + code) if code else "",
                                                     message or text[:200]),
                          code=code or ("http_%s" % status), backend_id=BACKEND_ASR_PROVIDER,
                          status=status)


# ---------------------------------------------------------------- ① 上传

def get_upload_policy(cfg: Dict[str, str], *, opener=None, timeout: float = 30.0) -> Dict[str, Any]:
    """拿临时上传凭证。返回 `data` 那一块（`upload_host` / `oss_access_key_id` / …）。"""
    q = urllib.parse.urlencode({"action": "getPolicy", "model": cfg["model"]})
    url = "%s%s?%s" % (cfg["baseUrl"], UPLOAD_PATH, q)
    status, raw = _request("GET", url, headers=_auth_headers(cfg), timeout=timeout, opener=opener)
    body = _json_or_error(status, raw, "取上传凭证")
    data = body.get("data")
    if not isinstance(data, dict) or not data.get("upload_host"):
        raise CapabilityError("error", "取上传凭证：回复里没有 upload_host（%s）" % str(body)[:200],
                              backend_id=BACKEND_ASR_PROVIDER)
    return data


def _multipart(fields: Sequence[Tuple[str, str]], file_field: str, path: str) -> Tuple[bytes, str]:
    """拼一个 multipart/form-data 体（官方那七个小字段 + 文件）。

    手拼而不是引 requests：**客户端默认装的是标准库**（"最小档不含 torch"那条铁律的
    同一个理由 —— 少一个依赖少一处装不上的可能）。
    """
    boundary = "----echo%s" % uuid.uuid4().hex
    out = bytearray()
    for name, value in fields:
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (boundary, name, value)).encode("utf-8")
    filename = os.path.basename(path)
    out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
            % (boundary, file_field, filename)).encode("utf-8")
    with open(path, "rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            out += block
    out += ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    return bytes(out), "multipart/form-data; boundary=%s" % boundary


def upload_file(policy: Dict[str, Any], path: str, *, opener=None,
                timeout: float = UPLOAD_TIMEOUT_S) -> str:
    """把本地音频传到临时空间 → `oss://…`。

    **上传前先看体积**：上限取"凭证里说的那个"和我们的硬上限里**小的那个**。
    凭证里的 `max_file_size_mb` 是**按模型给的**（官方示例 100MB，filetrans 更大），
    拿我们自己的 1GB 去比会在小模型上"传上去才发现不认"。超了当场说清楚（该分段），
    而不是传完再失败。
    """
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise CapabilityError("absent", "音频文件读不到：%s" % e, backend_id=BACKEND_ASR_PROVIDER)
    limit = MAX_UPLOAD_BYTES
    try:
        from_policy = int(float(policy.get("max_file_size_mb") or 0)) * 1024 * 1024
    except (TypeError, ValueError):
        from_policy = 0
    if from_policy > 0:
        limit = min(limit, from_policy)
    if size > limit:
        raise CapabilityError("unsupported",
                              "这一路单文件上限 %.0f MB，这段有 %.0f MB —— 请把分段调小"
                              % (limit / float(1024 ** 2), size / float(1024 ** 2)),
                              code="payload_too_large", backend_id=BACKEND_ASR_PROVIDER)
    key = "%s/%s" % (str(policy.get("upload_dir") or "").rstrip("/"), os.path.basename(path))
    fields = [("OSSAccessKeyId", str(policy.get("oss_access_key_id") or "")),
              ("Signature", str(policy.get("signature") or "")),
              ("policy", str(policy.get("policy") or "")),
              ("x-oss-object-acl", str(policy.get("x_oss_object_acl") or "")),
              ("x-oss-forbid-overwrite", str(policy.get("x_oss_forbid_overwrite") or "")),
              ("key", key),
              ("success_action_status", "200")]
    body, ctype = _multipart(fields, "file", path)
    status, raw = _request("POST", str(policy.get("upload_host") or ""),
                           headers={"Content-Type": ctype}, data=body,
                           timeout=timeout, opener=opener)
    if not (200 <= status < 300):
        raise CapabilityError("error",
                              "上传音频失败（HTTP %s）：%s" % (status, (raw or b"")[:200]),
                              backend_id=BACKEND_ASR_PROVIDER, status=status)
    return "oss://" + key


# ---------------------------------------------------------------- ② 提交 / ③ 轮询

def _auth_headers(cfg: Dict[str, str]) -> Dict[str, str]:
    if not cfg.get("apiKey"):
        raise CapabilityError("absent",
                              "还没填在线转写的密钥（设置 → 能力 → 在线转写）",
                              backend_id=BACKEND_ASR_PROVIDER)
    return {"Authorization": "Bearer %s" % cfg["apiKey"], "Content-Type": "application/json"}


def submit(cfg: Dict[str, str], file_url: str, *, diarization: bool = True,
           language_hints: Sequence[str] = ("zh",), channel_id: Sequence[int] = (0,),
           speaker_count: int = 0, parameters_extra: Optional[Dict[str, Any]] = None,
           opener=None, timeout: float = 60.0) -> str:
    """提交异步任务 → `task_id`。

    **`parameters` 一定存在**（官方：新域名下省略它 → 任务能提交但识别失败）。
    `diarization=True` 打开说话人分离（整场全局聚类）；它**仅支持单声道**。
    """
    parameters: Dict[str, Any] = {"channel_id": list(channel_id or (0,))}
    if language_hints:
        parameters["language_hints"] = [str(x) for x in language_hints][:4]
    if diarization:
        parameters["diarization_enabled"] = True
        if speaker_count:
            parameters["speaker_count"] = int(speaker_count)
    if parameters_extra:
        parameters.update(parameters_extra)
    payload = {"model": cfg["model"], "input": {"file_urls": [file_url]},
               "parameters": parameters}
    headers = _auth_headers(cfg)
    headers.update({"X-DashScope-Async": "enable",
                    # `oss://` 临时 URL 必须带它，否则服务端解析不了这个链接
                    "X-DashScope-OssResourceResolve": "enable"})
    status, raw = _request("POST", cfg["baseUrl"] + SUBMIT_PATH, headers=headers,
                           data=json.dumps(payload).encode("utf-8"),
                           timeout=timeout, opener=opener)
    body = _json_or_error(status, raw, "提交转写任务")
    task_id = str((body.get("output") or {}).get("task_id") or "")
    if not task_id:
        raise CapabilityError("error", "提交转写任务：回复里没有 task_id（%s）" % str(body)[:200],
                              backend_id=BACKEND_ASR_PROVIDER)
    return task_id


def poll(cfg: Dict[str, str], task_id: str, *, interval: float = POLL_INTERVAL_S,
         timeout: float = POLL_TIMEOUT_S, opener=None, sleep=time.sleep) -> Dict[str, Any]:
    """轮询到终态 → `output`（含 `results`）。**超时就如实报**，不无限等。"""
    deadline = time.time() + float(timeout)
    url = cfg["baseUrl"] + TASK_PATH + urllib.parse.quote(str(task_id))
    while True:
        status, raw = _request("GET", url, headers=_auth_headers(cfg), timeout=30.0,
                               opener=opener)
        body = _json_or_error(status, raw, "查询转写任务")
        out = body.get("output") or {}
        state = str(out.get("task_status") or "").upper()
        if state in ("SUCCEEDED", "FAILED", "UNKNOWN"):
            return out
        if time.time() >= deadline:
            raise CapabilityError("busy",
                                  "在线转写还没出结果（已等 %.0f 秒，状态 %s）—— 稍后重试"
                                  % (float(timeout), state or "?"),
                                  code="inference_timeout", backend_id=BACKEND_ASR_PROVIDER,
                                  retryable=True)
        sleep(max(0.2, float(interval)))


def fetch_result(url: str, *, opener=None, timeout: float = 120.0) -> Dict[str, Any]:
    """下载识别结果 JSON（`transcription_url`，**24 小时有效**）。"""
    status, raw = _request("GET", url, timeout=timeout, opener=opener)
    body = _json_or_error(status, raw, "下载识别结果")
    if not body:
        raise CapabilityError("error", "识别结果是空的", backend_id=BACKEND_ASR_PROVIDER)
    return body


# ---------------------------------------------------------------- 结果解析（纯函数）

def _ms(value: Any) -> float:
    try:
        return float(value or 0.0) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def parse_result(body: Dict[str, Any], *, backend_id: str = BACKEND_ASR_PROVIDER
                 ) -> Tuple[str, List[Tuple[float, float, str]],
                            List[Tuple[float, float, str]]]:
    """识别结果 JSON → `(全文, 句子, 说话人轮次)`（**纯函数，时间轴一律毫秒转秒**）。

    JSON 形状见官方"识别结果说明"：`transcripts[].sentences[]`，每个句子有
    `begin_time`/`end_time`（**毫秒**）、`text`、`speaker_id`（开了分离才有）、`words[]`。

    两个刻意的取舍：
      * **多音轨时按 `channel_id` 分段拼全文**（`channel_id=[0,1]` 会回两段 transcript）；
      * 句子带 `speaker_id` 才产出轮次，**没有就如实给空**（不编一个"说话人1"）。
    """
    texts: List[str] = []
    sentences: List[Tuple[float, float, str]] = []
    turns: List[Tuple[float, float, str]] = []
    for tr in (body.get("transcripts") or []):
        if not isinstance(tr, dict):
            continue
        texts.append(str(tr.get("text") or "").strip())
        for s in (tr.get("sentences") or []):
            if not isinstance(s, dict):
                continue
            start, end = _ms(s.get("begin_time")), _ms(s.get("end_time"))
            text = str(s.get("text") or "").strip()
            if text:
                sentences.append((start, end, text))
            spk = s.get("speaker_id")
            if spk is not None:
                turns.append((start, end, "S%s" % spk))
    return "".join(t for t in texts if t), sentences, turns


# ---------------------------------------------------------------- 整场合并

def _segment_offsets(paths: Sequence[str]) -> List[Tuple[str, float, float]]:
    """每段在整场里的 `(路径, 起点秒, 终点秒)`。读 wav 头拿帧数 —— **不重采样**。"""
    import wave as _wave
    out, cursor = [], 0.0
    for p in paths:
        try:
            with _wave.open(p, "rb") as f:
                frames, rate = int(f.getnframes()), int(f.getframerate() or 0)
        except Exception as e:
            raise CapabilityError("unsupported", "这一段读不出 wav 头（%s）：%s" % (p, e),
                                  code="unsupported_media", backend_id=BACKEND_ASR_PROVIDER)
        if rate <= 0:
            raise CapabilityError("unsupported", "这一段的采样率读不出来（%s）" % p,
                                  code="unsupported_media", backend_id=BACKEND_ASR_PROVIDER)
        dur = frames / float(rate)
        out.append((p, cursor, cursor + dur))
        cursor += dur
    return out


def concat_wavs(paths: Sequence[str], dst: str) -> None:
    """把若干段 wav 顺序拼成一个文件（**逐块拷贝 PCM，不重采样、不整体进内存**）。

    为什么值得这么麻烦：整场一次提交是这个后端的主要价值（全局说话人分离）。
    2 小时会议 ≈ 230 MB，用 numpy 全读进内存要 460 MB，还把"能不能跑"押在内存上。
    """
    import wave as _wave
    if not paths:
        raise CapabilityError("absent", "没有要拼的音频", backend_id=BACKEND_ASR_PROVIDER)
    with _wave.open(paths[0], "rb") as first:
        params = first.getparams()
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with _wave.open(dst, "wb") as out:
        out.setparams(params)
        for p in paths:
            with _wave.open(p, "rb") as f:
                if (f.getnchannels(), f.getsampwidth(), f.getframerate()) != \
                        (params.nchannels, params.sampwidth, params.framerate):
                    raise CapabilityError(
                        "unsupported",
                        "分段的格式不一致（%s 与第一段不同）—— 拼起来会是一条坏音频"
                        % os.path.basename(p),
                        code="unsupported_media", backend_id=BACKEND_ASR_PROVIDER)
                while True:
                    block = f.readframes(32000)      # 1 秒 @16k
                    if not block:
                        break
                    out.writeframes(block)


class _MeetingTask:
    """一场会的**一次**整场提交，按段时间窗切给各段用。

    `segments` 是同一目录下的段（排序后）；`results` 是整场解析出来的
    `(全文, 句子, 轮次)`。`slice_for(path)` 回这一段自己的部分（时间轴减掉段起点）。
    """

    def __init__(self, offsets: List[Tuple[str, float, float]], sentences, turns, text):
        self.offsets = offsets
        self.sentences = sentences
        self.turns = turns
        self.text = text

    def _window(self, path: str) -> Tuple[float, float]:
        for p, start, end in self.offsets:
            if os.path.abspath(p) == os.path.abspath(path):
                return start, end
        return 0.0, 0.0

    @staticmethod
    def _clip(rows, start: float, end: float):
        out = []
        for a, b, t in rows:
            if b <= start + 0.01 or a >= end - 0.01:
                continue
            out.append((max(a, start) - start, min(b, end) - start, t))
        return out

    def slice_for(self, path: str) -> Tuple[str, list, list]:
        start, end = self._window(path)
        sentences = self._clip(self.sentences, start, end)
        turns = self._clip(self.turns, start, end)
        text = "".join(t for _a, _b, t in sentences)
        return text, sentences, turns


# ---------------------------------------------------------------- 后端

class DashScopeAsrClient(CapabilityClient):
    """`asr-provider`：整场异步 + 临时上传。**不产出嵌入**（所以不提供 `speaker.embed`）。"""

    backend_id = BACKEND_ASR_PROVIDER
    source = SOURCE_WAN
    #: 它到底能干什么 —— 少一个 `speaker.embed` 是**事实**，不是遗漏
    provides = frozenset({"asr.text", "asr.timestamps", "diarize.turns"})
    vector_space_id = VECTOR_SPACE_ID

    #: 整场任务缓存条数（一场会议一条；多留一条是给"改了分段又重跑"的场合）
    MAX_CACHED_MEETINGS = 2

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 *, opener=None, tmp_dir: str = "", speak: bool = False):
        cfg = config()
        self.base_url = (base_url or cfg["baseUrl"]).rstrip("/")
        self.api_key = api_key if api_key is not None else cfg["apiKey"]
        self.model = model or cfg["model"]
        self.timeout_infer = POLL_TIMEOUT_S
        self._opener = opener
        self._tmp_dir = tmp_dir
        self._cache: List[Tuple[str, _MeetingTask]] = []
        self._last_task_id = ""
        #: "这一路认不了人"这句话要能被面板与日志读到（不是只写在文档里）
        self.no_embeddings_reason = ("在线转写只回说话人编号，不返回声纹嵌入 —— "
                                     "这一档认不了联系人")

    # ---- 就绪 ----------------------------------------------------------------

    def ready(self) -> Optional[bool]:
        """配了密钥就算就绪（**不联网探**：它是按次计费的公网服务，
        面板刷一下就去打一次没有意义；真连不上时 `_request` 会报 `offline`）。"""
        return bool(self.api_key and self.base_url)

    def refresh(self, force: bool = False) -> bool:
        return self.ready()

    def describe(self) -> Dict[str, Any]:
        out = super().describe()
        out.update({"baseUrl": self.base_url, "model": self.model,
                    "hasKey": bool(self.api_key), "lastTaskId": self._last_task_id,
                    "note": self.no_embeddings_reason})
        return out

    # ---- 整场任务 ------------------------------------------------------------

    def _cfg(self) -> Dict[str, str]:
        return {"baseUrl": self.base_url, "apiKey": self.api_key, "model": self.model}

    def _meeting_segments(self, wav: str) -> List[str]:
        """同目录下、名字是数字开头的音频段（`01.wav` / `01.flac`…），**排序后**。

        判据与会议目录里那套一致（`audiofile.segment_files` 的命名约定）；只有一段时
        就是它自己 —— 于是"整场"与"单段"走的是同一条代码路径。
        """
        folder = os.path.dirname(os.path.abspath(wav))
        try:
            names = [n for n in os.listdir(folder)
                     if os.path.splitext(n)[1].lower() in (".wav", ".flac")]
        except OSError:
            return [wav]
        segs = sorted(n for n in names if n.split(".")[0].isdigit())
        paths = [os.path.join(folder, n) for n in segs] or [wav]
        return paths

    def _task_for(self, wav: str) -> _MeetingTask:
        """拿（或建）这一段的整场任务。**同一目录只提交一次。**"""
        paths = self._meeting_segments(wav)
        key = "|".join("%s:%s" % (p, os.path.getmtime(p)) for p in paths
                       if os.path.exists(p))
        for cached_key, task in self._cache:
            if cached_key == key:
                return task
        task = self._run_meeting(paths)
        self._cache.insert(0, (key, task))
        del self._cache[self.MAX_CACHED_MEETINGS:]
        return task

    def _run_meeting(self, paths: Sequence[str]) -> _MeetingTask:
        """整场：拼文件 → 上传 → 提交 → 轮询 → 下载 → 解析。"""
        import tempfile
        cfg = self._cfg()
        tmp_dir = self._tmp_dir or tempfile.mkdtemp(prefix="echo-asr-provider-")
        work = list(paths)
        if any(str(p).lower().endswith(".flac") for p in work):
            # 压缩存的段（FLAC）先解成 wav —— 与会议链路同一个解码器，
            # 免得"同一批音频两条路各解一次"。
            from app.audio import audiofile
            work = [audiofile.decode_to_wav(p) if audiofile.is_flac(p) else p for p in work]
        # 时间窗按**实际要拼的那些段**算（顺序拼 → 累加时长）
        offsets = _segment_offsets(work)
        # 只有一段时**不白拷一遍**：直接上传那一段
        whole = work[0] if len(work) == 1 else os.path.join(tmp_dir, "meeting.wav")
        try:
            if len(work) > 1:
                concat_wavs(work, whole)
            policy = get_upload_policy(cfg, opener=self._opener)
            file_url = upload_file(policy, whole, opener=self._opener)
            task_id = submit(cfg, file_url, opener=self._opener)
            self._last_task_id = task_id
            output = poll(cfg, task_id, interval=POLL_INTERVAL_S, timeout=self.timeout_infer,
                          opener=self._opener)
            body = fetch_result(_result_url(output), opener=self._opener)
            text, sentences, turns = parse_result(body, backend_id=self.backend_id)
            return _MeetingTask(offsets, sentences, turns, text)
        finally:
            if len(work) > 1 and os.path.exists(whole):
                try:
                    os.unlink(whole)
                except OSError:
                    pass

    # ---- 槽 ----------------------------------------------------------------

    def transcribe(self, wav: str, *, lang: str = "auto", want_timestamps: bool = False,
                   variant: str = "long", **kw) -> AsrResult:
        task = self._task_for(wav)
        text, sentences, _turns = task.slice_for(wav)
        return AsrResult(text=text,
                         sentences=tuple(sentences),
                         timestamps="exact" if sentences else "none",
                         provenance=Provenance(self.backend_id, self.model),
                         audio_seconds=0.0)

    def diarize(self, wav: str, *, max_speakers=None, **kw) -> DiarizeResult:
        """说话人时间轴。**`speakers` 一定是空的** —— 这一路没有嵌入。

        空 `speakers` 不是"失败"：标签来自整场那一次全局聚类，在一场里自洽；
        `meeting.py` 看到"有轮次、没嵌入"会直接用它的标签（不走 `SpeakerRegistry`），
        而声纹比对自然不做。
        """
        task = self._task_for(wav)
        _text, _sentences, turns = task.slice_for(wav)
        return DiarizeResult(turns=tuple(turns), speakers={}, dim=0,
                             vector_space_id=self.vector_space_id,
                             provenance=Provenance(self.backend_id, self.model))

    def close(self) -> None:
        self._cache = []
        return None


def _result_url(output: Dict[str, Any]) -> str:
    """从 `output` 里取 `transcription_url`（子任务失败时**如实报**，不留白）。"""
    results = output.get("results") or []
    first = results[0] if results else {}
    url = str((first or {}).get("transcription_url") or "")
    if url:
        return url
    code = str((first or {}).get("code") or "FAILED")
    message = str((first or {}).get("message") or "")
    raise CapabilityError(
        "error",
        "在线转写没给出结果（%s%s）" % (code, ("：" + message) if message else ""),
        code=code, backend_id=BACKEND_ASR_PROVIDER)


def client_from_settings() -> Optional[DashScopeAsrClient]:
    """按设置造一个；**没填密钥就返回 None**（不造一个必然失败的）。

    与 `echo_server.client_from_settings()` 同一个约定 —— 路由那边对两者一视同仁。
    """
    if not configured():
        return None
    return DashScopeAsrClient()
