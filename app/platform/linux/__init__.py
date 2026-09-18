# -*- coding: utf-8 -*-
"""linux 平台的公开面。实现与说明见 ``env.py``。"""
from app.platform.linux.env import NAME, PLATFORM_DEFAULTS, dangerous_prefixes  # noqa: F401

__all__ = ["NAME", "PLATFORM_DEFAULTS", "dangerous_prefixes"]
