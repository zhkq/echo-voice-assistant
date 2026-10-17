package com.echo.contact.service

import com.echo.contact.data.dto.CommandItem
import com.google.gson.Gson
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 「这条答复该念哪一段」（`SpeakPlan`，纯函数）—— 用户 2026-10-17 真机问题的**主判据**。
 *
 * 现场原话：*"手机端朗读了全部内容，包括详情，应该跟电脑测试一样，只朗读前面的摘要"*。
 * 所以这一组用例钉的是四件事，一件都不能松：
 *  1. 电脑给了 `speakText` → 念的**恰好**是它（正文里躺着再长的详情也不许念出来）；
 *  2. 老电脑没给 → 只念**开头那段摘要**（多段长文里"详情"那几句一句都不许出现）；
 *  3. 空 / 空白 / 非字符串 / 脏值 → 安全退回，**不许崩**、也不许把那串脏值念出来；
 *  4. `origin=pc` → 一个字都不念；`mobile` / 拿不到 → 照常念（拿不到就不拦，与通知那条相反）。
 *
 * 为什么要在 JVM 里钉：这条判据错了的后果**在车里才听得出来**（念了五分钟详情），
 * 而它又完全是个纯函数 —— 没有理由把它留到真机上试。
 */
class SpeakPlanTest {

    private val gson = Gson()

    /** 一句"结论"（该念的）。 */
    private val conclusion = "三件事都办好了：灯已开、空调调到二十六度、明天的会改到十点半。"

    /** 一篇"结论 + 详情如下"的回复（详情部分的关键词都不该出现在朗读文本里）。 */
    private val withDetailMarker = """$conclusion
详情如下：
1. 客厅灯：已经打开，亮度 60%；
2. 空调：制冷二十六度，风速自动；
3. 会议：改到十点半，参会人已收到通知。"""

    /** 多段长文（没有"详情如下"标记，只有空行分段的旧格式）。 */
    private val multiParagraph = """会议纪要已经整理好了，一共三段。
 
第一段讲进度：三个模块有两个完成。
第二段讲风险：接口还没对接完。
第三段讲人：下周开始轮值。"""

    /** "详情"那几句的关键词 —— 任何一条出现在朗读文本里就算失败。 */
    private val detailWords = listOf("亮度", "风速自动", "参会人", "讲进度", "讲风险", "讲人")

    private fun assertNoDetail(spoken: String) {
        for (w in detailWords) {
            assertFalse("详情是给人看的，不许念出来（出现了「$w」）：$spoken", spoken.contains(w))
        }
    }

    // ------------------------------------------------------------ ① 有 speakText 就念它

    @Test
    fun `有 speakText 就恰好念它、正文里的超长详情一句都不念`() {
        val item = CommandItem(
            id = 1, status = "done", source = "mobile",
            speakText = conclusion,
            // 老电脑留下的 brief 与整篇正文**同时**存在：这一条要证明手机上不再"自己切"
            brief = "（老简报，不该被念）",
            reply = withDetailMarker,
        )
        val spoken = SpeakPlan.decide(item)
        assertEquals("有 speakText 就必须原样念它（唯一出处）", conclusion, spoken)
        assertNoDetail(spoken!!)
        assertFalse("speakText 存在时绝不能退回 brief：$spoken", spoken.contains("老简报"))
    }

    // ------------------------------------------------------------ ② 没有就退回"取摘要"

    @Test
    fun `没有 speakText 时只念第一段摘要`() {
        val item = CommandItem(id = 2, status = "done", source = "mobile", reply = multiParagraph)
        val spoken = SpeakPlan.decide(item)
        assertEquals("只念第一个段落（空行之前）", "会议纪要已经整理好了，一共三段。", spoken)
        assertNoDetail(spoken!!)
    }

    @Test
    fun `详情标记之后的内容一句都不念`() {
        val spoken = SpeakPlan.summaryOf(withDetailMarker)
        assertEquals(conclusion, spoken)
        assertNoDetail(spoken)
    }

    @Test
    fun `小标题之后的列表是后文、不念`() {
        val reply = "这一段已经处理完了。\n## 详情\n- 第一条：改了 A\n- 第二条：改了 B"
        val spoken = SpeakPlan.summaryOf(reply)
        assertEquals("这一段已经处理完了。", spoken)
        for (w in listOf("第一条", "改了 A", "改了 B")) {
            assertFalse("小标题之后的列表属于后文：$spoken", spoken.contains(w))
        }
    }

    @Test
    fun `代码块是给眼睛看的、不念`() {
        val reply = "先跑了一下脚本。\n```\npython x.py --all\n```\n结果正常。"
        val spoken = SpeakPlan.summaryOf(reply)
        assertEquals("先跑了一下脚本。", spoken)
        assertFalse("代码块念出来是一串符号：$spoken", spoken.contains("python"))
    }

    @Test
    fun `老格式的单行长文也不许整篇念出来`() {
        // 没有空行、没有"详情如下"、没有小标题：整篇就是一个自然段。
        // 只按"第一段"切会把它整个念出来 —— 这正是用户碰到的那个毛病，所以必须有长度上限。
        val tail = "详情部分是：第一件事改了接口。"
        val long = "结论：" + "这句话在解释背景".repeat(30) + tail
        val spoken = SpeakPlan.summaryOf(long)
        assertTrue("不能是空", spoken.isNotEmpty())
        assertTrue(
            "超过 ${SpeakPlan.MAX_SUMMARY_CHARS} 字必须截断：${spoken.length} 字",
            spoken.length <= SpeakPlan.MAX_SUMMARY_CHARS + 2,
        )
        assertFalse("截断之后不许把后文念出来：$spoken", spoken.contains("第一件事"))
    }

