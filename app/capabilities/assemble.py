# -*- coding: utf-8 -*-
"""拼装：把各后端给回的东西，拼成"逐句 + 时间"这一种形状 —— **只在这一处实现**。

设计（`docs/能力路由` §4.4）把这条写成了硬要求：*"拼装规则要写死，别到时候各写一份"*。
理由是它会**悄悄分叉**：本地引擎给句级时间轴、服务端可能只给整段文本、
内网流式给的是增量文本 —— 如果每条路各自决定"怎么切句、时间怎么来"，
那么同一场会议里，A 段是本机的、B 段是服务端的，出来的时间轴**精度不一样**，
而面板上两者长得一模一样。用户看到的会是"时间戳有时准有时不准"，查不出原因。

## 时间轴的四个档位（**这个标签必须写进数据**）

设计 §4.4 还点名了一个现存的谎：*"现在这个信息只写在注释里，面板和导出看不出来"* ——
意思是代码里明明知道时间戳是估的，但**没告诉任何人**。所以这里每次拼装都返回一个
`timestamps` 档位，调用方要把它带进 `meta.json` 与面板。

| 档位 | 什么情况 | 时间精度 |
|---|---|---|
| `exact` | 后端**自己**给了句级时间轴（qwen3asr + ForcedAligner；whisper 的 segments） | 模型给的 |
| `aligned` | 有精确的**骨架**，但文本来自另一个后端，按字对齐（SenseVoice 文本 + whisper 骨架） | 句界近似、顺序与位置对 |
| `estimated` | 谁都没给时间轴，按字数在段时长内均摊 | 只保证顺序与大致位置 |
| `none` | 连文本都没有（这段没人说话） | — |

**为什么要有 `aligned` 这一档，而不是并进 `exact` 或 `estimated`：**
并进 `exact` 是**说大话**（句界是在 whisper 段内插值出来的，不是模型直接给的）；
并进 `estimated` 是**漏报**（它比"整段按字数平摊"准得多，因为骨架是逐字的）。
两边的代价都是"用户以为他知道精度，其实不知道"。多一个词换一句实话，值。

## 三条规则（按优先级，第一个能用的赢）

1. `sentences` 非空 → 直接用它（后端给的句级时间轴）→ `exact`
2. `skeleton` 与 `text` 都有 → 拿 `text` 对齐到骨架 → `aligned`
   （对齐不出句子就退回骨架自己的行 —— **宁可粗一点，也不要一段空白**）
3. `skeleton` 有、`text` 没有 → 骨架就是结果 → `exact`
4. 只有 `text` → 按字数均摊 → `estimated`
5. 都没有 → 空 → `none`

**`text` 为空不等于失败**：那是"这段没人说话"。它会走规则 5 返回空行 + `none`，
**不抛异常** —— 拿它当失败去换后端，会做出"安静片段 → 换后端 → 还是安静"这种蠢事。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

#: 句级时间轴的精度档位（见模块注释的表）
TIMESTAMPS_EXACT = "exact"
TIMESTAMPS_ALIGNED = "aligned"
TIMESTAMPS_ESTIMATED = "estimated"
TIMESTAMPS_NONE = "none"

TIMESTAMPS_KINDS = (TIMESTAMPS_EXACT, TIMESTAMPS_ALIGNED,
                    TIMESTAMPS_ESTIMATED, TIMESTAMPS_NONE)

#: `(start, end, text)` —— 与 `meeting.py` 一直用的形状一致，便于直接替换
Sentence = Tuple[float, float, str]

#: 中文（含中英混排）的句末标点。**只用这些**：逗号不断句，否则一句话会被切碎。
_SENTENCE_END = "。！？…"
_SENTENCE_END_RE = r"(?<=[。！？!?；;])"


@dataclass
class Assembled:
    """一次拼装的结果。`rule` 是**哪条规则生效了**（写日志用，排障时省一半时间）。"""
    sentences: List[Sentence] = field(default_factory=list)
    timestamps: str = TIMESTAMPS_NONE
    rule: str = ""

    def __bool__(self) -> bool:
        return bool(self.sentences)


def estimate_sentences(text: str, seg_seconds: float) -> List[Sentence]:
    """按句切分、**按字数在段时长内均摊**时间。

    整段一行会让面板的"逐句跳转"失去意义；均摊之后时间不精确，
    但**顺序与位置对** —— 而且这一次我们**明说了它是 `estimated`**（见模块注释）。
    """
    text = (text or "").strip()
    if not text:
        return []
    parts = [p for p in re.split(_SENTENCE_END_RE, text) if p and p.strip()]
    if not parts:
        parts = [text]
    total = sum(len(p) for p in parts) or 1
    out: List[Sentence] = []
    t = 0.0
    for p in parts:
        dur = max(float(seg_seconds or 0.0) * len(p) / total, 0.05)
        out.append((round(t, 2), round(t + dur, 2), p.strip()))
        t += dur
    return out


def align_sentences(text: str, skeleton: Sequence[Sentence]) -> List[Sentence]:
    """把整段文本**按字对齐**到骨架的逐字时间轴上，再按标点切句。

    这是 `meeting.py` 里原来那个 `_align_sentences`，**原样搬过来的** ——
    它已经在实机上跑过不少会议（SenseVoice 文本 + whisper 骨架的组合），
    重写一遍只会引入"看着更整齐但时间轴飘了"这种很难发现的退化。

    做法：把骨架的每句话拆成"字符 → 时间"的细表（句内按位置线性插值），
    用 `difflib` 把文本与骨架的字符序列对齐；文本里对不上的字符按前后已知点插值。
    然后按句末标点聚合。
    """
    if not skeleton or not text:
        return []
    w_chars: List[str] = []
    w_times: List[float] = []
    for st, en, txt in skeleton:
        t = (txt or "").strip()
        if not t:
            continue
        n = len(t)
        for i, ch in enumerate(t):
            w_chars.append(ch)
            w_times.append(st + (en - st) * (i + 0.5) / n)
    if not w_chars:
        return []

    src = list(text)
    sm = difflib.SequenceMatcher(None, src, w_chars, autojunk=False)
    tmap = {}
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                tmap[i1 + k] = w_times[j1 + k]

    times: List[float] = []
    last_i, last_t = -1, 0.0
    for i in range(len(src)):
        if i in tmap:
            last_i, last_t = i, tmap[i]
            times.append(tmap[i])
            continue
        nxt = None
        for j in range(i + 1, len(src)):
            if j in tmap:
                nxt = (j, tmap[j])
                break
        if nxt and last_i >= 0 and nxt[0] != last_i:
            times.append(last_t + (nxt[1] - last_t) * (i - last_i) / (nxt[0] - last_i))
        else:
            times.append(last_t)

    sentences: List[Sentence] = []
    buf: List[str] = []
    seg_start = 0.0
    for i, ch in enumerate(src):
        if not buf:
            seg_start = times[i]
        buf.append(ch)
        if ch in _SENTENCE_END:
            txt = "".join(buf).strip()
            if txt:
                sentences.append((seg_start, times[i], txt))
            buf = []
    if buf:
        txt = "".join(buf).strip()
        if txt:
            sentences.append((seg_start, times[-1], txt))
    return sentences


def assemble(text: str = "", *, sentences: Sequence[Sentence] = (),
             skeleton: Sequence[Sentence] = (), seg_seconds: float = 0.0) -> Assembled:
    """**唯一的拼装入口**（规则见模块注释）。

    参数都叫得直白：`sentences` = 后端给的句级时间轴；`skeleton` = 精确的逐句骨架
    （文本可能来自别处）；`text` = 整段文本。
    """
    # 规则 1：后端自己给了句级时间轴
    if sentences:
        rows = [(float(a), float(b), str(t).strip()) for a, b, t in sentences
                if str(t).strip()]
        if rows:
            return Assembled(rows, TIMESTAMPS_EXACT, "sentences")

    text = (text or "").strip()

    # 规则 2：有骨架也有文本 → 按字对齐
    if skeleton and text:
        rows = align_sentences(text, skeleton)
        if rows:
            return Assembled(rows, TIMESTAMPS_ALIGNED, "text+skeleton")

    # 规则 3：只有骨架 —— 骨架就是结果（粗一点，但那是真的）
    if skeleton:
        rows = [(float(a), float(b), str(t).strip()) for a, b, t in skeleton
                if str(t).strip()]
        if rows:
            return Assembled(rows, TIMESTAMPS_EXACT, "skeleton")

    # 规则 4：只有文本 → 均摊
    if text:
        rows = estimate_sentences(text, seg_seconds)
        if rows:
            return Assembled(rows, TIMESTAMPS_ESTIMATED, "text-only")

    # 规则 5：什么都没有 = 这段没人说话（**不是失败**）
    return Assembled([], TIMESTAMPS_NONE, "empty")


def describe_kind(kind: str) -> str:
    """给面板看的一句话（别让界面直接显示 `estimated` 这种单词）。"""
    return {
        TIMESTAMPS_EXACT: "精确（模型直接给出）",
        TIMESTAMPS_ALIGNED: "对齐（有精确骨架，文本按字对齐）",
        TIMESTAMPS_ESTIMATED: "估算（无时间戳，按字数均摊）",
        TIMESTAMPS_NONE: "无（这段没有内容）",
    }.get(kind, kind)
