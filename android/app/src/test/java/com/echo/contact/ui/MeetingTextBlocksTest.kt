package com.echo.contact.ui

import androidx.compose.ui.text.font.FontWeight
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 会议正文排版的**纯函数**判据（B7：会议详情的阅读排版）。
 *
 * 为什么这些值得写成用例：这段代码的两种坏法都很安静 ——
 *  * **丢字**：解析时把认不出来的东西吃掉（`#标题` 后面没空格就没了、或者行首的 `**`
 *    被当成项目符号啃掉两个 `*`）—— 界面上少的是用户的原话，没人会发现；
 *  * **抛异常**：`toInt()` 之类的顺手写法遇到怪输入就崩，而会议正文是**模型/后端给的**，
 *    什么样的怪行都可能冒出来。
 * 所以这里既钉"语法认对了"，也钉"拼回来等于原文"和"什么输入都不许崩"。
 *
 * 两个函数的分工（本轮踩过一次，写清楚）：
 *  * [stripInlineBold] = 把 `**` **去掉**（`Block.text` 用的是它 —— 存"写了什么"）；
 *  * [inlineBold] = 把 `**` **变成加粗样式**，正文一字不改（渲染用的是它）。
 * 所以 `inlineBold(x).text == x` 是**对的**，不是 bug。
 */
class MeetingTextBlocksTest {

    /**
     * 「不丢字」的参照实现：把行首标记与 `**` 拿掉、去掉空行与水平线，其余**一个字都不许动**。
     * 它刻意写得比解析器"笨"（纯字符串处理），两边独立，不会一起错。
     * 用 `[0-9]` 而不是 `\d`：两种字符串的转义规则不一样，别踩那个坑。
     *
     * 为什么水平线也要在这里过滤掉：`---` 是**没有文本**的块（解析器给的是 `Block.Rule`，
     * 拼文本时天然不出现）。参照实现要是把它当普通行留着，两边就会差一行"---" ——
     * 而那不是丢字，是两边对"水平线有没有文本"的口径没对齐（本轮真被它绊了一次）。
     */
    private fun stripped(text: String): String =
        text.replace("\r\n", "\n").replace("\r", "\n")
            .split('\n')
            .map { it.trim() }
            .filter { it.isNotEmpty() }
            .filterNot { Regex("^([-*_])[ \\t]*(\\1[ \\t]*){2,}$").matches(it) }  // 水平线不算正文
            .map { line ->
                line
                    .replace(Regex("^#{1,3} +"), "")
                    .replace(Regex("^[-*+] +"), "")
                    .replace(Regex("^[0-9]+[.)] +"), "")
                    .replace("**", "")
                    .trim()
            }
            .filter { it.isNotEmpty() }
            .joinToString("\n")

    /** 所有 Block 的文本按行拼回来（一个 Block 的文本当一行；段落内部的换行保留）。 */
    private fun joined(blocks: List<Block>): String =
        blocks.filter { it !is Block.Rule }.joinToString("\n") { it.text }

    @Test
    fun `三级标题各认各的`() {
        val blocks = parseMeetingText("# 一级\n## 二级\n### 三级")
        assertEquals("三行该出三个标题块：$blocks", 3, blocks.size)
        assertEquals("`#` 后面跟一个空格才是标题", Block.Heading(1, "一级"), blocks[0])
        assertEquals(Block.Heading(2, "二级"), blocks[1])
        assertEquals(Block.Heading(3, "三级"), blocks[2])
    }

    @Test
    fun `无序列表两种符号都认`() {
        val blocks = parseMeetingText("- 甲\n* 乙\n+ 丙")
        assertEquals(listOf("甲", "乙", "丙"), blocks.map { it.text })
        assertTrue("都该是无序列表项：$blocks", blocks.all { it is Block.Bullet })
        // 项目符号后面**必须有空格**：`-甲` 是正文，不是列表（否则连字符会凭空消失）
        assertEquals("-甲", parseMeetingText("-甲")[0].text)
        assertTrue(parseMeetingText("-甲")[0] is Block.Paragraph)
    }

