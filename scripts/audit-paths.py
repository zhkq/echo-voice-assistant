# -*- coding: utf-8 -*-
"""audit-paths.py - ECHO 2.0 P3 seam audit (read-only report, ASCII output).

Why this exists
---------------
D10-D12 and the path layer (app/paths.py) require two things:

  1. the install root and the data roots are DERIVED IN EXACTLY ONE PLACE
     (app/paths.py); every other module asks the layer;
  2. platform differences live ONLY under app/platform/<os>/ - no os.name /
     sys.platform / drive-letter / LOCALAPPDATA branches in app/ business code.

P3 has to convert the leftovers, and a guard test cannot be written before the
inventory is known. Grepping by hand is what this replaces: it walks app/, and
reports every site that still deviates, with the file, line and the rule.

Usage
-----
    python scripts/audit-paths.py            # human report, always exit 0
    python scripts/audit-paths.py --check    # exit 1 if anything is NOT allowed

Exit codes: 0 = clean (or report mode), 1 = violations found in --check mode.

Notes
-----
* Read-only. It never writes, never imports app/ (so it works even when the tree
  cannot be imported, e.g. missing optional deps).
* ASCII-only output on purpose: it must survive an ANSI/GBK console.
* Comments and docstrings are ignored, so prose that mentions C:\\example does
  not count as a violation.
"""
import io
import os
import re
import sys
import tokenize

# --------------------------------------------------------------------------- rules

RULES = {
    "PATH_DERIVATION": "derives a root itself instead of asking app/paths.py",
    "DRIVE_LITERAL": "absolute drive path in a string literal",
    "PLATFORM_TOKEN": "platform branch/token outside app/platform/",
}

#: prefix -> rules that are acceptable there ("*" = every rule)
ALLOWED = [
    ("app/platform/", "*"),
    ("app/paths.py", "PATH_DERIVATION"),
]

#: 真正的盘符绝对路径；负向后顾排除 http:// / https:// 这类 scheme（"p:/" 会误中）。
DRIVE_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")
#: tokens that mean "this module is branching on the platform"
#: 注意 win32 / macos / linux 是**清单中立标记**（见 app/platform/__init__.py
#: 「清单用的平台标记」），清单里写它们不算平台分支，所以不在此列。
PLATFORM_NAMES = {
    "darwin", "cygwin", "msys",
    "LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)",
    "SystemRoot", "windir",
}
#: 属性式平台判定。tokenize 会在 "." 两侧加空格（"os . name"），
#: 所以必须用容忍空白的正则，不能拿 "os.name" 直接做子串匹配。
PLATFORM_ATTR_RES = (
    re.compile(r"\bos\s*\.\s*name\b"),
    re.compile(r"\bsys\s*\.\s*platform\b"),
    re.compile(r"\bplatform\s*\.\s*(?:system|platform)\b"),
)
DERIVE_NAMES = ("dirname", "abspath", "__file__")

SKIP_DIRS = {"__pycache__", ".git", "node_modules"}


def _iter_app_files(root):
    app_dir = os.path.join(root, "app")
    for base, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(base, name)


def _rel(root, path):
    return os.path.relpath(path, root).replace("\\", "/")


def _allowed(rel, rule):
    for prefix, r in ALLOWED:
        if rel.startswith(prefix) and (r == "*" or r == rule):
            return True
    return False


