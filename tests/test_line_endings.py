# -*- coding: utf-8 -*-
"""换行符守卫：`server/` 下的文件必须保持 LF（在 Windows 上也要）。

背景
----
仓库开着 `core.autocrlf=true`，checkout 时会把文本文件写成 CRLF。对本机跑的
Python 无害，但 `server/` 是**只跑在 Linux 容器里**的那一半：Dockerfile 变成 CRLF 后，
容器里 `/bin/sh` 把 `\\r` 当成参数的一部分（`pip install -r requirements.txt\\r`），
构建会以 "not found" 这类看不出根因的方式失败 —— 与 `.githooks` / `*.sh` 是同一类坑，
只是它坏在镜像里、本机复现不出来。

所以 `.gitattributes` 里声明了 `server/** -text`。本文件把这个约定变成可执行断言：

  1. `.gitattributes` 确实声明了 `server/** -text`（否则 checkout 又会写回 CRLF）；
  2. 工作树里 `server/` 下的文本文件当前不含 CRLF（防编辑器/工具把 BOM 或行尾改掉）；
  3. 顺带钉住原有的两条：`*.sh` 与 `.githooks/**` 也必须是 `-text` ——
     它们是同类坑里最早踩过的（`\\r: not found`）。

注：只查"声明在不在"与"当前字节干不干净"，**不**去查 git index 里的 blob
（那需要跑 git，单测不该依赖 git 可执行文件在场）。

这条守卫**当场抓过一次真事故**（2026-09-24）：一次临时的变异脚本用
`pathlib.Path.read_text()` + `write_text()` 改 `server/routes.py`，
在 Windows 上把整份文件悄悄转成了 CRLF —— `read_text` 按 universal newlines
把 `\r\n` 归一成 `\n`，`write_text` 又按 `os.linesep` 把它们写回 `\r\n`。
`edit` 工具不会这样，但**任何"读出来再写回去"的脚本都会**。
所以改过 `server/` 下的文件之后，跑这个用例，别靠眼睛看。
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GITATTRIBUTES = os.path.join(ROOT, ".gitattributes")

#: 二进制权重不该被 `-text` 之外的规则碰；这些扩展名跳过行尾检查
BINARY_EXT = (".onnx", ".bin", ".npz", ".model", ".pt", ".pb", ".wav", ".mp3", ".png", ".jpg")


def _read_gitattributes():
    with open(GITATTRIBUTES, "r", encoding="utf-8") as f:
        return f.read()


class GitAttributesDeclareLfForServer(unittest.TestCase):
    def test_server_is_declared_minus_text(self):
        """`server/** -text` 必须在 —— 少了它，Dockerfile 在 Windows 上 checkout 就变 CRLF。"""
        body = _read_gitattributes()
        rules = [ln.strip() for ln in body.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        self.assertIn("server/** -text", rules,
                      ".gitattributes 缺少 `server/** -text`：Dockerfile 会在 checkout 时"
                      "变成 CRLF，容器构建失败（详见本文件顶部）")

    def test_shell_scripts_stay_minus_text(self):
        """原有两条（最早踩过的同一个坑）不能被后来的编辑挤掉。"""
        rules = [ln.strip() for ln in _read_gitattributes().splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        self.assertIn(".githooks/** -text", rules)
        self.assertIn("*.sh          -text", rules)


class ServerTreeHasNoCrlf(unittest.TestCase):
    def test_no_crlf_in_server_text_files(self):
        base = os.path.join(ROOT, "server")
        self.assertTrue(os.path.isdir(base), "server/ 目录不存在？")
        bad = []
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in sorted(filenames):
                if fn.lower().endswith(BINARY_EXT):
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, "rb") as f:
                    raw = f.read()
                scanned += 1
                if b"\r\n" in raw:
                    bad.append(os.path.relpath(path, ROOT))
        self.assertGreaterEqual(scanned, 10, f"只扫到 {scanned} 个文件，路径可能配错了")
        self.assertEqual(bad, [], "以下文件含 CRLF（应为 LF，否则容器里会出错）：\n"
                                  + "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