    @Test
    fun `有序列表照抄原文序号不重排`() {
        // 序号是**原文的一部分**（"第 3 步"）。从 1 重排会让它和正文里提到的步骤对不上。
        val blocks = parseMeetingText("1. 第一步\n2) 第二步\n17. 第十七步")
        assertEquals(
            "序号要照原文：$blocks",
            listOf(1, 2, 17),
            blocks.map { (it as Block.Ordered).index },
        )
        assertEquals(listOf("第一步", "第二步", "第十七步"), blocks.map { it.text })
    }

    @Test
    fun `块里的文本已经把加粗标记去掉了`() {
        // 这条为什么单独钉：`Block.text` 的语义是"这一段到底写了什么"。
        // 如果它把 `**` 留着，一来渲染要重复处理标记，二来"拼回来 = 原文去标记"
        // 那条不丢字判据就永远对不上（本轮真踩过：看着像渲染坏了，其实是解析没去标记）。
        assertEquals("预算 -20% 的事", parseMeetingText("- 预算 **-20%** 的事")[0].text)
        assertEquals("参会：张三、李四", parseMeetingText("参会：**张三**、李四")[0].text)
        assertEquals("加粗的标题", parseMeetingText("## **加粗的**标题")[0].text)
        // 去标记只去**成对**的：不配对的星号是正文，必须留着
        assertEquals("** 没配对", stripInlineBold("** 没配对"))
        assertEquals("***三颗", stripInlineBold("***三颗"))
        // 两段加粗挨着：这是正则最容易读错的那种（会把 `a**` 当一对）
        assertEquals("甲乙", stripInlineBold("**甲****乙**"))
        // 标记之外的字一个不许动（前后文都在）
        assertEquals("x y z", stripInlineBold("x **y** z"))
        // 行首就是加粗的一段：**不许**被当成项目符号啃掉两个星号（本轮真踩过）
        assertEquals("甲乙", parseMeetingText("**甲****乙**")[0].text)
        assertTrue(
            "行首加粗的段落还是段落，不是列表项：${parseMeetingText("**甲****乙**")}",
            parseMeetingText("**甲****乙**")[0] is Block.Paragraph,
        )
    }

    @Test
    fun `加粗渲染不改编排标记之外的正文`() {
        // 加粗渲染的契约：**字一个不少**（`**` 还在，只是变成样式），并且只给标记里面那段上粗。
        val s = inlineBold("这是 **重点** 和普通字")
        assertEquals("加粗不改字：$s", "这是 **重点** 和普通字", s.text)
        val spans = s.spanStyles
        assertEquals("只该有一段加粗：$spans", 1, spans.size)
        assertEquals("加粗范围要正好盖住「重点」：$spans", "重点", s.text.substring(spans[0].start, spans[0].end))
        assertEquals("加粗就是 Bold：$spans", FontWeight.Bold, spans[0].item.fontWeight)
        // 没标记的文本：原样、且不带任何 span
        assertEquals("没标记就原样", "普通字", inlineBold("普通字").text)
        assertTrue("没标记不该有 span", inlineBold("普通字").spanStyles.isEmpty())
    }

    @Test
    fun `挨着的两段加粗不该被读成一整段`() {
        // `**a****b**` 的常见误读是把 `a**` 当一对 → 结果是两段里只粗一段、或者多留两个星号。
        val s = inlineBold("**甲****乙**")
        assertEquals("两个星号是标记，不是内容：$s", "**甲****乙**", s.text)
        assertEquals("该是两段加粗：${s.spanStyles}", 2, s.spanStyles.size)
        assertEquals("第一段是「甲」", "甲", s.text.substring(s.spanStyles[0].start, s.spanStyles[0].end))
        assertEquals("第二段是「乙」", "乙", s.text.substring(s.spanStyles[1].start, s.spanStyles[1].end))
    }

