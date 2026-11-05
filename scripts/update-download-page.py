#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步下载页与下载清单：`site/assets/downloads.json` 是唯一事实源。

用法（仓库根目录）：

    python scripts/update-download-page.py --check   # 默认：只校验，不一致时非零退出
    python scripts/update-download-page.py --write   # 把 JSON 的值写回页面与 README

`site/download.html` 仍然是**静态 HTML**：本脚本只在发版时改文本，不引入运行时
JS 注入，也不改页面结构。它同步三处：

  * `site/download.html` 里带 `data-download="..."` / `data-download-url="..."` 的文本；
  * `site/downloads/README.md` 的表格（`downloads-table` 标记之间）；
  * 页面上的版本、安装包构建日期、文件名、大小、SHA-256。

测试 `tests/test_vibepay_site.py` 会以 `--check` 跑它，所以页面与 JSON 漂移会直接红。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JSON_PATH = ROOT / "site" / "assets" / "downloads.json"
HTML_PATH = ROOT / "site" / "download.html"
README_PATH = ROOT / "site" / "downloads" / "README.md"

PLATFORMS = ("Windows", "macOS", "Android")
PLATFORM_KEYS = {"Windows": "windows", "macOS": "macos", "Android": "android"}
TABLE_START = "<!-- downloads-table:start -->"
TABLE_END = "<!-- downloads-table:end -->"


def fail(message: str) -> int:
    print("FAIL: " + message, file=sys.stderr)
    return 1


def load_data() -> dict:
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    for key in ("product", "version", "date", "files"):
        if not data.get(key):
            raise ValueError("downloads.json 缺少字段：%s" % key)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(data["date"])):
        raise ValueError("downloads.json 的 date 必须是 YYYY-MM-DD：%r" % data["date"])
    files = data["files"]
    if not isinstance(files, list) or len(files) != 3:
        raise ValueError("downloads.json 的 files 必须是 3 个平台")
    seen = []
    for item in files:
        for key in ("platform", "name", "size", "sha256"):
            if key not in item:
                raise ValueError("downloads.json 的某个文件缺少字段：%s" % key)
        if item["platform"] not in PLATFORMS:
            raise ValueError("未知平台：%r" % item["platform"])
        if not isinstance(item["size"], int) or item["size"] <= 0:
            raise ValueError("size 必须是正整数：%r" % item["size"])
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(item["sha256"])):
            raise ValueError("sha256 必须是 64 位十六进制：%s" % item["name"])
        if not re.search(r"\.(zip|apk)$", str(item["name"])):
            raise ValueError("文件名应以 .zip 或 .apk 结尾：%s" % item["name"])
        seen.append(item["platform"])
    if sorted(seen) != sorted(PLATFORMS):
        raise ValueError("files 必须恰好包含 Windows / macOS / Android 各一个")
    android = next(item for item in files if item["platform"] == "Android")
    for key in ("versionName", "versionCode"):
        if key not in android:
            raise ValueError("Android 文件缺少 %s" % key)
    return data


def format_mb(size: int) -> str:
    return "%.1f MB" % (size / 1_000_000.0)


def format_bytes(size: int) -> str:
    return "{:,} 字节（{:.1f} MiB）".format(size, size / 1_048_576.0)


def build_values(data: dict) -> dict:
    values = {
        "version-label": "内测版 " + str(data["version"]),
        "date-label": str(data["date"]),
    }
    for item in data["files"]:
        key = PLATFORM_KEYS[item["platform"]]
        name = str(item["name"])
        values[key + "-version"] = str(data["version"])
        values[key + "-size"] = format_mb(int(item["size"]))
        values[key + "-date"] = str(data["date"])
        values[key + "-name"] = name
        values[key + "-bytes"] = format_bytes(int(item["size"]))
        values[key + "-sha"] = str(item["sha256"])
        values[key + "-hash-command"] = (
            "shasum -a 256 %s" % name
            if item["platform"] == "macOS"
            else "Get-FileHash .\\%s -Algorithm SHA256" % name
        )
        values[key + "-url"] = "/downloads/" + name
    android = next(item for item in data["files"] if item["platform"] == "Android")
    values["android-build"] = "debug %s（versionCode %s）" % (
        android["versionName"], android["versionCode"])
    values["android-build-label"] = "debug %s" % android["versionName"]
    values["android-shasum-command"] = "shasum -a 256 %s" % android["name"]
    return values


