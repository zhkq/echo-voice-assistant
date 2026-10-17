package com.echo.contact.service

import com.echo.contact.data.dto.CommandItem

/**
 * 「这条答复**要不要念**、**念哪一段**」—— 手机侧念回复的**唯一**判据（纯函数，JVM 可测）。
 *
 * ## 它修的是什么（用户 2026-10-17 真机原话）
 *
 * > *"手机端朗读了全部内容，包括详情，应该跟电脑测试一样，只朗读前面的摘要"*
 *
 * **详情是给人看的，不是给耳朵听的。** 一段几十行的详情念出来要好几分钟：人在车里/路上
 * 既抓不住重点，也没法像看屏幕那样跳着读、跳回去重读；而"眼睛"那条路本来就把整篇摆在
 * 卡片上了，念它只是把同样的话再占一遍耳朵。所以念这条路上**只走摘要**。
 *
 * ## 单一出处（这一条最容易被"顺手"破坏）
 *
 * 电脑侧现在统一产出"要念的那段话"，放在指令结果的 `speakText` 里 —— 它只含摘要，
 * 而且与电脑本机念的**是同一段文字**。有它就**原样念它**：
 * 手机侧**不再自己切一遍正文**（两处各自切，迟早切出两个不一样的结果：用户听到的
 * 跟电脑听到的不是同一句；而且改哪一边都只改对一半）。
 *
 * 只有老电脑（还没有 `speakText`）才退回 [summaryOf] 自己取摘要 —— 这是**兼容退路**，
 * 不是主路径（见那边的规则）。
 *
 * ## 谁发起谁念
 *
 * 电脑发起的任务**不在手机喇叭上念**（`origin == "pc"` → 一个字都不念）。判据优先看
 * `origin`；老数据没有它就看 `source`（与电脑侧 `app/assistant.py::_command_origin`
 * **同口径**：只有 `mobile` 算手机发起，`web`/`desktop`/`hotkey`/`api`… 全算电脑）。
 * **拿不到就不拦** —— 字段没带上不该变成"手机一声不吭"。
 *
 * ⚠️ 这与 `EventNotify.initiator` 的兜底方向**故意相反**，别"统一"掉：
 *  * 通知那条：拿不到就当电脑 → 少打扰（多发一条是打扰用户，代价不对称）；
 *  * 念这条：拿不到就当手机 → 照念（该念却静默 = 功能坏了，用户得自己回面板才发现）。
 */
object SpeakPlan {

    /**
     * 摘要段最多念多少字（约 40 秒）。
     *
     * 为什么要有这道上限：老格式的回复**可能整篇就是一个自然段**（没有空行、没有
     * "详情如下"标记）—— 只按"第一段"切就会把整篇念出来，正是用户碰到的那个毛病。
     * 超过就截断（见 [summaryOf]）；再长就不叫"摘要"了。
     */
    const val MAX_SUMMARY_CHARS = 200

    /**
     * 详情段的分隔标记 —— 与电脑侧 `app/assistant.py::_DETAIL_SPLIT_RE` **同一组词**。
     *
     * 为什么必须跟电脑那侧对得上：回复的约定格式是
     * `<极简结论一句话>` + `详情如下：` + `<详细内容>`，电脑那边就是按这一行切的。
     * 两处词表不一致时，手机上会多念（或漏念）一段 —— 而它看起来完全不像是 bug。
     */
    private val DETAIL_MARKER = Regex(
        "^(?:详情如下|详细如下|细节如下|以下是详情|以下为详情|详情[:：]|详细信息[:：]?)[:：]?"
    )

    /** markdown 小标题（`## 详情`）：它开的是后文的一节，不是摘要。 */
    private val HEADING = Regex("^#{1,6}\\s*")

    /** 标题小标题里这几个字本身就写着"这是详情"，开头就是它 = 没有可念的摘要。 */
    private val DETAIL_TITLE_WORDS = listOf("详情", "详细", "细节", "明细")

    /**
     * 一条指令该念什么。返回 null = **一个字都不念**（电脑发起的 / 没有可念的文字）。
     *
     * 调用方（常驻听写、说完一段语音后的自动朗读）都只调它，不再自己看
     * `brief` / `reply` —— 见类注释"单一出处"。
     */
    fun decide(item: CommandItem): String? = decide(
        origin = originOf(item),
        speakText = item.speakText,
        brief = item.brief,
        reply = item.reply,
    )

    /**
     * 判据本体（纯函数，单测直接喂参数，不用造 DTO / 网络）。
     *
     * @param origin    发起方（`mobile` / `pc` / null = 拿不到）
     * @param speakText 电脑侧给的"要念的那段话"；**只认字符串**（数字 / 对象 / 空白一律当没有）
     * @param brief     老电脑的语音简报（`commands.brief`，本来就只含结论）
     * @param reply     老电脑的整篇回复（`commands.reply`，可能带详情）
     */
    fun decide(origin: String?, speakText: Any?, brief: String?, reply: String?): String? {
        // ① 谁发起谁念：电脑的活儿，手机上连一个字都不念。
        if (normalizeOrigin(origin) == EventNotify.ORIGIN_PC) return null

        // ② 单一出处：电脑给了要念的那段话就原样念它（**不再自己切**）。
        //    `as? String` 是故意宽容的：电脑侧哪天把字段写成数字/对象/true，
        //    这里只是"当它没给"然后走第 ③ 条 —— 绝不能把 [42] 或 [object] 念出来，
        //    更不能因为一个脏字段把整条结果判成失败。
        val given = (speakText as? String)?.trim()
        if (!given.isNullOrEmpty()) return given

        // ③ 老电脑：退回"取摘要"。先看 `brief`（电脑侧早就取好的"念的短句"），
        //    没有才从整篇 `reply` 里切开头那一段。
        val candidate = brief?.trim()?.takeIf { it.isNotEmpty() }
            ?: reply?.trim()?.takeIf { it.isNotEmpty() }
            ?: return null
        return summaryOf(candidate).ifEmpty { null }
    }

