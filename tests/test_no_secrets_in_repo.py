# -*- coding: utf-8 -*-
"""**密钥/凭据不许进仓库**（2026-09-29 加）。

起因：用户在实测在线转写时给了一把真密钥，随即问"doc 目录是不是也该 git 忽略"。
正确回答是**不该忽略 docs**（它是交付物与权威记录，还有门禁在读它），
而且忽略目录**挡不住密钥** —— 挡它只有一个办法：**根本进不去**，且有机械判据。

这条用例扫的是 **git 跟踪的文件**（`git ls-files`）—— 那正好等于"会被推出去的东西"，
所以 `dist/`、`data/`、`models/`、`*.env` 这些本来就被忽略的目录天然不在扫描范围内。

## 判据是怎么定出来的（别改成"长度超 20 就算"）

真密钥有**形状**，不是"够长就算"：

| 规则 | 为什么 |
|---|---|
| `sk-ws-` + 40 个以上 base64 字符 | 千问AI平台的 Key 是这个形状（实测那把 100+ 字符） |
| `sk-` + 32 个以上**无连字符**的字母数字 | OpenAI 系的形状（`sk-proj-…`） |
| `-----BEGIN … PRIVATE KEY-----` | 私钥整块 |
| `AKIA` + 16 位大写字母数字 | AWS Access Key ID |

**刻意不按"长度"判**：仓库里的用例塞满了**故意造的假密钥**
（`sk-test` / `sk-x` / `sk-llm-secret` / `sk-super-secret-value-123` …），
纯长度规则会把这些全判成红的，于是这条护栏很快会被加白名单加到失效。
现在这套规则在**整个跟踪集**上的误报是 **0**（2026-09-29 实测）。

失败时的输出**只打掩码**（前 6 位 + 长度），绝不把命中的串整条写进日志 ——
否则"扫密钥的用例"自己成了泄漏点。
"""
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 只扫文本类文件（二进制里"像密钥"的随机字节没有意义）
TEXT_SUFFIXES = (".py", ".md", ".js", ".html", ".json", ".yaml", ".yml", ".txt", ".ps1",
                 ".sh", ".cfg", ".ini", ".toml", ".example", ".css")

#: 规则用字符串拼出来，免得**本文件自己**命中自己（扫到了也说不清是谁）
_DASH = "-"
RULES = (
    ("千问AI平台 Key", re.compile("sk" + _DASH + "ws" + _DASH + r"[A-Za-z0-9._\-]{40,}")),
    ("OpenAI 系 Key", re.compile("sk" + _DASH + r"(?:proj" + _DASH + r")?[A-Za-z0-9]{32,}")),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----")),
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
)

#: 不扫的目录 —— 与 `.gitignore` 里那几条**一一对应**（`.git` / `venv` / `models` / `data`
#: / `dist` / 缓存）。这里不 shell 出去问 `git ls-files`：本仓库所在环境的沙箱**不允许**
#: 子进程走管道取输出（实测 `stdout=None`），写在用例里的东西不能依赖那个。
#: **明确豁免**（2026-09-29 第一次跑就抓到它，逐条判过）：`tests/tls_test_cert.py` 里
#: 内联的自签证书 + 私钥。它不是泄漏 —— 它只用来给"客户端证书固定（pinned context）"
#: 的用例在 **localhost** 上起一个 TLS 服务端，域名是测试域名、没有任何生产用途，
#: 而且删掉它那些用例就没法验"没给上下文就拒连"这条纪律。
#:
#: 豁免**指名道姓**、不放松规则：规则一直严，被豁免的东西一个个写在案上（D24）。
ALLOWED = {os.path.join("tests", "tls_test_cert.py")}

SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "models", "data", "dist",
             "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "html"}


def _mask(hit: str) -> str:
    """只留前 6 位与长度。**绝不整条打出来**。"""
    return "%s…（共 %d 字符）" % (hit[:6], len(hit))