    @Test
    fun `水平线认三个杠`() {
        val blocks = parseMeetingText("甲\n\n---\n\n乙")
        assertEquals("水平线是独立的一块：$blocks", Block.Rule, blocks[1])
        assertEquals("它不该带文本", "", blocks[1].text)
        // 更多横杠、带空格的都认（统一渲染成一条线）
        assertEquals(Block.Rule, parseMeetingText("----")[0])
        assertEquals(Block.Rule, parseMeetingText("- - -")[0])
        // 光秃秃的 `--` 不是线，是正文（两横可能是分隔符，也可能就是原文）
        assertTrue("`--` 不算水平线：${parseMeetingText("--")}", parseMeetingText("--")[0] is Block.Paragraph)
    }

    @Test
    fun `空行分段而连续行合成一段`() {
        val blocks = parseMeetingText("第一段第一行\n第一段第二行\n\n第二段")
        assertEquals("空行分两段：$blocks", 2, blocks.size)
        assertEquals("连续行之间保留换行（不丢换行也不多分段）", "第一段第一行\n第一段第二行", blocks[0].text)
        assertEquals("第二段", blocks[1].text)
    }

    @Test
    fun `标记后面的正文一字不丢`() {
        // 这条是"不丢字"的机器判据：混合文本里标题/列表/加粗/水平线/空行都有。
        val text = """
            # 周会纪要

            参会：**张三**、李四

            ## 议题

            - 预算 **-20%** 的事
            * 另一件
            
            1. 先做 A
            2. 再做 B

            ---

            结论：按 **A** 走。后面这句不分段，接着上面。
            第二行。
        """.trimIndent()
        val blocks = parseMeetingText(text)
        // 先卡一下参照实现本身：它要是写错了，下面那条红会指向解析器，让人去修一个没坏的东西。
        // 这一行保证"红的地方就是坏的地方"。
        assertEquals("参照实现自检（去标记 + 去空行）", "甲\n乙\n丙", stripped("# 甲\n\n- 乙\n\n---\n\n1. 丙\n"))
        assertEquals("拼回来的正文必须等于原文去掉标记后的内容", stripped(text), joined(blocks))
        assertTrue("解析出来不该是空的（否则这条判据等于没测）", blocks.size >= 7)
    }

    @Test
    fun `空串与只有空白不抛异常也没有块`() {
        for (t in listOf("", " ", "\n", "\n\n", " \t \r\n  \n")) {
            assertEquals("空白文本不该产出块：[${t.replace("\n", "\\n")}]", emptyList<Block>(), parseMeetingText(t))
        }
    }

    @Test
    fun `超长单行不抛异常且原样成段`() {
        val long = "会".repeat(200_000)
        val blocks = parseMeetingText(long)
        assertEquals("超长行还是一段", 1, blocks.size)
        assertEquals("长度不许被截断", long.length, blocks[0].text.length)
        // 加粗扫描器也得撑得住（它按 `**` 找边界，长行里可能一个都没有）
        assertEquals(long.length, inlineBold(long).text.length)
        assertEquals(long, stripInlineBold(long))
    }

    @Test
    fun `没闭合的星号照原样显示不吞字`() {
        val text = "这里有 **两个星号但没闭合，后面还有字"
        val blocks = parseMeetingText(text)
        assertEquals("没闭合就是普通段落", text, blocks[0].text)
        val s = inlineBold(blocks[0].text)
        assertEquals("`**` 是原文的一部分，不许吃掉：$s", text, s.text)
        assertTrue("不许凭空加粗：${s.spanStyles}", s.spanStyles.isEmpty())
        // 星号数量为奇数那种（`***`）也一样：不配对就不当标记
        val odd = inlineBold("***三个")
        assertEquals("不配对就原样：$odd", "***三个", odd.text)
        assertTrue("不配对不该有 span", odd.spanStyles.isEmpty())
    }

    @Test
    fun `井号后面没空格就是普通段落且井号保留`() {
        val blocks = parseMeetingText("#标题没空格")
        assertTrue("不是标题：$blocks", blocks[0] is Block.Paragraph)
        assertEquals("那个 `#` 是正文的一部分，必须留着", "#标题没空格", blocks[0].text)
        // 只有井号、连字都没有，同样当段落（不能变成空标题把 `#` 弄丢）
        assertEquals("#", parseMeetingText("#")[0].text)
        assertEquals("##", parseMeetingText("##")[0].text)
    }

