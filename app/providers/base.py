# -*- coding: utf-8 -*-
"""provider 的接口形状（P5）

ASR / TTS 两类能力各一个抽象，方法都保持**最小**：只要现有实现能塞进来、调用方能拿到明确结果。
不做"大一统管道"——这一层只负责"语音进、语音出"，推理不在 ECHO 内完成（见 `providers/__init__.py`）。

约定（三条，全部有测试钉住）
--------------------------
1. **就绪探测不抛异常**：``ready()`` 返回 True / False / None（None = 判不了），
   绝不因为"没装依赖"把面板/接口带崩；
2. **失败要给原因**：业务性失败返回结构化结果或抛带说明的异常，不许静默吞掉
   （1.x 的 `stt.py` 把"空结果"和"引擎挂了"混成一个空串，教训见 PROGRESS §19 发现③）；
3. **出网必须能声明**：需要联网的 provider 在 spec 里写 ``egress=True`` + ``egress_note``，
   由注册表强制（不写会注册失败）。
"""
from __future__ import annotations

import abc


class Provider(abc.ABC):
    """所有 provider 的公共面。"""

    #: 由注册表填充（`id`/`kind`/`name`/`source`/`egress`…）
    spec = {}

    @abc.abstractmethod
    def ready(self):
        """True / False / None（判不了）。**不抛异常**。"""
        raise NotImplementedError


class AsrProvider(Provider):
    """语音转写：输入 wav 路径，输出文字（+ 可选分句）。"""

    @abc.abstractmethod
    def transcribe(self, wav_path, lang="zh", **kw):
        """返回 ``{"text": str, "engine": str, "model": str, "sentences": [...]}``。

        `text` 为空**不等于**失败：要带 ``reason``（例如 ``"no-speech"``）让调用方区分
        "这段没人说话"和"引擎挂了"。
        """
        raise NotImplementedError


class TtsProvider(Provider):
    """语音合成：把文字念出来（阻塞到念完或超时）。"""

    @abc.abstractmethod
    def speak(self, text, timeout=60, **kw):
        """成功返回 True；失败返回 False（提示音/朗读失败不该炸主流程）。"""
        raise NotImplementedError