def replace_text_fields(html: str, values: dict) -> tuple[str, list]:
    pattern = re.compile(
        r'<(?P<tag>[a-zA-Z0-9]+)\b(?P<attrs>[^>]*\bdata-download="(?P<key>[^"]+)"[^>]*)>'
        r'(?P<body>.*?)</(?P=tag)>',
        re.S,
    )
    changed = []

    def repl(match):
        key = match.group("key")
        if key not in values:
            return match.group(0)
        old = match.group("body")
        new = values[key]
        if old != new:
            changed.append((key, old, new))
        return match.group(0).replace(">" + old + "</", ">" + new + "</", 1)

    return pattern.sub(repl, html), changed


def replace_urls(html: str, values: dict) -> tuple[str, list]:
    pattern = re.compile(r'<a\b[^>]*\bdata-download-url="(?P<key>[^"]+)"[^>]*>')
    changed = []

    def repl(match):
        key = match.group("key")
        if key + "-url" not in values:
            return match.group(0)
        tag = match.group(0)
        new = 'href="%s"' % values[key + "-url"]
        updated = re.sub(r'href="[^"]*"', new, tag)
        if updated != tag:
            changed.append((key + "-url", tag, updated))
        return updated

    return pattern.sub(repl, html), changed


def ensure_fields(html: str, values: dict) -> None:
    missing = []
    for key in values:
        if key.endswith("-url"):
            attr = 'data-download-url="%s"' % key[:-4]
        else:
            attr = 'data-download="%s"' % key
        if attr not in html:
            missing.append(attr)
    if missing:
        raise ValueError("download.html 缺少字段：" + "、".join(missing))


def build_table(data: dict) -> str:
    lines = [
        TABLE_START,
        "| 平台 | 文件 | 大小（字节） | SHA-256 |",
        "|---|---|---|---|",
    ]
    for item in data["files"]:
        lines.append("| %s | `%s` | %s | `%s` |" % (
            item["platform"], item["name"], format(int(item["size"]), ","), item["sha256"]))
    lines.append(TABLE_END)
    return "\n".join(lines)


def replace_table(readme: str, data: dict) -> tuple[str, list]:
    pattern = re.compile(re.escape(TABLE_START) + r".*?" + re.escape(TABLE_END), re.S)
    new_table = build_table(data)
    if not pattern.search(readme):
        raise ValueError("README 缺少 downloads-table 标记")
    old_table = pattern.search(readme).group(0)
    if old_table == new_table:
        return readme, []
    return pattern.sub(lambda _m: new_table, readme, count=1), [("downloads-table", old_table, new_table)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="用 site/assets/downloads.json 校验或更新下载页与 README。")
    parser.add_argument("--write", action="store_true", help="把 JSON 的值写回页面与 README")
    parser.add_argument("--check", action="store_true", help="只校验（默认行为）")
    args = parser.parse_args(argv)
    if args.write and args.check:
        return fail("--write 与 --check 不能同时使用")

    try:
        data = load_data()
        values = build_values(data)
    except Exception as exc:  # noqa: BLE001 - 命令行工具，如实报错即可
        return fail(str(exc))

    html = HTML_PATH.read_text(encoding="utf-8")
    readme = README_PATH.read_text(encoding="utf-8")
    try:
        ensure_fields(html, values)
    except ValueError as exc:
        return fail(str(exc))
    html, text_changes = replace_text_fields(html, values)
    html, url_changes = replace_urls(html, values)
    readme, table_changes = replace_table(readme, data)
    changes = text_changes + url_changes + table_changes

    for key, old, new in changes:
        print("- %s: %r -> %r" % (key, old, new))
    if not changes:
        print("OK: download.html / site/downloads/README.md 与 downloads.json 一致")
        return 0
    if not args.write:
        print("FAIL: %d 处与 downloads.json 不一致；发版时跑 --write" % len(changes), file=sys.stderr)
        return 1

    HTML_PATH.write_text(html, encoding="utf-8")
    README_PATH.write_text(readme, encoding="utf-8")
    print("已写回 %d 处" % len(changes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