    /**
     * 从一整篇回复里取"**开头那段摘要**"（纯函数）。
     *
     * 规则：逐行往下收，撞到下面任意一条就**停**（后面的都属于"详情"，一句都不念）：
     *  1. **空行** —— 段落边界（`第一段\n\n后面都是详情…`）；
     *  2. **详情标记行** —— `详情如下：` / `详细信息：` …（与电脑侧同一组词）；
     *  3. **代码围栏**（```` ``` ````）—— 代码是给眼睛看的，念出来是一串符号；
     *  4. **markdown 小标题**（`## …`）—— 它开的是后文的一节。
     *
     * 一条都没撞上（老格式的**单行长文**）就整段按 [MAX_SUMMARY_CHARS] 截断：
     * 宁可少念一句，也**绝不**把整篇详情念出来（用户原话就是冲着这个来的）。
     *
     * 开头那行如果是小标题，就把它当正文收下（去掉 `#`）继续往下走 ——
     * 除非它自己写的就是"详情/详细…"（那就是没有摘要可说）。
     */
    fun summaryOf(raw: String): String {
        val text = raw.replace("\r\n", "\n").replace('\r', '\n').trim()
        if (text.isEmpty()) return ""

        val kept = mutableListOf<String>()
        for (line in text.split('\n')) {
            var t = line.trim()
            if (t.isEmpty()) break                                   // ① 空行 = 段落边界
            if (startsAt(t, DETAIL_MARKER)) break                     // ② "详情如下：" 起头
            if (t.startsWith("```")) break                            // ③ 代码块
            if (HEADING.find(t)?.range?.first == 0) {                 // ④ 小标题
                val title = t.replace(HEADING, "").trim()
                if (kept.isNotEmpty() || DETAIL_TITLE_WORDS.any { title.startsWith(it) }) break
                // 开头就是标题（`## 结论`）：标题不算正文，去掉井号当第一句收下
                t = title
                if (t.isEmpty()) continue
            }
            kept += t
        }
        if (kept.isEmpty()) return ""
        return clamp(kept.joinToString("\n"))
    }

    /** 太长的摘要按句子截断（[MAX_SUMMARY_CHARS]），并如实留一个省略号。 */
    private fun clamp(text: String): String {
        if (text.length <= MAX_SUMMARY_CHARS) return text
        val cut = text.substring(0, MAX_SUMMARY_CHARS)
        val stop = cut.indexOfLast { it in "。！？；，、：!?" }
        val kept = if (stop >= MAX_SUMMARY_CHARS / 2) cut.substring(0, stop + 1) else cut
        return kept.trimEnd('，', '、', '；', '：', ' ', '\n') + "……"
    }

    /** [regex] 是否**从行首**匹配（`^` 在 `find` 里不作为"任意位置"用，这里显式比对起点）。 */
    private fun startsAt(line: String, regex: Regex): Boolean =
        regex.find(line)?.range?.first == 0

    /**
     * 一条指令的发起方：先看 `origin`（电脑侧新带上的），没有再看 `source`。
     *
     * **拿不到就回 null = 不拦**（[decide] 只在明确 `pc` 时才静默）。
     */
    fun originOf(item: CommandItem): String? {
        normalizeOrigin(item.origin)?.let { return it }
        val source = item.source?.trim()?.lowercase().orEmpty()
        return when {
            source.isEmpty() -> null
            source == EventNotify.ORIGIN_MOBILE -> EventNotify.ORIGIN_MOBILE
            else -> EventNotify.ORIGIN_PC
        }
    }

    /** `origin` 取值归一（容忍大小写与空白）；认不出的取值 → null（**不拦**）。 */
    fun normalizeOrigin(raw: String?): String? = when (raw?.trim()?.lowercase()) {
        EventNotify.ORIGIN_PC -> EventNotify.ORIGIN_PC
        EventNotify.ORIGIN_MOBILE -> EventNotify.ORIGIN_MOBILE
        else -> null
    }

    /**
     * 不念的**人话原因**（只在 [decide] 回了 null 之后调）。
     *
     * 为什么要说出来：常驻听写没有界面，用户能知道的只有通知那一行。
     * "这条不念"与"它没反应"在车里听起来一模一样，不说清就会被当成坏了。
     */
    fun silentReason(item: CommandItem): String =
        if (originOf(item) == EventNotify.ORIGIN_PC) {
            "电脑发起的任务，手机上不念（谁发起谁念）"
        } else {
            "这条没有可念的摘要（回复是空的）"
        }
}
