#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build-design-page.py — 把 docs/ 里的设计文档渲染成可离线打开的 HTML。

为什么需要
----------
这批设计文档有 8 张 mermaid 图和大量表格，在纯文本里读很费劲；
而浏览器能直接渲染它们。产物是**自包含的静态页**，双击即可看，不需要起服务。

三个刻意的选择
--------------
1. **不引第三方 markdown 库**。venv 里没有，也不想为几份文档加依赖；
   这里手写一个覆盖本文档实际用到的子集（标题/表格/围栏/列表/引用/行内）。
2. **mermaid 用仓库自带的 `web/vendor/mermaid.min.js`**（相对路径）。
   不走 CDN —— 这台机器的网络环境不可靠，离线必须能看。
3. **配色直接抄 `web/app.css` 的主题变量**，与 ECHO 面板同一套深色观感。

优雅降级
--------
mermaid 的源码以**转义后**的形式放进 `<pre class="mermaid">`：
- 正常时 mermaid 读 innerHTML → entityDecode → 渲染成图；
- bundle 加载失败时，`<pre>` 原样显示源码，页面仍然可读（不会一片空白）。

用法
----
    python scripts/build-design-page.py            # 出全部页面 + index
    python scripts/build-design-page.py --check    # 只报告产物是否最新
"""
import html as _html
import os
import re
import sys

# 文档清单：(源文件, 导航名, 一句话说明)
DOCS = [
    ("3.0-设计总览与组件关系.md", "3.0 总览", "组件关系图 · 一致性对齐 · 排期"),
    ("统一路由-模型能力与设备.md", "统一路由", "四域共享的选则内核 + 采集/播放设备"),
    ("能力路由-三后端与轻客户端.md", "能力路由", "本机 / ECHO 后端 / 内网公共 + 安装预算"),
    ("ECHO能力后端-服务端设计.md", "能力后端", "EnginePool · 临时文件 · 错误码契约"),
    ("前后分离-能力服务化设计.md", "前后分离", "切面原则：业务数据留客户端"),
]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS_DIR = os.path.join(ROOT, "docs")
OUT_DIR = os.path.join(DOCS_DIR, "html")
# 页内引用 mermaid 的相对路径（docs/html/x.html -> web/vendor/mermaid.min.js）
MERMAID_HREF = "../../web/vendor/mermaid.min.js"


# ---------------------------------------------------------------- 行内转换

_CODE_RE = re.compile(r"`([^`]+)`")
_TAG_RE = re.compile(r"<(br|hr)\s*/?>", re.I)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_SLOT_RE = re.compile("\x00(\\d+)\x00")


def inline(text):
    """行内标记 → HTML。顺序很重要：先把 code/tag 抽成占位符，最后再还原。"""
    slots = []

    def stash(frag):
        slots.append(frag)
        return "\x00%d\x00" % (len(slots) - 1)

    text = _CODE_RE.sub(lambda m: stash("<code>%s</code>" % _html.escape(m.group(1))), text)
    text = _TAG_RE.sub(lambda m: stash("<br>" if m.group(1).lower() == "br" else "<hr>"), text)
    # 转义剩下的裸 HTML —— 文档里出现的 <script> 之类不该被当标签
    text = _html.escape(text, quote=False)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _LINK_RE.sub(r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    return _SLOT_RE.sub(lambda m: slots[int(m.group(1))], text)


# ---------------------------------------------------------------- 块级转换

_RE_H = re.compile(r"^(#{1,6})\s+(.*)$")
_RE_HR = re.compile(r"^\s*(-{3,}|\*{3,})\s*$")
_RE_UL = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_RE_OL = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
_RE_FENCE = re.compile(r"^\s*```(\w*)\s*$")


def split_row(line):
    """表格行按 | 切分，**但不切 code span 内的 |**（文档里有 `a|b` 这种）。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    cells, buf, in_code, i = [], [], False, 0
    while i < len(s):
        ch = s[i]
        if ch == "`":
            in_code = not in_code
            buf.append(ch)
        elif ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            buf.append("|")
            i += 1
        elif ch == "|" and not in_code:
            cells.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    cells.append("".join(buf))
    return [c.strip() for c in cells]


