package com.echo.contact.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.echo.contact.ui.theme.EchoPalette

/**
 * 会议正文（转写 / 纪要 / 主题）的**极简排版**：`text → List<Block>`。
 *
 * 为什么要有这一段：这三份正文以前是**整块纯文本**塞进一个 `Text`（`MutedLine` 那一档），
 * 一小时的会转写就是一坨几千字，标题、列表、分段全糊在一起 —— 手机上根本读不下去。
 *
 * **克制**是这里的第一原则：
 *  * 只认会议纪要/大模型真的会吐出来的那几样：`#`/`##`/`###` 标题、`-`/`*` 无序列表、
 *    `1.` 有序列表、`**加粗**`、`---` 水平线、空行分段、普通段落；
 *  * 行内**只处理加粗**（不做链接/代码/斜体 —— 那些语法在会议正文里出现时，
 *    多半是正文本身，硬解只会把内容改样）；
 *  * **不许吞内容**：认不出来的行一律当普通段落原样显示（`#标题` 后面没空格就是普通段落，
 *    连那个 `#` 也留着）。"少显示几个字"比"显示得不好看"严重得多。
 *
 * 为什么做成纯函数（而不是在 `@Composable` 里现拆）：`Block` 的结构是能写 JVM 用例钉住的
 * （见 `MeetingTextBlocksTest`）—— 尤其是"把所有 Block 的文本拼回来 = 原文去掉标记"这条性质，
 * 它是"一个字都不丢"的机器判据。放在 Composable 里就只能靠眼睛看。
 */
internal sealed interface Block {
    /** 这一块的纯文本（标题不含 `#`、列表不含项目符号/序号、加粗不含 `**`）。 */
    val text: String

    /** 标题：`level` = 1/2/3（`#`/`##`/`###` 的个数）。 */
    data class Heading(val level: Int, override val text: String) : Block

    /** 无序列表项（原文 `-` 或 `*`）。 */
    data class Bullet(override val text: String) : Block

    /** 有序列表项（`index` 是原文里那个序号 —— **照抄不改写**，原文写 `3.` 就显示 3）。 */
    data class Ordered(val index: Int, override val text: String) : Block

    /** 普通段落（一个或多个连续非空行；它们之间保留换行）。 */
    data class Paragraph(override val text: String) : Block

    /** `---` 水平线（没有文本，`text` 固定是空串）。 */
    data object Rule : Block {
        override val text: String = ""
    }
}

/**
 * 无序列表标记：`- ` / `* ` / `+ ` 后面**必须有空格**。
 *
 * ⚠️ 这里刻意**只认一个**符号：早期写成 `^[-*+] +` 时，`*` 会贪婪吃掉行首的 `**`，
 * 于是"行首就是加粗"的一段正文（`**甲****乙**`）被当成列表项、还被啃掉两个星号
 * （实测：`stripInlineBold` 只剩 `甲乙`，但整段不再是从行首开始，正文凭空少字）。
 * 已知边界：行首以 `**` 开头的一段**加粗正文**仍会被读成列表项（`**x` → 项目符号 +
 * `*x`）—— 会议正文里没有这种写法，而"少字"比"当列表排"严重得多，所以取这一档。
 */
private val UNORDERED = Regex("^([-*+]) +")

/** 有序列表标记：`1. ` / `2) `，数字位数不限（照原文显示，不重排）。 */
private val ORDERED = Regex("^(\\d+)[.)] +")

/** 标题标记：`# ` / `## ` / `### `（**必须有空格**，`#标题` 是普通段落不是标题）。 */
private val HEADING = Regex("^(#{1,3}) +(.*)$")

/** 水平线：三个及以上同一个符号（`-`/`*`/`_`），中间允许空格，**行里不能有别的东西**。 */
private val RULE = Regex("^([-*_])[ \\t]*(\\1[ \\t]*){2,}$")