def scan_file(path):
    """Return [(rule, lineno, text)] for one file. Never raises."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return [("UNREADABLE", 0, "cannot open file")]

    hits = []
    lines = raw.decode("utf-8", "replace").splitlines()

    # code-only rendering per line (strings blanked, comments dropped) so that
    # prose in comments/docstrings cannot trip the rules
    code_lines = {}
    string_lines = {}
    prev_meaningful = None
    try:
        toks = list(tokenize.tokenize(io.BytesIO(raw).readline))
    except Exception:
        return [("UNPARSEABLE", 0, "tokenize failed")]

    for tok in toks:
        if tok.type == tokenize.COMMENT:
            continue
        ln = tok.start[0]
        if tok.type == tokenize.STRING:
            standalone = prev_meaningful in (
                None, tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT)
            if not standalone:
                string_lines.setdefault(ln, []).append(tok.string)
            code_lines[ln] = code_lines.get(ln, "") + ' "" '
        else:
            code_lines[ln] = code_lines.get(ln, "") + " " + tok.string
        # 必须把 NEWLINE/INDENT/DEDENT 也记进来：否则 `def f():` 之后缩进里的
        # 第一个字符串（docstring）会被当成表达式字符串，docstring 里的
        # `C:\example` 之类就会误报（本工具最初就有这个洞）。
        if tok.type != tokenize.ENCODING:
            prev_meaningful = tok.type

    for ln, code in sorted(code_lines.items()):
        text = code.strip()
        # tokenize 会在 "." 两侧插空格（``os . path``），检测前先去掉点周围空白，
        # 这样 (a) 负向后顾能识别 ``X.__file__``（不是本模块的 __file__）；
        # (b) ``Path(__file__).resolve().parent`` 这类 pathlib 写法能一次抓全。
        compact = re.sub(r"\s*\.\s*", ".", code)
        # 只抓**裸** __file__：``speechbrain.__file__`` 是第三方包自身位置，与安装根无关。
        if re.search(r"(?<![\w.])__file__", compact) and any(
                k in compact for k in ("dirname", "abspath", "parent", "resolve(")):
            hits.append(("PATH_DERIVATION", ln, text))
        if any(rx.search(compact) for rx in PLATFORM_ATTR_RES):
            hits.append(("PLATFORM_TOKEN", ln, text))
        else:
            for name in PLATFORM_NAMES:
                if re.search(r"\b%s\b" % re.escape(name), code, re.IGNORECASE):
                    hits.append(("PLATFORM_TOKEN", ln, text))
                    break

    for ln, vals in sorted(string_lines.items()):
        for val in vals:
            if DRIVE_RE.search(val):
                hits.append(("DRIVE_LITERAL", ln, val.strip()[:90]))
                continue
            # Python 3.11 把 f-string 当**一个** STRING token，里面的
            # ``{platform.system()}`` 在代码通道里是隐身字符串——必须在这里扫，
            # 否则 app/services.py:23 这种展示型分支会被漏掉。
            q = min([i for i, ch in enumerate(val) if ch in "\"'"] or [len(val)])
            if "f" in val[:q].lower() and any(rx.search(val) for rx in PLATFORM_ATTR_RES):
                hits.append(("PLATFORM_TOKEN", ln, val.strip()[:90]))
                continue
            # env-var platform tokens live INSIDE strings (e.g. "%LOCALAPPDATA%"),
            # so they have to be looked for here - the code pass blanks strings.
            for name in PLATFORM_NAMES:
                if re.search(r"\b%s\b" % re.escape(name), val, re.IGNORECASE):
                    hits.append(("PLATFORM_TOKEN", ln, val.strip()[:90]))
                    break

    return hits


def main(argv):
    check = "--check" in argv
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    found = {}
    total = 0
    allowed = 0
    for path in _iter_app_files(root):
        rel = _rel(root, path)
        for rule, ln, text in scan_file(path):
            if _allowed(rel, rule):
                allowed += 1
                continue
            found.setdefault(rule, []).append((rel, ln, text))
            total += 1

    print("ECHO P3 seam audit  root=%s" % root)
    print("rules: %s" % ", ".join(sorted(RULES)))
    print("")
    for rule in sorted(set(RULES) | set(found)):
        rows = found.get(rule) or []
        print("== %s (%d) - %s =="
              % (rule, len(rows), RULES.get(rule, "unexpected finding")))
        for rel, ln, text in rows:
            print("  %s:%s: %s" % (rel, ln, text[:120]))
        print("")
    print("SUMMARY: %d violation(s) to convert, %d site(s) already allowed"
          % (total, allowed))
    print("next: convert by rule, then shrink ALLOWED and promote this into a test")

    if check and total:
        print("CHECK FAILED: %d violation(s) outside the whitelist" % total)
        return 1
    if check:
        print("CHECK OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