def _is_sep_row(line):
    s = line.strip()
    if not s.startswith("|") and "|" not in s:
        return False
    return all(re.fullmatch(r":?-{2,}:?", c or "") for c in split_row(s)) and bool(split_row(s))


def render_blocks(lines):
    """块级渲染。返回 (html, headings)，headings 供侧栏导航用。"""
    out, headings = [], []
    i, n = 0, len(lines)
    hid = [0]

    def flush_paragraph(buf):
        if buf:
            out.append("<p>%s</p>" % inline(" ".join(buf)))
            buf.clear()

    para = []
    while i < n:
        line = lines[i]

        # 围栏代码
        m = _RE_FENCE.match(line)
        if m:
            flush_paragraph(para)
            lang = m.group(1).lower()
            body = []
            i += 1
            while i < n and not _RE_FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # 吃掉收尾 ```
            src = "\n".join(body)
            if lang == "mermaid":
                # 转义后再放进去：mermaid 读 innerHTML 并 entityDecode，正好还原；
                # 而 bundle 没加载时 <pre> 会原样显示源码，页面仍可读。
                out.append('<div class="diagram"><pre class="mermaid">%s</pre></div>'
                           % _html.escape(src))
            else:
                out.append('<pre class="code"><code>%s</code></pre>' % _html.escape(src))
            continue

        # 表格
        if line.strip().startswith("|") and i + 1 < n and _is_sep_row(lines[i + 1]):
            flush_paragraph(para)
            header = split_row(line)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            thead = "".join("<th>%s</th>" % inline(c) for c in header)
            tbody = []
            for r in rows:
                r = r + [""] * (len(header) - len(r))
                tbody.append("<tr>%s</tr>" % "".join("<td>%s</td>" % inline(c)
                                                     for c in r[:len(header)]))
            out.append('<div class="tw"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>'
                       % (thead, "".join(tbody)))
            continue

        # 引用：剥掉 "> " 后**递归**渲染，这样引用里的表格/列表也能正确处理
        if line.lstrip().startswith(">"):
            flush_paragraph(para)
            inner = []
            while i < n and lines[i].lstrip().startswith(">"):
                s = lines[i].lstrip()[1:]
                inner.append(s[1:] if s.startswith(" ") else s)
                i += 1
            sub, sub_h = render_blocks(inner)
            headings.extend(sub_h)
            out.append("<blockquote>%s</blockquote>" % sub)
            continue

        # 分隔线
        if _RE_HR.match(line):
            flush_paragraph(para)
            out.append("<hr>")
            i += 1
            continue

        # 标题
        m = _RE_H.match(line)
        if m:
            flush_paragraph(para)
            lvl = len(m.group(1))
            text = m.group(2).strip()
            hid[0] += 1
            anchor = "h%d" % hid[0]
            if 2 <= lvl <= 3:
                headings.append((lvl, re.sub(r"[`*]", "", text), anchor))
            out.append('<h%d id="%s">%s</h%d>' % (lvl, anchor, inline(text), lvl))
            i += 1
            continue

        # 列表（支持两级缩进嵌套）
        m_ul, m_ol = _RE_UL.match(line), _RE_OL.match(line)
        if m_ul or m_ol:
            flush_paragraph(para)
            items = []          # (indent, ordered, text)
            while i < n:
                mu, mo = _RE_UL.match(lines[i]), _RE_OL.match(lines[i])
                if mu:
                    items.append((len(mu.group(1)), False, mu.group(2)))
                elif mo:
                    items.append((len(mo.group(1)), True, mo.group(3)))
                elif lines[i].strip() and lines[i].startswith((" ", "\t")) and items:
                    # 续行：并进上一条
                    ind, od, tx = items[-1]
                    items[-1] = (ind, od, tx + " " + lines[i].strip())
                else:
                    break
                i += 1
            out.append(_render_list(items))
            continue

        # 空行
        if not line.strip():
            flush_paragraph(para)
            i += 1
            continue

        para.append(line.strip())
        i += 1

    flush_paragraph(para)
    return "\n".join(out), headings


