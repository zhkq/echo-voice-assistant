# -*- coding: utf-8 -*-
"""**真渲染**一次清洗后的 mermaid —— 针"静态查不出"的那类错（2026-10-13 加）。

## 为什么必须有这一道

`_sanitize_mermaid` 的回归测试一直是**字符串断言**（`assertIn("title: …")` 之类）。
2026-10-13 就被这种东西骗过去了：模型在 `flowchart` 里写了 `title: …`，字符串看起来
"有 title 冒号，符合规范"，而**mermaid 的 flowchart 根本不认这条语句**：

    Parse error on line 2: ...flowchart TD title: 多模态智能体快速生成流程 A["
    Expecting 'SEMI','NEWLINE','EOF',... got 'UNICODE_TEXT'

用户看到的就是面板上"下面还有个图渲染失败"。字符串断言看不出这个 —— 只有真拿 mermaid
解析一遍才知道。同 `tests/test_panel_renders.py` 的立场：这类错必须真跑。

## 做法

用**仓库自带的** `web/vendor/mermaid.min.js`（= 面板用的那一份）在无头 Edge 里逐块
`mermaid.parse()`，断言每块都过。用 `file://` 打开即可，**不需要面板在跑、不碰任何数据**。

没有 Edge 时**响亮地失败**（不静默跳过）：这道闸的意义就是别让"图上不去"溜过去，
而 Windows 自带 Edge —— 与 `test_panel_renders.py` 同一个取舍。
"""
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import meeting  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MERMAID_JS = os.path.join(ROOT, "web", "vendor", "mermaid.min.js")

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

#: 每一块都是"模型真实写过"的写法（前两条对应真实事故）。
SAMPLES = {
    "flowchart 里写了 title:（2026-10-13 用户实测那一条）": (
        'flowchart TD\n'
        '    title: 多模态智能体快速生成流程\n'
        '    A["开始"] --> B["选择输入图片类型"]\n'
        '    B --> C{"是否需要预处理?"}\n'
        '    C -->|"是"| D["勾选预处理步骤<br/>(旋转/锐化/梯形矫正等)"]'
    ),
    "引号标签里又嵌半角引号（2026-09-20 那一条）": (
        'flowchart TD\n'
        '    F --> G["结论: 消费级麦克风优先过滤"他人说话"等环境音"]'
    ),
    "未加引号却带括号的标签": (
        'flowchart TD\n    A[初验(出验)证书] --> B[结束]'
    ),
    "timeline：title 缺冒号 + 时间戳带冒号": (
        'timeline\n'
        'title 录音设备技术特征对比\n'
        '00:00:12 : 发起试音\n'
        '00:03:40 : 对比结论'
    ),
    "sequenceDiagram（带引号的参与者与消息）": (
        'sequenceDiagram\n'
        '    participant U as "使用者"\n'
        '    participant DE as "数字员工"\n'
        '    U->>DE: "授权以我的身份执行"\n'
        '    DE->>U: "汇报执行结果"'
    ),
    "flowchart + 引号 subgraph 标题": (
        'flowchart LR\n'
        '    subgraph "当前状态"\n'
        '        A1["功能罗列"]\n'
        '    end\n'
        '    subgraph "目标状态"\n'
        '        B1["场景驱动"]\n'
        '    end\n'
        '    A1 --> B1'
    ),
}


def _edge():
    for p in _EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


def _mm(body):
    return "```mermaid\n" + body + "\n```"


def _body_of(marked):
    """从清洗后的 markdown 里取回 mermaid 源码（首尾围栏之间那几行）。"""
    lines = marked.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.strip().startswith("```mermaid"))
    out = []
    for ln in lines[start + 1:]:
        if ln.strip().startswith("```"):
            break
        out.append(ln)
    return "\n".join(out)


class SanitizedDiagramsActuallyRenderTests(unittest.TestCase):

    def test_every_sample_parses_with_the_panels_own_mermaid(self):
        edge = _edge()
        if not edge:
            self.fail("没找到 Edge —— 这道'真渲染'检查没跑（这不等于通过）。"
                      "Windows 自带 Edge；找过：%s" % list(_EDGE_CANDIDATES))
        self.assertTrue(os.path.isfile(MERMAID_JS), "仓库里没有 web/vendor/mermaid.min.js")

        blocks = []
        for name, body in SAMPLES.items():
            cleaned = _body_of(meeting._sanitize_mermaid(_mm(body)))
            blocks.append({"id": name, "text": cleaned})

        tmp = tempfile.mkdtemp(prefix="echo-mermaid-")
        try:
            shutil.copyfile(MERMAID_JS, os.path.join(tmp, "mermaid.min.js"))
            payload = json.dumps(blocks, ensure_ascii=False).replace("</", "<\\/")
            page = (
                '<!doctype html><meta charset="utf-8"><title>t</title>'
                '<script type="application/json" id="blocks">' + payload + '</script>'
                '<script src="mermaid.min.js"></script><div id="RESULT">pending</div>'
                '<script>(async () => {'
                'const bs = JSON.parse(document.getElementById("blocks").textContent);'
                'const res = [];'
                'try { mermaid.initialize({startOnLoad:false, theme:"dark",'
                ' securityLevel:"strict"}); }'
                'catch (e) { document.getElementById("RESULT").textContent = "INITFAIL " + e; return; }'
                'for (const b of bs) {'
                '  try { await mermaid.parse(b.text); res.push({id:b.id, ok:true}); }'
                '  catch (e) { res.push({id:b.id, ok:false,'
                '    err:String((e && e.message) || e).replace(/\\s+/g, " ").slice(0, 300)}); }'
                '}'
                'document.getElementById("RESULT").textContent = JSON.stringify(res);'
                '})();</script>')
            page_path = os.path.join(tmp, "probe.html")
            with io.open(page_path, "w", encoding="utf-8") as fh:
                fh.write(page)

            profile = tempfile.mkdtemp(prefix="echo-edge-")
            try:
                r = subprocess.run(
                    [edge, "--headless=new", "--disable-gpu", "--no-first-run",
                     "--user-data-dir=" + profile, "--virtual-time-budget=20000",
                     "--dump-dom", "file:///" + page_path.replace("\\", "/")],
                    capture_output=True, timeout=300)
                dom = (r.stdout or b"").decode("utf-8", "replace")
            finally:
                shutil.rmtree(profile, ignore_errors=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertGreater(len(dom), 200, "无头 Edge 没吐出 DOM")
        m = re.search(r'id="RESULT">(.*?)</div>', dom, re.S)
        self.assertIsNotNone(m, "探针页没写结果（页面没跑起来？）：%s" % dom[:400])
        raw = html.unescape(m.group(1))
        self.assertNotIn("pending", raw, "探针页的脚本没跑完：%s" % raw[:200])
        results = json.loads(raw)

        bad = [(x["id"], x.get("err", "")) for x in results if not x.get("ok")]
        self.assertEqual(
            bad, [],
            "清洗之后的图**真的**渲染不过 —— 字符串断言看不出这类错，所以别改这条：\n"
            + "\n".join("%s\n    -> %s" % (b[0], b[1]) for b in bad))


if __name__ == "__main__":
    unittest.main()
