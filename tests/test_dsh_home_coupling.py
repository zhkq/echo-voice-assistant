# -*- coding: utf-8 -*-
"""钉住"业务代码写死 DSH 家目录"的现状（P0，为 2.0 的 D6 铺路）。

2.0 要把 DSH 家目录变成 `{DATA}/dsh-home`（D6），并让所有对 DSH 的访问都经过一个
解析函数。在那之前，`app/` 里仍有几处写死 `~/.dsh` 的位置——本测试把它们登记成
**文件白名单 + 总数上限**，一旦出现新的写死点（新文件，或总数上涨）就失败。

为什么值得一个测试：这些路径是 2.0 迁移最容易漏的地方，漏了的表现是"面板里 DSH
突然离线"，而常规单元测试覆盖不到这些常量。

白名单的含义（按 2026-09-19 的现状登记）：

  * `llm_router.py`       —— **正确写法的样板**：`DSH_HOME` 环境变量优先，取不到才回落
                             `~/.dsh`。2.0 的解析函数应以它为基准。
  * `manager.py`          —— 仅提示文案里提到路径，不是真写死。
  * `worklog.py`          —— `_SESSION_STORE`，真写死（P6 改）。
  * `agents/dsh_agent.py` —— `CREDENTIALS_PATH` 与 workspace 注册表，真写死（P6 改）。

新增这类路径时请改代码走 `DSH_HOME`，**不要**放宽这里的白名单或上限。
"""
import os
import re
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")

# 任何"引号字符串里含 .dsh"的字面量：".dsh" / r"~\.dsh\..." / 提示语里的 "~/.dsh/..."
PATTERN = re.compile(r'["\'][^"\']*\.dsh[^"\']*["\']')

ALLOWED_FILES = {
    "llm_router.py",           # env-overridable: the pattern to copy
    "manager.py",              # message text only
    "worklog.py",              # _SESSION_STORE (P6)
    "agents/dsh_agent.py",     # CREDENTIALS_PATH + workspace registry (P6)
}
ALLOWED_TOTAL = 8


def scan():
    """返回 {相对路径: 命中数}，只扫 app/ 下的 .py。"""
    hits = {}
    for dirpath, dirnames, filenames in os.walk(APP_DIR):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, APP_DIR).replace("\\", "/")
            n = 0
            with open(full, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    n += len(PATTERN.findall(line))
            if n:
                hits[rel] = n
    return hits


class DshHomeCouplingTests(unittest.TestCase):
    def test_no_new_hardcoded_dsh_home(self):
        hits = scan()
        new_files = sorted(set(hits) - ALLOWED_FILES)
        self.assertEqual(
            new_files, [],
            "app/ 里出现了新的写死 ~/.dsh 路径：%s\n"
            "请改用 DSH_HOME 环境变量（见 app/llm_router.py 的写法），"
            "而不是把它加进白名单。" % new_files)

    def test_hardcoded_dsh_home_count_does_not_grow(self):
        hits = scan()
        total = sum(hits.values())
        self.assertLessEqual(
            total, ALLOWED_TOTAL,
            "写死 ~/.dsh 的位置变多了（%d > %d）：%s\n"
            "2.0 的目标是让它们全部走解析函数，只允许减少。" % (total, ALLOWED_TOTAL, hits))

    def test_llm_router_still_honours_dsh_home_env(self):
        """样板检查：唯一"正确写法"的那处必须仍然支持环境变量覆盖。"""
        path = os.path.join(APP_DIR, "llm_router.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('os.environ.get("DSH_HOME")', src,
                      "llm_router.py 不再从 DSH_HOME 取家目录了 —— "
                      "2.0 的 dsh-home 隔离（D6）依赖这个覆盖点")


if __name__ == "__main__":
    unittest.main()