def _render_list(items):
    """两级列表：缩进 > 0 视为上一项的嵌套子列表。"""
    html, stack = [], []          # stack: [(indent, tag)]
    for indent, ordered, text in items:
        tag = "ol" if ordered else "ul"
        if not stack:
            stack.append((indent, tag))
            html.append("<%s>" % tag)
        elif indent > stack[-1][0]:
            stack.append((indent, tag))
            html.append("<%s>" % tag)
        else:
            while len(stack) > 1 and indent < stack[-1][0]:
                html.append("</%s>" % stack.pop()[1])
            html.append("</li>")
        html.append("<li>%s" % inline(text))
    while stack:
        html.append("</li></ul>" if stack[-1][1] == "ul" else "</li></ol>")
        stack.pop()
    return "".join(html)


# ---------------------------------------------------------------- 页面外壳

_CSS = """
:root{--bg:#0f1117;--card:#171a23;--card2:#1d2230;--border:#2a2f3e;--text:#e6e9f0;
--muted:#8b93a7;--accent:#4f8cff;--green:#3ecf8e;--yellow:#e6b84c;--red:#ef5b5b;
--purple:#a78bfa;--radius:10px}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
code{background:var(--card2);border:1px solid var(--border);border-radius:4px;
padding:.08em .38em;font-family:"Cascadia Mono",Consolas,monospace;font-size:.88em}
pre.code{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
padding:14px 16px;overflow:auto}
pre.code code{background:none;border:0;padding:0;font-size:.86em;line-height:1.6}
.layout{display:flex;align-items:flex-start;max-width:1720px;margin:0 auto}
nav{position:sticky;top:0;width:232px;flex:0 0 232px;height:100vh;overflow:auto;
padding:22px 12px 40px;border-right:1px solid var(--border)}
nav .brand{font-weight:700;font-size:15px;margin:0 8px 4px}
nav .brand small{display:block;font-weight:400;color:var(--muted);font-size:12px;margin-top:2px}
nav .docs{margin:14px 0 18px;padding:0 0 14px;border-bottom:1px solid var(--border)}
nav .docs a{display:block;padding:6px 8px;border-radius:6px;color:var(--muted);font-size:13px}
nav .docs a:hover{background:var(--card);color:var(--text);text-decoration:none}
nav .docs a.on{background:var(--card2);color:var(--text);font-weight:600}
nav .toc a{display:block;padding:3px 8px;color:var(--muted);font-size:12.5px;
border-left:2px solid transparent}
nav .toc a:hover{color:var(--text);text-decoration:none;border-left-color:var(--border)}
nav .toc a.l3{padding-left:20px;font-size:12px}
main{flex:1 1 auto;min-width:0;padding:30px 40px 120px}
header.doc{border-bottom:1px solid var(--border);padding-bottom:16px;margin-bottom:26px}
header.doc h1{margin:0 0 6px;font-size:27px;line-height:1.3}
header.doc .src{color:var(--muted);font-size:12.5px}
h1,h2,h3,h4{line-height:1.35;scroll-margin-top:18px}
h2{font-size:21px;margin:40px 0 14px;padding-top:14px;border-top:1px solid var(--border)}
h2:first-of-type{border-top:0;padding-top:0}
h3{font-size:17px;margin:26px 0 10px;color:#cfd6e6}
h4{font-size:15px;margin:20px 0 8px;color:var(--muted)}
p{margin:10px 0}
ul,ol{margin:10px 0;padding-left:24px}
li{margin:5px 0}
li>ul,li>ol{margin:5px 0}
blockquote{margin:14px 0;padding:10px 16px;background:var(--card);
border-left:3px solid var(--accent);border-radius:0 var(--radius) var(--radius) 0;color:#c8cfdd}
blockquote>p:first-child{margin-top:0}
blockquote>p:last-child{margin-bottom:0}
hr{border:0;border-top:1px solid var(--border);margin:34px 0}
.tw{overflow-x:auto;margin:16px 0;border:1px solid var(--border);border-radius:var(--radius)}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:8px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--border)}
th{background:var(--card2);font-weight:600;white-space:nowrap}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:#1a1e29}
strong{color:#fff;font-weight:600}
.diagram{margin:20px 0;padding:14px;background:var(--card);border:1px solid var(--border);
border-radius:var(--radius);overflow-x:auto}
/* mermaid 渲染时会把 SVG **塞进** pre.mermaid 里（替换其内容），
   所以这里只负责"还没渲染 / 渲染失败"时的源码外观。 */
.diagram pre.mermaid{margin:0;background:none;color:var(--muted);font-size:12px;
font-family:"Cascadia Mono",Consolas,monospace;white-space:pre-wrap}
/* 一旦里面有 svg（JS 加 .rendered），就把字体与颜色交还给 mermaid。
   —— 早先这里写成 .diagram.done pre.mermaid{display:none}，等于把渲染好的图自己藏了。 */
.diagram.rendered pre.mermaid{white-space:normal;font-size:inherit;color:inherit}
/* mermaid 给 SVG 打 width="100%"，宽图会被整体缩到容器宽 —— 实测一张自然宽 3170px 的图
   被压到 1348px，14px 的字变成约 6px，等于看不清。
   注意：**光靠 CSS 的 width:auto 没用**（SVG 的 auto 仍会解析成 100%），
   所以由 JS 按 viewBox 把自然尺寸写成确定的 width/height 属性（见 markRendered）。
   这里只声明"缩小"这一档：加了 .fit 才按容器宽缩放。 */
.diagram.fit pre.mermaid svg{width:100% !important;max-width:100% !important;height:auto !important}
.fitbtn{display:none;margin:0 0 10px;padding:3px 10px;font-size:12px;font-family:inherit;
background:var(--card2);color:var(--muted);border:1px solid var(--border);
border-radius:6px;cursor:pointer}
.diagram.overflow .fitbtn{display:inline-block}
.fitbtn:hover{color:var(--text);border-color:var(--accent)}
.banner{display:none;margin:0 0 18px;padding:10px 14px;border-radius:var(--radius);
background:#2a2318;border:1px solid var(--yellow);color:var(--yellow);font-size:13px}
.no-mermaid .banner{display:block}
@media print{
  nav{display:none}main{padding:0}.layout{max-width:none}
  body{background:#fff;color:#111}.diagram{border-color:#ccc}
}
@media (max-width:900px){
  nav{display:none}.layout{display:block}main{padding:20px 16px 80px}
}
"""

