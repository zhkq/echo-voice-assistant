# -*- coding: utf-8 -*-
"""JSON 兜底：**非有限浮点不许把请求打成 500**（2026-10-05 真机事故后补）。

现场：模型（pyannote 分离）在极短/静音/合成音输入下吐出 `NaN` 的 start/end，
`round(nan, 3)` 还是 nan，而 Starlette 的 `JSONResponse` 调
`json.dumps(..., allow_nan=False)` → `ValueError: Out of range float values are not
JSON compliant` → 整个接口 **HTTP 500**。用户看到的是"转写一直不完成"（分离那一步卡住）。

规矩：**模型的脏数据要在这里被驯服，而不是变成 500**。`null` 是 JSON 里表达"这个数没有"
的合法方式；`NaN` 不是（很多解析器直接拒收）。
"""
import json
import math
from typing import Any

from fastapi.responses import JSONResponse


def sane(obj: Any) -> Any:
    """递归地把非有限浮点换成 `None`（其余原样）。

    `bool` 是 `int` 的子类，**不要**被 `float()` 化；字符串/整数照原样返回。
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, dict):
        return {k: sane(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [sane(v) for v in obj]
    return obj


class SafeJSONResponse(JSONResponse):
    """`JSONResponse` 的兜底版：渲染前先过一遍 `sane()`。"""

    def render(self, content: Any) -> bytes:
        return super().render(sane(content))
