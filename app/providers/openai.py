# -*- coding: utf-8 -*-
"""在线服务 provider（P5）：任意 OpenAI 兼容端点，作为 ASR / LLM 的另一种实现

与 `router.py` 的分工
--------------------
* `echo-auto`（router.py）= **多上游派发**：一个入口背后是多个上游组成的模型组，
  带健康探测与熔断（原 `dsh-failover`，D25 归主包）；
* 本文件 = **单上游直连**：用户直接填一个 OpenAI 兼容地址 + 密钥，不走路由。
  两者都是 LLM provider 的实现，按 `providerLlm` 选。

规划要求的验收点是"**配一个在线 ASR/LLM 即可完成一次转写 + 一次纪要**"——
本文件提供那个"在线"实现，`providerLlm`/`providerAsr` 指向它们即生效。

出网标注（硬要求）
----------------
两个 provider 都 `egress=True`，并在 spec 里写清"发什么、给谁"：
LLM 发提示词与文本；ASR **整段音频**会上传。这是用户最需要知道的一条。

凭据
----
密钥从配置读（`providerLlmApiKey` / `providerAsrApiKey`），存在本机库里，
**永远不会出现在 `Settings.all()` / `/api/providers` / 日志里**（见 config.py 的 secret 遮罩）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from app.providers.base import AsrProvider, LlmProvider

DEFAULT_ASR_MODEL = "whisper-1"


def _setting(key, default=""):
    try:
        from app.config import settings
        return settings.get(key, default)
    except Exception:
        return default


def _post_json(url, payload, headers=None, timeout=60, who="openai"):
    """POST 一个 JSON 并解析 JSON 回复。失败抛带 provider 名的异常（不返回空串）。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise RuntimeError("%s: HTTP %s %s" % (who, e.code, body)) from None
    except Exception as e:
        raise RuntimeError("%s: 连不上在线服务（%s）——检查地址与网络" % (who, e)) from None
    try:
        return json.loads(raw)
    except Exception as e:
        raise RuntimeError("%s: 回复不是 JSON（%s）：%s" % (who, e, raw[:200])) from None


class OpenAICompatLlmProvider(LlmProvider):
    """单上游直连的 OpenAI 兼容 LLM（DeepSeek 官方 / 内网网关 / 任意兼容服务）。"""

    id = "openai-llm"

    def base_url(self):
        return str(_setting("providerLlmBaseUrl", "") or "").strip().rstrip("/")

    def api_key(self):
        return str(_setting("providerLlmApiKey", "") or "").strip()

    def model(self):
        return str(_setting("providerLlmModel", "") or "").strip()

    def ready(self):
        """配了地址就算就绪（没地址 = 没配 → False；不看密钥，有些自建网关不要密钥）。"""
        try:
            return bool(self.base_url())
        except Exception:
            return None

    def chat(self, messages, timeout=60, base_url=None, api_key=None, model=None, **kw):
        base = (base_url or self.base_url()).rstrip("/")
        if not base:
            raise RuntimeError("openai-llm: 还没配「在线 LLM 地址」（设置 → 能力 provider）")
        if not messages:
            raise ValueError("openai-llm: messages 为空")
        headers = {}
        token = self.api_key() if api_key is None else api_key
        if token:
            headers["Authorization"] = "Bearer %s" % token
        payload = {"model": model or self.model() or "gpt-4o-mini",
                   "messages": list(messages), "stream": False}
        obj = _post_json(base + "/chat/completions", payload, headers, timeout, "openai-llm")
        try:
            content = obj["choices"][0]["message"]["content"]
        except Exception as e:
            raise RuntimeError("openai-llm: 回复格式不认识（%s）：%s"
                               % (e, json.dumps(obj, ensure_ascii=False)[:200])) from None
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("openai-llm: 上游返回了空回复")
        return content