_JS = """
(function () {
  var banner = document.querySelector('.banner');
  if (!window.mermaid) {
    document.documentElement.classList.add('no-mermaid');
    if (banner) banner.textContent = '⚠️ 没能加载 ' + @@MERMAID@@ +
      '（需要保持仓库目录结构）。下面是 mermaid 源码，可直接粘到支持 mermaid 的编辑器里看。';
    return;
  }
  // startOnLoad:false —— 自己驱动 run()，这样能拿到 promise 做错误呈现，
  // 也避免 bundle 自己的 load 钩子再跑一遍。
  mermaid.initialize({
    startOnLoad: false, theme: 'dark', securityLevel: 'loose',
    themeVariables: {
      fontFamily: 'inherit', fontSize: '14px',
      primaryColor: '#1d2230', primaryTextColor: '#e6e9f0', primaryBorderColor: '#3a4256',
      lineColor: '#8b93a7', secondaryColor: '#171a23', tertiaryColor: '#0f1117',
      background: '#171a23', mainBkg: '#1d2230', nodeBorder: '#3a4256',
      clusterBkg: '#141822', clusterBorder: '#2a2f3e', titleColor: '#e6e9f0'
    }
  });
  function markRendered() {
    // pre.mermaid 里出现 svg = 这个图渲染成功了 → 交出源码外观
    document.querySelectorAll('.diagram').forEach(function (d) {
      var svg = d.querySelector('svg');
      if (!svg) return;
      // 按 viewBox 把自然尺寸钉成确定的 width/height 属性。
      // 为什么必须在 JS 里做：mermaid 让 svg width="100%"，
      // 而 CSS 的 `width:auto` 对 SVG 仍会解析成 100%，缩不掉也放不大。
      var vb = (svg.getAttribute('viewBox') || '').trim().split(/[\s,]+/);
      if (vb.length === 4) {
        var nw = parseFloat(vb[2]), nh = parseFloat(vb[3]);
        if (nw > 0 && nh > 0) {
          svg.setAttribute('width', nw);
          svg.setAttribute('height', nh);
        }
      }
      svg.style.maxWidth = 'none';   // 清掉 mermaid 打的 inline max-width
      d.classList.add('rendered');
      // 自然宽超出容器 → 给一个「适应宽度」开关（此时还没加 .fit，量到的是自然宽）
      var natural = svg.getBoundingClientRect().width;
      var avail = d.clientWidth - 28;          // 减去 .diagram 的左右 padding
      if (natural > avail + 4) {
        d.classList.add('overflow');
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'fitbtn';
        b.textContent = '适应宽度';
        b.addEventListener('click', function () {
          var fit = d.classList.toggle('fit');
          b.textContent = fit ? '原始大小' : '适应宽度';
          d.scrollLeft = 0;
        });
        d.insertBefore(b, d.firstChild);
      }
    });
    // 页级开关：图多的时候逐张点太烦。只在真的有超宽图时才出现。
    var wrapped = document.querySelectorAll('.diagram.overflow').length;
    if (wrapped && !document.getElementById('fitall')) {
      var host = document.querySelector('header.doc');
      if (host) {
        var all = document.createElement('button');
        all.id = 'fitall';
        all.type = 'button';
        all.className = 'fitbtn';
        all.style.display = 'inline-block';
        all.style.margin = '10px 0 0';
        all.textContent = '全部适应宽度（' + wrapped + ' 张超宽）';
        all.addEventListener('click', function () {
          var diags = document.querySelectorAll('.diagram.overflow');
          var toFit = !document.querySelector('.diagram.overflow.fit');
          diags.forEach(function (d) {
            d.classList.toggle('fit', toFit);
            d.scrollLeft = 0;
            var b = d.querySelector('.fitbtn');
            if (b) b.textContent = toFit ? '原始大小' : '适应宽度';
          });
          all.textContent = toFit
            ? '全部原始大小（' + diags.length + ' 张）'
            : '全部适应宽度（' + diags.length + ' 张超宽）';
        });
        host.appendChild(all);
      }
    }
  }
  function start() {
    var p;
    try { p = mermaid.run({ querySelector: '.mermaid' }); } catch (e) { p = Promise.reject(e); }
    Promise.resolve(p).catch(function (e) {
      // 渲染失败时**说出来**，而不是安静地只留一段源码
      if (banner) {
        banner.style.display = 'block';
        banner.textContent = '⚠️ 有图渲染失败：' + (e && e.message ? e.message : e) +
          '（源码已原样保留在各图框里）';
      }
      if (window.console) console.error(e);
    }).then(markRendered);
  }
  if (document.readyState === 'complete') { start(); }
  else { window.addEventListener('load', start); }
})();
"""


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@@TITLE@@ · ECHO 3.0 设计</title>
<style>@@CSS@@</style>
</head><body>
<div class="layout">
@@NAV@@
<main>
<div class="banner"></div>
<header class="doc"><h1>@@TITLE@@</h1>
<div class="src">源文件 <code>docs/@@DOCFILE@@</code> · 由 <code>scripts/build-design-page.py</code> 生成，勿手改</div>
</header>
@@BODY@@
</main>
</div>
<script src="@@MERMAID_HREF@@"></script>
<script>@@JS@@</script>
</body></html>
"""

_INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ECHO 3.0 设计文档</title><style>@@CSS@@
.idx{max-width:820px;margin:0 auto;padding:60px 24px}
.idx h1{font-size:30px;margin:0 0 8px}
.idx .sub{color:var(--muted);margin-bottom:34px}
a.item{display:block;background:var(--card);border:1px solid var(--border);
border-radius:var(--radius);padding:18px 20px;margin:12px 0;color:var(--text)}
a.item:hover{border-color:var(--accent);text-decoration:none}
a.item h3{margin:0 0 6px;font-size:17px;color:var(--accent)}
a.item p{margin:0 0 8px;color:var(--muted);font-size:13.5px}
</style></head><body><div class="idx">
<h1>ECHO 3.0 设计文档</h1>
<div class="sub">五份设计 + 组件关系图 · 离线可看 · 由 <code>scripts/build-design-page.py</code> 生成</div>
@@ITEMS@@
</div></body></html>
"""


