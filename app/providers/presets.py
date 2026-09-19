# -*- coding: utf-8 -*-
"""在线服务预设（P5）——面板"一键填入"用的公开信息

规则（很重要）
------------
* 这里**只放公开信息**：厂商的公开地址与常见模型名；
* **不放任何密钥**（密钥由用户自己填，存本机库，接口永不回显）；
* **不放单位内网地址**：那属内部信息，仓库是公开的（REFACTOR-PLAN §13.4 已定）。
  "内网网关"这个预设只给名字与说明，`base_url` 留空让用户按部署文档填。

用途：面板的「在线服务」下拉/一键填入；`GET /api/providers/presets` 返回本清单。
"""
from __future__ import annotations

#: 每条：id / kind / name / base_url（空=让用户填）/ model（可空）/ note（出网说明）
PRESETS = [
    dict(id="deepseek", kind="llm", name="DeepSeek 官方",
         base_url="https://api.deepseek.com/v1", model="deepseek-chat",
         note="公网直连；发给它的是提示词与转写文本，请自行判断内容是否可外发"),
    dict(id="openai", kind="llm", name="OpenAI 官方",
         base_url="https://api.openai.com/v1", model="gpt-4o-mini",
         note="公网直连；同上，内容会离开本机"),
    dict(id="intranet-gateway", kind="llm", name="单位内网网关（地址自填）",
         base_url="", model="",
         note="地址与鉴权方式属单位内部信息，不写进本仓库；按部署文档填写 base_url 即可"),
    dict(id="openai-asr", kind="asr", name="OpenAI 兼容在线转写",
         base_url="https://api.openai.com/v1", model="whisper-1",
         note="**整段会议音频会上传**到该服务；不想出网就用本地转写引擎"),
]


def catalog():
    """给面板的清单（纯公开信息，没有密钥字段）。"""
    return {"presets": [dict(p) for p in PRESETS]}


def find(preset_id):
    for p in PRESETS:
        if p["id"] == preset_id:
            return dict(p)
    return None