/** 时间戳前缀：`hh:mm` 或 `hh:mm:ss`（方括号形式另算，见 [splitSpeakerPrefix]）。 */
private val CLOCK = Regex("^\\d{1,2}:\\d{2}(:\\d{2})?")

/**
 * 把一份正文拆成块。
 *
 * 口径（都与"不丢内容"配套）：
 *  * 行尾统一去 `\r`（后端给的是 Windows 换行时，行尾会多一个不可见字符）；
 *  * 空行（或只有空白）只用来分段，自己不产生 Block；
 *  * **连续的非空普通行合成一个段落**（保留它们之间的换行）—— 模型吐出的正文经常是硬换行的，
 *    一行一个段落会让段间距满地都是；
 *  * 一行里只认**行首**的标记，块级标记之外只有 `**` 会去掉（其余一律原样）。
 */
internal fun parseMeetingText(text: String): List<Block> {
    val blocks = mutableListOf<Block>()
    // 攒着连续的非空普通行，遇到块级标记或空行就收成一个段落。
    val paragraph = mutableListOf<String>()

    fun flush() {
        if (paragraph.isNotEmpty()) {
            blocks += Block.Paragraph(paragraph.joinToString("\n"))
            paragraph.clear()
        }
    }

    for (raw in text.split('\n')) {
        val line = raw.trimEnd('\r').trim()   // CRLF：`\r` 只是换行的一半，不算内容
        if (line.isEmpty()) {
            flush()                            // 空行 = 分段
            continue
        }
        if (RULE.matches(line)) {
            flush()
            blocks += Block.Rule
            continue
        }
        val head = HEADING.matchEntire(line)
        if (head != null) {
            flush()
            blocks += Block.Heading(head.groupValues[1].length, stripInlineBold(head.groupValues[2].trim()))
            continue
        }
        // 无序项用**前缀**匹配（不是 matchEntire）：`- 甲` 后面还有一整句正文。
        if (UNORDERED.containsMatchIn(line)) {
            flush()
            blocks += Block.Bullet(stripInlineBold(UNORDERED.replaceFirst(line, "").trim()))
            continue
        }
        // 有序项同理：`1. 先做 A` 的前缀是 `1. `，**不能**要求整行都是标记。
        val ord = ORDERED.find(line)
        if (ord != null) {
            flush()
            val rest = line.substring(ord.value.length).trim()
            blocks += Block.Ordered(ord.groupValues[1].toIntOrNull() ?: 1, stripInlineBold(rest))
            continue
        }
        // 普通段落：也要去行内标记（早先这里漏了一次 —— 段落里的 `**` 就这么留下来了，
        // 而标题/列表那三条路都去了，于是"拼回来 = 原文去标记"只在这一种块上对不上）。
        paragraph += stripInlineBold(line)
    }
    flush()
    return blocks
}

/**
 * 找出**成对** `**...**` 的内容区间（左闭右开），供加粗渲染与去标记共用一套边界判定。
 *
 * 为什么手写扫描而不是正则替换：`**a****b**` 这种连着写的时候，非贪婪正则会把
 * `a**` 当成一对；而正确读法是"两段加粗挨着"（`**a**` 与 `**b**`）。
 * 扫描器只认"下一个 `**`"当下一个边界，遇到不成对的就把那两个 `*` 当普通字。
 */
internal fun boldSpans(text: String): List<Pair<Int, Int>> {
    val spans = mutableListOf<Pair<Int, Int>>()
    var i = 0
    while (i < text.length) {
        val open = text.indexOf("**", i)
        if (open < 0) break
        val close = text.indexOf("**", open + 2)
        if (close < 0) break                    // 没闭合：后面的 `**` 就是普通字
        if (close == open + 2) {                // `****` 空加粗：不当标记（否则会吃掉星号）
            i = close + 2
            continue
        }
        spans += (open + 2) to close
        i = close + 2
    }
    return spans
}

