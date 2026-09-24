# -*- coding: utf-8 -*-
"""能力层：**按槽**从多个后端里取能力，而不是"从 N 选 1"。

一句话区分它与 `app/providers/`：

    providers/     从 N 个实现里**选一个**（这一整类能力都由它干）
    capabilities/  一场会议可能同时用到**三四个**后端，每个槽各选各的

所以别把槽塞进 `providers/`（3.0 总览 §9 的裁定 #1）。

怎么用：

    from app.capabilities import build_default_router, Need

    router = build_default_router()
    need = Need(slots=("asr.text", "asr.timestamps", "diarize.turns"),
                purpose="meeting", privacy="lan")
    result, plan = router.call("asr.text", need, wav="/path/seg.wav", lang="zh")
    print(plan.as_dict())        # 每个槽用了谁、跳过了谁、为什么
"""
from app.capabilities.base import (          # noqa: F401
    BACKEND_ECHO_SERVER,
    BACKEND_INTRANET,
    BACKEND_LOCAL,
    LOCAL_ONLY_SLOTS,
    PRIVACY_ORDER,
    SERVER_CODE_RETRY,
    SERVER_CODE_TO_REASON,
    SKIP_REASONS,
    SLOTS,
    SOURCE_LAN,
    SOURCE_LOCAL,
    SOURCE_WAN,
    VECTOR_SLOTS,
    AsrResult,
    CapabilityClient,
    CapabilityError,
    DiarizeResult,
    EmbedResult,
    Provenance,
    error_from_server,
    slots_str,
)
from app.capabilities.router import (        # noqa: F401
    DEFAULT_ORDER,
    CapabilityRouter,
    Need,
    Pick,
    Plan,
    Skipped,
    build_default_router,
)

__all__ = [
    "BACKEND_ECHO_SERVER", "BACKEND_INTRANET", "BACKEND_LOCAL",
    "LOCAL_ONLY_SLOTS", "PRIVACY_ORDER", "SKIP_REASONS", "SLOTS",
    "SOURCE_LAN", "SOURCE_LOCAL", "SOURCE_WAN", "VECTOR_SLOTS",
    "SERVER_CODE_RETRY", "SERVER_CODE_TO_REASON",
    "AsrResult", "CapabilityClient", "CapabilityError", "DiarizeResult",
    "EmbedResult", "Provenance", "error_from_server", "slots_str",
    "DEFAULT_ORDER", "CapabilityRouter", "Need", "Pick", "Plan", "Skipped",
    "build_default_router",
]
