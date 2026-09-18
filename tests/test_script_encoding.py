# -*- coding: utf-8 -*-
"""脚本编码守卫：含非 ASCII 的 .ps1 必须带 UTF-8 BOM。

背景
----
Windows PowerShell 5.1 对**无 BOM** 的 .ps1 按系统 ANSI（本机 936/GBK）解析。
文件里若含 UTF-8 中文，就会被读成乱码；乱码里如果凑出引号或反引号，
整份脚本会以"字符串缺少终止符"之类的方式解析失败 —— 而失败是**静默**的
（2026-09-12：`start.ps1` / `launch-desktop.ps1` 就是这么坏掉且日志里什么都没写；
2026-09-18 又发现 `restart-echo.ps1` / `start.ps1` / `startup.ps1` 三个同病）。

`docs/powershell-编码与脚本经验.md` 定的约定是「本仓库脚本一律纯 ASCII 或带 BOM」，
但以前只是注释里的君子协定。本文件把它变成可执行断言：

  1. `scripts/` 与 `dsh-failover/` 下的 .ps1 必须是合法 UTF-8；
  2. 含非 ASCII 字节的，必须带 UTF-8 BOM；
  3. BOM 之后不得再有别的 BOM（避免双重 BOM）。

注：`.vbs` 不在此列 —— `install.ps1` 生成的启动器**故意**用 GBK/ANSI，
因为 wscript 按系统 ANSI 读取（见 install.ps1 里 Step07 的说明）。
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ("scripts", "dsh-failover")
UTF8_BOM = b"\xef\xbb\xbf"


def _ps1_files():
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in ("__pycache__",)]
            for fn in sorted(filenames):
                if fn.lower().endswith(".ps1"):
                    yield os.path.join(dirpath, fn), os.path.relpath(
                        os.path.join(dirpath, fn), ROOT)


class PowerShellScriptsAreBomSafe(unittest.TestCase):
    def test_ps1_with_non_ascii_has_utf8_bom(self):
        bad, not_utf8 = [], []
        for path, rel in _ps1_files():
            with open(path, "rb") as f:
                raw = f.read()
            body = raw[len(UTF8_BOM):] if raw.startswith(UTF8_BOM) else raw
            try:
                body.decode("utf-8")
            except UnicodeDecodeError as e:
                not_utf8.append(f"{rel}: 不是合法 UTF-8（{e}）—— 请转成 UTF-8 后再决定要不要 BOM")
                continue
            has_non_ascii = any(b > 0x7F for b in body)
            if has_non_ascii and not raw.startswith(UTF8_BOM):
                bad.append(f"{rel}: 含非 ASCII 但缺 UTF-8 BOM"
                           "（WinPS 5.1 会按 ANSI 解析 → 中文乱码，甚至静默解析失败）")
            if body.startswith(UTF8_BOM):
                bad.append(f"{rel}: BOM 之后又出现了 BOM（双重 BOM）")
        self.assertEqual(not_utf8, [], "存在非 UTF-8 的 .ps1：\n" + "\n".join(not_utf8))
        self.assertEqual(bad, [], "脚本编码不合规：\n" + "\n".join(bad))

    def test_scan_finds_scripts(self):
        """防止 SCAN_DIRS 写错导致"扫了个寂寞"却永远绿。"""
        found = list(_ps1_files())
        self.assertGreaterEqual(len(found), 10,
                                f"只扫到 {len(found)} 个 .ps1，SCAN_DIRS 可能配错了")


if __name__ == "__main__":
    unittest.main()