/**
 * 去掉文本里**成对**的 `**` 标记（其余字符一个不动）。
 *
 * 为什么 Block 里存的是**去标记**的文本：`Block.text` 是"这一段到底写了什么"，
 * 排版标记不该混在里面 —— 否则"把所有 Block 的文本拼回来 = 原文去掉标记"这条
 * 不丢字判据就没法写，渲染时还得再防一次重复的 `**`。
 *
 * 为什么用游标走一遍而不是 `joinToString(...) { substring }`：这里的拼法是
 * "标记之间的原样 + 去掉星号的内容"，不是"把每段区间逐个拼起来" ——
 * 后者会把**标记之外的正文整段丢掉**（本轮踩过：`x **y** z` 只剩 `y`）。
 */
internal fun stripInlineBold(text: String): String {
    val out = StringBuilder()
    var cursor = 0
    for ((start, end) in boldSpans(text)) {
        out.append(text, cursor, start - 2)   // 上一段正文（含它自己的字符，一个不丢）
        out.append(text, start, end)          // 加粗的内容（那对 `**` 就是被去掉的"标记"）
        cursor = end + 2
    }
    out.append(text, cursor, text.length)     // 尾巴
    return out.toString()
}

/**
 * 行内加粗渲染：`**x**` → 加粗的 `x`，其余字符（包括不配对的 `*`）**原样**。
 *
 * 边界判定与 [stripInlineBold] 共用 [boldSpans]，所以"显示出来的字"与
 * "Block.text 里的字"永远一致（不会一个去掉标记、一个留着）。
 */
internal fun inlineBold(text: String): AnnotatedString = buildAnnotatedString {
    var cursor = 0
    for ((start, end) in boldSpans(text)) {
        append(text.substring(cursor, start))   // 上一段原样
        withStyle(SpanStyle(fontWeight = FontWeight.Bold)) { append(text.substring(start, end)) }
        cursor = end
    }
    append(text.substring(cursor))              // 尾巴
}

/** 说话人/时间前缀的切分结果：`prefix` 是"谁/什么时候"，`body` 是他说的内容。 */
internal data class SpeakerPrefix(val prefix: String, val body: String)

/**
 * 认识转写行开头的"说话人/时间"前缀：`[00:12:34] 张三：……`、`00:12:34 张三：……`、
 * `说话人 1：……`、`张三:……`。
 *
 * 为什么要求前缀**短**（≤40 字）：这是"读起来像对话"的**排版判断**，不是解析。
 * 光看字符串分不出"他说："和"张三："，所以这里只做保守的事 —— 前缀太长的、
 * 冒号后没内容的，一律返回 null，调用方按普通正文渲染，**一个字都不动**。
 * 代价（已知边界）：`他说：明天开会` 这种短前缀也会被弱化前两个字符 —— 只影响长相，
 * 因为前缀与内容拼起来永远等于原文（用例钉着）。
 */
internal fun splitSpeakerPrefix(text: String): SpeakerPrefix? {
    val s = text.trimEnd('\r')
    val head = s.trimStart()
    val lead = s.length - head.length
    if (head.isEmpty()) return null

    var i = 0
    // 可选的时间戳：`[..]` 或裸的 `hh:mm(:ss)`（转写行几乎都带，但不强制）
    if (head.startsWith("[")) {
        val end = head.indexOf(']')
        if (end <= 1) return null
        i = end + 1
    } else {
        val m = CLOCK.find(head)
        if (m != null) i = m.value.length
    }
    while (i < head.length && head[i] == ' ') i++

    var j = i
    while (j < head.length && head[j] != '：' && head[j] != ':') j++
    if (j >= head.length) return null          // 整行没有冒号：不是对话行
    val speaker = head.substring(i, j)
    if (speaker.isEmpty() || speaker.length > 40) return null
    val body = head.substring(j + 1)
    if (body.isBlank()) return null            // 冒号后没内容：当普通正文，别渲染成空对话
    return SpeakerPrefix(
        prefix = s.substring(0, lead + j + 1), // 连行首缩进一起留着（不丢字符）
        body = body,
    )
}