def _tracked_text_files():
    """会进仓库的文本文件（**自己走目录，不问 git** —— 见 `SKIP_DIRS` 的说明）。

    判据是"排除 `.gitignore` 里那些目录"，所以它比 `git ls-files` **更严**
    （万一某个被忽略的文件混进来，只会多扫、不会漏扫）。
    """
    out = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if not name.lower().endswith(TEXT_SUFFIXES):
                continue
            rel = os.path.relpath(os.path.join(base, name), ROOT)
            if name.endswith(".env") or name.endswith(".db"):
                continue                     # 本就不该进仓库的运行时文件
            out.append(rel)
    return sorted(out)


class SecretScannerTests(unittest.TestCase):
    """先证明"这个扫描器抓得住"，再说"仓库是干净的"。"""

    def test_the_scanner_catches_realistic_keys(self):
        samples = (
            ("sk" + "-ws-" + "H.PRMMEED.3ws4." + "A" * 60, "千问AI平台 Key"),
            ("sk" + "-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6", "OpenAI 系 Key"),
            # 这两条也拼出来：本文件若出现裸 PEM 头/AKIA 串，扫自己就会红
            ("-----BEGIN " + "RSA PRIVATE " + "KEY-----", "私钥块"),
            ("AKIA" + "IOSFADNN7EXAMPLE", "AWS Access Key"),
        )
        for text, want in samples:
            with self.subTest(want=want):
                hits = [name for name, rx in RULES if rx.search(text)]
                self.assertIn(want, hits, "扫描器漏了这类密钥：%s" % want)

    def test_the_scanner_ignores_the_fake_keys_our_tests_use(self):
        """**误报会让护栏失效**：这些是仓库里真实存在的假密钥，一个都不许命中。"""
        for fake in ("sk-test", "sk-x", "sk-abc", "sk-SECRET", "sk-probe-123",
                     "sk-keep-me", "sk-live", "sk-llm-secret", "sk-meeting-secret",
                     "sk-super-secret-value-123", "sk-ws-...", "sk-stored"):
            with self.subTest(fake=fake):
                hits = [name for name, rx in RULES if rx.search(fake)]
                self.assertEqual(hits, [], "把假密钥 %s 判成了真密钥" % fake)


class NoSecretInTrackedFilesTests(unittest.TestCase):
    def test_no_key_shaped_string_is_tracked(self):
        files = _tracked_text_files()
        self.assertTrue(files, "一个文件都没扫到，这条就白跑了")
        offenders = []
        for rel in files:
            if rel in ALLOWED:
                continue
            try:
                with open(os.path.join(ROOT, rel), encoding="utf-8", errors="ignore") as fh:
                    for no, line in enumerate(fh, 1):
                        for name, rx in RULES:
                            m = rx.search(line)
                            if m:
                                offenders.append("%s:%d  %s  %s"
                                                 % (rel, no, name, _mask(m.group(0))))
            except OSError:
                continue
        self.assertEqual(offenders, [],
                         "仓库里出现了密钥形状的串（只打掩码）：\n  " + "\n  ".join(offenders))

    def test_the_scan_actually_looks_at_the_docs(self):
        """用户问的正是 docs —— 那就明确把它算进来，别"恰好扫不到"。"""
        docs = [f for f in _tracked_text_files() if f.startswith("docs" + os.sep)]
        self.assertGreaterEqual(len(docs), 10, "docs/ 没被扫到？这条护栏就少了一半意义")

    def test_the_only_exemption_is_the_test_certificate(self):
        """豁免是有代价的（它是这条护栏唯一的例外），所以**钉住它只有一条**。

        多出一条时应该有人来解释，而不是悄悄加进集合。
        """
        self.assertEqual(ALLOWED, {os.path.join("tests", "tls_test_cert.py")},
                         "豁免清单变了 —— 新增豁免要在提交说明里讲清为什么它不算泄漏")
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "tests", "tls_test_cert.py")),
                        "被豁免的文件已经不存在了？那这条豁免该删掉")


if __name__ == "__main__":
    unittest.main()