def _nav_html(current_file, headings):
    docs = []
    for fn, name, _desc in DOCS:
        cls = ' class="on"' if fn == current_file else ""
        docs.append('<a href="%s.html"%s>%s</a>' % (fn[:-3], cls, _html.escape(name)))
    toc = []
    for lvl, text, anchor in headings:
        toc.append('<a class="l%d" href="#%s">%s</a>' % (lvl, anchor, _html.escape(text)))
    return ('<nav><div class="brand">ECHO 3.0 设计<small>架构设计文档</small></div>'
            '<div class="docs">%s</div><div class="toc">%s</div></nav>'
            % ("".join(docs), "".join(toc)))


def render_doc(md_path):
    with open(md_path, encoding="utf-8") as fh:
        raw = fh.read()
    lines = raw.split("\n")
    title = os.path.basename(md_path)[:-3]
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]
    body, headings = render_blocks(lines)
    return title, body, headings


def build_page(md_path, out_path):
    title, body, headings = render_doc(md_path)
    doc_file = os.path.basename(md_path)
    # 刻意用 @@TOKEN@@ + replace 而不是 % 格式化：CSS/JS 里有大量 % 与 {}，
    # 走 %-格式化会被当成格式符（`max-width:100%` 直接抛 unsupported format character）。
    page = (_PAGE_TEMPLATE
            .replace("@@TITLE@@", _html.escape(title))
            .replace("@@CSS@@", _CSS)
            .replace("@@NAV@@", _nav_html(doc_file, headings))
            .replace("@@DOCFILE@@", _html.escape(doc_file))
            .replace("@@BODY@@", body)
            .replace("@@MERMAID_HREF@@", MERMAID_HREF)
            .replace("@@JS@@", _JS.replace("@@MERMAID@@", '"%s"' % MERMAID_HREF)))
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return len(page)