/**
 * 把块列表渲染成一列文字（标题不同字号、列表带符号/序号、段落给行距与段间距）。
 *
 * 为什么用 `Column` 而不是 `LazyColumn`：它总是被放进外面那层 `LazyColumn` 的 `item` 里，
 * **嵌套可滚动容器会直接抛异常**。块数就是一段正文的行数（几十到几百），一次量出来没问题。
 * 需要"一个块一条 item"的懒加载（正文可能几千行）就用 [MeetingBlock] 自己排。
 */
@Composable
internal fun MeetingBlocks(
    blocks: List<Block>,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        for (b in blocks) MeetingBlock(b)
    }
}

/** 单个块的长相（[MeetingBlocks] 与"一个块一条 item"的懒加载列表都走它，长相只有一处定义）。 */
@Composable
internal fun MeetingBlock(block: Block) {
    when (block) {
        is Block.Heading -> Text(
            inlineBold(block.text),
            style = when (block.level) {
                1 -> MaterialTheme.typography.titleLarge
                2 -> MaterialTheme.typography.titleMedium
                else -> MaterialTheme.typography.titleSmall
            },
            color = EchoPalette.Text,
        )

        is Block.Bullet -> Row(verticalAlignment = Alignment.Top) {
            Text("•", style = MaterialTheme.typography.bodySmall, color = EchoPalette.Muted)
            Spacer(Modifier.width(6.dp))
            Text(inlineBold(block.text), style = MaterialTheme.typography.bodySmall, color = EchoPalette.Text)
        }

        is Block.Ordered -> Row(verticalAlignment = Alignment.Top) {
            // 序号照原文显示（原文写 `3.` 就显示 3）—— 重排会让"第几步"对不上。
            Text("${block.index}.", style = MaterialTheme.typography.bodySmall, color = EchoPalette.Muted)
            Spacer(Modifier.width(6.dp))
            Text(inlineBold(block.text), style = MaterialTheme.typography.bodySmall, color = EchoPalette.Text)
        }

        is Block.Paragraph -> Text(
            inlineBold(block.text),
            style = MaterialTheme.typography.bodySmall.copy(
                lineHeight = MaterialTheme.typography.bodySmall.fontSize * 1.6f,
            ),
            color = EchoPalette.Text,
        )

        Block.Rule -> Spacer(
            Modifier.fillMaxWidth().height(1.dp).background(EchoPalette.Border),
        )
    }
}

/**
 * 一行转写（`[00:12:34] 张三：……`）的长相：**前缀弱化、内容主体加重**。
 *
 * 为什么单独一个渲染入口：转写行是**对话**，一眼该抓住的是"说了什么"，
 * 说话人和时间只是坐标。但**一个字都不许改** —— 所以前缀与内容来自同一个
 * `AnnotatedString` 的两段（拼起来就是原文），只是前一段更淡更小、后一段更实。
 *
 * 认不出前缀（普通句子里的冒号）就整行当正文，不猜 —— 猜错会把别人的话算到某个人头上。
 */
@Composable
internal fun DialogueLine(text: String, modifier: Modifier = Modifier) {
    val split = splitSpeakerPrefix(text)
    val annotated: AnnotatedString = if (split == null) {
        inlineBold(text)
    } else {
        buildAnnotatedString {
            withStyle(
                SpanStyle(
                    color = EchoPalette.Muted,
                    fontSize = MaterialTheme.typography.labelSmall.fontSize,
                    letterSpacing = 0.5.sp,
                ),
            ) { append(split.prefix) }
            withStyle(SpanStyle(color = EchoPalette.Text, fontWeight = FontWeight.Medium)) {
                append(inlineBold(split.body))
            }
        }
    }
    Text(
        annotated,
        modifier = modifier.fillMaxWidth(),
        style = MaterialTheme.typography.bodySmall.copy(
            lineHeight = MaterialTheme.typography.bodySmall.fontSize * 1.6f,
        ),
    )
}
