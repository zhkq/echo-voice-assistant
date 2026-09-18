# -*- coding: utf-8 -*-
"""win32 平台的公开面。实现与说明见 ``env.py``。"""
from app.platform.win32.env import NAME, PLATFORM_DEFAULTS, dangerous_prefixes  # noqa: F401

__all__ = ["NAME", "PLATFORM_DEFAULTS", "dangerous_prefixes"]