def build_index(pages):
    items = []
    for fn, name, desc in DOCS:
        if fn not in pages:
            continue
        items.append('<a class="item" href="%s.html"><h3>%s</h3><p>%s</p>'
                     '<code>docs/%s</code></a>'
                     % (fn[:-3], _html.escape(name), _html.escape(desc), _html.escape(fn)))
    page = (_INDEX_TEMPLATE
            .replace("@@CSS@@", _CSS)
            .replace("@@ITEMS@@", "".join(items)))
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)


def main(argv):
    check = "--check" in argv
    os.makedirs(OUT_DIR, exist_ok=True)
    built, missing, stale = [], [], []
    for fn, _name, _desc in DOCS:
        src = os.path.join(DOCS_DIR, fn)
        if not os.path.isfile(src):
            missing.append(fn)
            continue
        out = os.path.join(OUT_DIR, fn[:-3] + ".html")
        if check:
            if not os.path.isfile(out):
                stale.append(fn + "（还没生成）")
            elif os.path.getmtime(out) < os.path.getmtime(src):
                stale.append(fn + "（源文件更新，产物过期）")
            continue
        size = build_page(src, out)
        built.append((fn, size))
    if check:
        for s in stale:
            print("  [过期] " + s)
        for m in missing:
            print("  [缺源文件] " + m)
        if stale or missing:
            print("\n跑 `python scripts/build-design-page.py` 重新生成")
            return 2
        print("  docs/html/ 与源码一致")
        return 0
    build_index([f for f, _s in built])
    for fn, size in built:
        print("  %-34s -> docs/html/%s.html  (%d KB)" % (fn, fn[:-3], size // 1024))
    print("  %-34s -> docs/html/index.html" % "（索引）")
    if missing:
        for m in missing:
            print("  [跳过] 找不到 docs/" + m)
    print("\n打开：docs\\html\\index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