class OpenAICompatAsrProvider(AsrProvider):
    """单上游直连的 OpenAI 兼容转写（`/audio/transcriptions`）。**音频整段出网**。"""

    id = "openai-asr"

    def base_url(self):
        return str(_setting("providerAsrBaseUrl", "") or "").strip().rstrip("/")

    def api_key(self):
        return str(_setting("providerAsrApiKey", "") or "").strip()

    def model(self):
        return str(_setting("providerAsrModel", "") or "").strip() or DEFAULT_ASR_MODEL

    def ready(self):
        try:
            return bool(self.base_url())
        except Exception:
            return None

    # ---- multipart（stdlib 手工拼，不引第三方依赖）----

    @staticmethod
    def _multipart(fields, file_field, file_path):
        import uuid
        boundary = "----echo%s" % uuid.uuid4().hex
        chunks = []
        for name, value in fields.items():
            chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                           % (boundary, name, value)).encode("utf-8"))
        with open(file_path, "rb") as fh:
            blob = fh.read()
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                       "Content-Type: audio/wav\r\n\r\n"
                       % (boundary, file_field, os.path.basename(file_path))).encode("utf-8"))
        chunks.append(blob)
        chunks.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
        return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary

    def transcribe(self, wav_path, lang="zh", timeout=180, base_url=None,
                   api_key=None, model=None, **kw):
        base = (base_url or self.base_url()).rstrip("/")
        if not base:
            raise RuntimeError("openai-asr: 还没配「在线转写地址」（设置 → 能力 provider）")
        if not os.path.isfile(wav_path):
            raise RuntimeError("openai-asr: 音频文件不存在：%s" % wav_path)
        fields = {"model": model or self.model()}
        if lang and str(lang).lower() not in ("auto", ""):
            fields["language"] = str(lang).lower()
        body, ctype = self._multipart(fields, "file", wav_path)
        headers = {"Content-Type": ctype}
        token = self.api_key() if api_key is None else api_key
        if token:
            headers["Authorization"] = "Bearer %s" % token
        req = urllib.request.Request(base + "/audio/transcriptions", data=body,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise RuntimeError("openai-asr: HTTP %s %s" % (e.code, detail)) from None
        except Exception as e:
            raise RuntimeError("openai-asr: 连不上在线转写（%s）" % e) from None
        try:
            obj = json.loads(raw)
            text = obj.get("text", "")
        except Exception as e:
            raise RuntimeError("openai-asr: 回复不是 JSON（%s）：%s" % (e, raw[:200])) from None
        out = {"text": text or "", "engine": "openai-asr", "model": fields["model"],
               "sentences": []}
        if not out["text"]:
            out["reason"] = "empty-or-unknown"
        return out


def register_builtin():
    """登记两个在线 provider（出网说明写在 spec 里，注册表会强制要求）。"""
    from app import providers as P

    P.register(P.ProviderSpec(
        id=OpenAICompatLlmProvider.id, kind="llm", name="在线 LLM（OpenAI 兼容）",
        source="online", egress=True,
        egress_note="发给它的是提示词与转写/纪要文本；地址与密钥由你自己填（可为内网网关）",
        purpose="单上游直连；不想用多上游派发（ECHO AUTO）时选它。配好即可出纪要，无需 agent",
        details={"settings": ["providerLlmBaseUrl", "providerLlmApiKey", "providerLlmModel"]}),
        OpenAICompatLlmProvider)
    P.register(P.ProviderSpec(
        id=OpenAICompatAsrProvider.id, kind="asr", name="在线转写（OpenAI 兼容）",
        source="online", egress=True,
        egress_note="**整段会议音频会上传**到该服务；不想出网就用本地转写引擎",
        purpose="没有 GPU 或想省本地算力时用；把 providerAsr 指向它即生效",
        details={"settings": ["providerAsrBaseUrl", "providerAsrApiKey", "providerAsrModel"],
                 "model": DEFAULT_ASR_MODEL}),
        OpenAICompatAsrProvider)