    @Test
    fun `brief 与整篇同时存在时念 brief、同样不含详情`() {
        // 老电脑本来就会给 brief（电脑侧早就取好了结论段）——比从整篇里再切一次更准。
        val item = CommandItem(id = 3, status = "done", reply = withDetailMarker, brief = conclusion)
        assertEquals(conclusion, SpeakPlan.decide(item))
    }

    // ------------------------------------------------------------ ③ 空/脏值安全退回、别崩

    @Test
    fun `speakText 是空串空白或非字符串时安全退回摘要`() {
        val junk = listOf<Any?>(null, "", "   ", "\n\t ", 42, 4.2, true, mapOf("x" to 1), listOf(1, 2))
        for (v in junk) {
            val item = CommandItem(id = 4, status = "done", speakText = v, reply = "第一段。\n\n第二段详情。")
            assertEquals("speakText=$v 该当成没给、退回第一段", "第一段。", SpeakPlan.decide(item))
        }
    }

    @Test
    fun `线上 JSON 里的怪 speakText 不许把整条记录弄崩`() {
        // Gson 对 `String` 字段遇到对象会直接抛（等于一个脏字段把整页历史弄崩），
        // 所以 DTO 用 `Any?` 接住，判据只认字符串。
        for (junk in listOf("42", "4.2", "true", """{"weird":1}""", "[1,2]", "null", "\"   \"")) {
            val parsed = gson.fromJson(
                """{"id":5,"status":"done","reply":"第一段。\n\n详情在后","speakText":$junk}""",
                CommandItem::class.java,
            )
            assertEquals("speakText=$junk 必须安全退回摘要", "第一段。", SpeakPlan.decide(parsed))
        }
        // 正常字符串（含 snake_case 写法）照样认得
        assertEquals(
            "念这句。",
            SpeakPlan.decide(
                gson.fromJson("""{"id":6,"status":"done","speakText":"念这句。"}""", CommandItem::class.java)
            ),
        )
        assertEquals(
            "念这句。",
            SpeakPlan.decide(
                gson.fromJson("""{"id":7,"status":"done","speak_text":"念这句。"}""", CommandItem::class.java)
            ),
        )
    }

    @Test
    fun `没有可念的文字时不念、绝不念空`() {
        val blanks = listOf<CommandItem>(
            CommandItem(id = 8, status = "done"),
            CommandItem(id = 9, status = "done", reply = ""),
            CommandItem(id = 10, status = "done", reply = "   ", brief = "\n\t", speakText = "  "),
            // 整篇以"详情如下"开头 = 只有详情，没有结论可念
            CommandItem(id = 11, status = "done", reply = "详情如下：\n1. 一堆细节"),
        )
        for (item in blanks) {
            assertNull("没有摘要就一个字都不念（别念空）：${item.id}", SpeakPlan.decide(item))
            assertTrue("summaryOf 对空文本要给空串，不能给 null 崩调用方", SpeakPlan.summaryOf("").isEmpty())
        }
    }

    // ------------------------------------------------------------ ④ 谁发起谁念

    @Test
    fun `电脑发起的答复一个字都不念`() {
        // origin 明确写 pc（大小写/空白都算）
        for (o in listOf("pc", "PC", " pc ")) {
            assertNull(
                "电脑发起的任务不该在手机喇叭上念（origin=$o）",
                SpeakPlan.decide(CommandItem(status = "done", origin = o, reply = conclusion)),
            )
        }
        // 老数据没有 origin：看 source（与电脑侧 `_command_origin` 同口径：只有 mobile 算手机）
        for (s in listOf("web", "desktop", "hotkey", "api", "pc")) {
            assertNull(
                "source=$s 算电脑发起，不该念",
                SpeakPlan.decide(CommandItem(status = "done", source = s, reply = conclusion)),
            )
        }
    }

    @Test
    fun `手机发起或拿不到发起方都正常念`() {
        assertEquals(
            conclusion,
            SpeakPlan.decide(CommandItem(status = "done", origin = "mobile", reply = conclusion)),
        )
        // 老电脑只给 source
        assertEquals(
            conclusion,
            SpeakPlan.decide(CommandItem(status = "done", source = "mobile", reply = conclusion)),
        )
        // 两个都没有 = 拿不到 → **不拦**（该念却静默是功能坏掉，与通知那条的取舍相反）
        assertEquals(
            conclusion,
            SpeakPlan.decide(CommandItem(status = "done", reply = conclusion)),
        )
        // origin 认不出的取值也**不拦**（只有明确的 pc 才拦）
        assertEquals(
            conclusion,
            SpeakPlan.decide(CommandItem(status = "done", origin = "laptop", reply = conclusion)),
        )
    }

    @Test
    fun `电脑发起的指令也要说清为什么不念`() {
        val why = SpeakPlan.silentReason(CommandItem(status = "done", origin = "pc", reply = conclusion))
        assertTrue("得说清是谁发起的：$why", why.contains("电脑"))
        val other = SpeakPlan.silentReason(CommandItem(status = "done", origin = "mobile"))
        assertFalse("手机发起却没念，原因是'没有摘要'而不是'电脑的任务'：$other", other.contains("电脑"))
    }
}
