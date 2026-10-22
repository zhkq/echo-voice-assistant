# -*- coding: utf-8 -*-
"""在线转写 provider（P5）：任意 OpenAI 兼容端点，作为 ASR 的另一种实现

## 为什么这里没有 LLM

2026-10-22 用户定调：**ECHO 不直接连 LLM 完成动作**，纪要/总结/推理一律发给
**配置的智能体**。`echo-auto` 是**给智能体用的内外网模型路由**
（`app/llm_router.py` → DSH 的 `llm-pi-ai.providers.echo-auto`，由 `dsh-failover/` 提供），
不是 ECHO 直接调用的 LLM provider —— 所以本文件只留在线 ASR。

## 出网标注（硬要求）

ASR 的 `egress=True`：**整段音频会上传**。这是用户最需要知道的一条。

## 凭据

密钥从配置读（`providerAsrApiKey`），存在本机库里，
**永远不会出现在 `Settings.all()` / `/api/providers` / 日志里**（见 config.py 的 secret 遮罩）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from app.providers.base import AsrProvider

DEFAULT_ASR_MODEL = "whisper-1"


def _setting(key, default=""):
    try:
        from app.config import settings
        return settings.get(key, default)
    except Exception:
        return default


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
    """登记在线转写 provider（出网说明写在 spec 里，注册表会强制要求）。"""
    from app import providers as P

    P.register(P.ProviderSpec(
        id=OpenAICompatAsrProvider.id, kind="asr", name="在线转写（OpenAI 兼容）",
        source="online", egress=True,
        egress_note="**整段会议音频会上传**到该服务；不想出网就用本地转写引擎",
        purpose="没有 GPU 或想省本地算力时用；把 providerAsr 指向它即生效",
        details={"settings": ["providerAsrBaseUrl", "providerAsrApiKey", "providerAsrModel"],
                 "model": DEFAULT_ASR_MODEL}),
        OpenAICompatAsrProvider)