    @Test
    fun `CRLF 换行不该在行尾留下看不见的字符`() {
        val text = "第一行\r\n\r\n# 标题\r\n- 甲\r\n"
        val blocks = parseMeetingText(text)
        assertEquals("该是 段落 / 标题 / 列表：$blocks", 3, blocks.size)
        assertEquals("第一行", blocks[0].text)
        assertEquals(Block.Heading(1, "标题"), blocks[1])
        assertTrue("行尾不许带 \\r：${blocks[2]}", !blocks[2].text.contains('\r'))
        assertEquals("甲", blocks[2].text)
    }

    @Test
    fun `说话人时间前缀能拆成型`() {
        // 这是"读起来像对话"的前提：前缀（谁/什么时候）与内容分开，才好弱化前者、加重后者。
        val a = splitSpeakerPrefix("[00:12:34] 张三：预算砍两成")
        assertEquals("前缀要含时间戳、说话人和冒号", "[00:12:34] 张三：", a?.prefix)
        assertEquals("冒号后就是内容（一个字不动）", "预算砍两成", a?.body)

        val b = splitSpeakerPrefix("说话人 1：第二段")
        assertEquals("说话人 1：", b?.prefix)
        assertEquals("第二段", b?.body)

        val c = splitSpeakerPrefix("00:12 李四:用半角冒号也认")
        assertEquals("00:12 李四:", c?.prefix)
        assertEquals("用半角冒号也认", c?.body)
    }

    @Test
    fun `前缀拆出来的两段拼回去等于原文`() {
        // 拆前缀只是排版，**不许改字**：前缀 + 内容必须跟原文一模一样。
        for (raw in listOf(
            "[00:12:34] 张三：预算砍两成",
            "说话人 1：第二段",
            "00:12 李四:半角冒号",
            "  王五：前面有两个空格",
        )) {
            val sp = splitSpeakerPrefix(raw)
            assertTrue("该认出前缀：[$raw]", sp != null)
            assertEquals("拼回去要一字不差：[$raw]", raw, sp!!.prefix + sp.body)
        }
    }

    @Test
    fun `正文里本来就有冒号不猜成说话人`() {
        // 说话人判定是**排版判断**，纯字符串没法百分之百分得开，所以这里钉的是"拿不准就别拆"：
        // 没有冒号、冒号后没内容、前缀长到不像名字 —— 都当普通正文。
        assertNull("空行/空白", splitSpeakerPrefix(""))
        assertNull("只有空白", splitSpeakerPrefix("   "))
        assertNull("冒号后没内容（拆出来是个空对话，不如不拆）", splitSpeakerPrefix("张三："))
        assertNull("没有冒号", splitSpeakerPrefix("这是普通的一句话"))
        // 前缀长到 40 字以上就不是"谁说的"，是正文里的冒号
        val long = "这是一句很长很长的正文".repeat(10) + "：后面还有"
        assertNull("长前缀不算说话人：$long", splitSpeakerPrefix(long))
        // 认不出的行，文字必须原样留在块里（调用方按普通正文渲染）
        assertEquals("这是普通的一句话", parseMeetingText("这是普通的一句话")[0].text)
    }

    @Test
    fun `短冒号前缀会被当成说话人这是有意的取舍`() {
        // 如实钉住这条边界：**光看字符串分不出**"他说："和"张三："，所以 40 字以内的短前缀
        // 一律按说话人排（弱化那几个字）。代价仅限于排版 —— 拼回去仍是原文，一个字没动。
        val sp = splitSpeakerPrefix("他说：明天开会")
        assertEquals("他说：", sp?.prefix)
        assertEquals("明天开会", sp?.body)
        assertEquals("拆了也不许改字", "他说：明天开会", sp!!.prefix + sp.body)
    }
}
