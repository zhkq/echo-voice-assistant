package com.echo.contact.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 「哪种事件值得打扰人」的判据（`EventNotify.summarize`，纯函数）。
 *
 * 用户 2026-10-15 的口径是"**手机收**" —— 但"收"不等于"什么都弹"：
 *  `command_received` / `command_sent` 是过程，弹出来只会吵；而"转写好了""纪要好
 *  了""有结果了"才是他等的那件事。这条边界写错了，用户会关掉整个通知权限。
 */
class EventNotifyTest {

    @Test
    fun `等的那几件事都要弹`() {
        for (t in listOf("meeting_transcribed", "meeting_summary_ready",
                         "meeting_stopped", "meeting_imported", "command_done")) {
            val s = EventNotify.summarize(t, mapOf("origin" to "mobile"))
            assertTrue("$t 该有通知", s != null)
            assertTrue("$t 的标题不该为空", s!!.first.isNotBlank())
            assertTrue("$t 的正文不该为空", s.second.isNotBlank())
        }
    }

    @Test
    fun `过程类事件不打扰`() {
        // 发起方给 mobile：这条钉的是**第二层**（种类）—— 手机自己的过程类事件也不吵，
        // 免得"加了发起方这道闸"之后这条用例变成因为别的理由通过（那样第二层就没人守了）。
        for (t in listOf("command_received", "command_sent", "wake_fired", "whatever_new")) {
            assertNull("$t 不该弹通知", EventNotify.summarize(t, mapOf("origin" to "mobile")))
        }
    }

    @Test
    fun `带上会议名更像人话`() {
        val s = EventNotify.summarize(
            "meeting_transcribed",
            mapOf("origin" to "mobile", "meeting" to "2026-10-15_15-36-48"),
        )
        assertTrue(s!!.second, s.second.contains("2026-10-15_15-36-48"))
    }

    @Test
    fun `没有会议名也要能说清楚`() {
        val s = EventNotify.summarize("meeting_transcribed", mapOf("origin" to "mobile"))
        assertEquals("会议转写好了", s!!.first)
        assertTrue(s.second.isNotBlank())
    }

    @Test
    fun `payload 里乱七八糟的值不该炸`() {
        val s = EventNotify.summarize("meeting_transcribed",
            mapOf("origin" to "mobile", "meeting" to 42, "name" to null, "id" to listOf(1, 2)))
        assertTrue(s != null)
        assertTrue("数字/空值都不该被当成会议名", !s!!.second.contains("42"))
    }

    // ---------------------------------------------------------------- 任务 B8：点通知要落到那场会议

    /**
     * 「哪个事件该带目标 → extra 键值」的映射（需求判据 ①）。
     *
     * 为什么键也要断言：`EventNotify` 与 `MainActivity` 是靠**同一个常量**
     * 对上的，谁把它改成别的字面量都不会编译报错 —— 那是"点通知没反应"这类
     * 最难查的 bug（两边各自看着都对）。所以这里连键一起钉住。
     */
    @Test
    fun `会议类事件要带上目标会议名与约定的键`() {
        for (t in listOf(
            "meeting_transcribed", "meeting_summary_ready", "meeting_stopped", "meeting_imported",
        )) {
            val target = EventNotify.meetingTargetOf(t, mapOf("meeting" to "2026-10-16_09-00-00"))
            assertTrue("$t 该带上目标会议", target != null)
            assertEquals("键必须是约定的那个，否则 MainActivity 读不到", "echo_open_meeting", target!!.first)
            assertEquals("2026-10-16_09-00-00", target.second)
        }
    }

    @Test
    fun `与会议无关的事件不该硬塞一个目标`() {
        // 指令有结果、回顾好了：它们**不属于**任何一场会议。
        // 硬跳进会议页只会让用户莫名其妙（点"指令有结果"却看见会议列表）。
        for (t in listOf("command_done", "review_ready", "command_sent", "wake_fired")) {
            assertNull(
                "$t 不该带会议目标",
                EventNotify.meetingTargetOf(t, mapOf("meeting" to "2026-10-16_09-00-00")),
            )
        }
    }

    @Test
    fun `没有会议名的事件不带目标`() {
        // 没有名字就无从"定位"：只能照旧回首页，不能带着一个空目标跳过去。
        assertNull(
            "没有会议名时不该有目标（否则会打开一场空会议）",
            EventNotify.meetingTargetOf("meeting_transcribed", emptyMap()),
        )
        assertNull(
            "会议名是数字（脏 payload）时也不算目标",
            EventNotify.meetingTargetOf("meeting_stopped", mapOf("meeting" to 42)),
        )
        assertNull(
            "会议名是空白时不算目标",
            EventNotify.meetingTargetOf("meeting_stopped", mapOf("name" to "   ")),
        )
    }

    @Test
    fun `payload 只有 name 字段时也能认出那场会议`() {
        // 导入录音那条路给的是 `name` 而不是 `meeting`（见 summarize），别漏。
        val target = EventNotify.meetingTargetOf("meeting_imported", mapOf("name" to "导入的那场"))
        assertEquals("导入的那场", target?.second)
    }

    /**
     * 正文 → 会议名 的登记（`post` 就是靠它把目标补进 intent 的）。
     *
     * 为什么要这条：**后台那条通知路**（`ListenService.eventLoop`）只拿得到
     * `summarize` 的标题+正文。如果这里不登记，后台收到的"会议纪要好了"
     * 点进去就仍然只到首页 —— 也就是需求要修的那个现状，只是换了一条路复发。
     */
    @Test
    fun `summarize 会把会议名登记到那句正文上供 post 取回`() {
        val s = EventNotify.summarize(
            "meeting_summary_ready",
            mapOf("origin" to "mobile", "meeting" to "2026-10-16_10-00-00"),
        )
        assertTrue(s != null)
        assertEquals(
            "post 只拿得到正文，必须能从正文反查出会议名",
            "2026-10-16_10-00-00",
            EventNotify.rememberedMeetingFor(s!!.second),
        )
    }

    @Test
    fun `与会议无关的通知不会把正文登记成会议`() {
        val s = EventNotify.summarize("command_done", mapOf("origin" to "mobile", "meeting" to "不该出现"))
        assertNull(
            "无关事件的正文不该被当成某场会议的目标",
            EventNotify.rememberedMeetingFor(s!!.second),
        )
    }

    /**
     * 登记表**必须有界**：`EventNotify` 活在常驻听写进程里，可能连着开好几天。
     * 无界的表就是一条慢性内存泄漏；有界表超出后的后果只是"点通知回首页"
     * （等于这条功能之前的行为），可以接受。
     */
    @Test
    fun `登记表有上限且丢的是最旧的`() {
        val field = EventNotify::class.java.getDeclaredField("rememberedMeeting")
        field.isAccessible = true

        // 用 summarize 真正的返回体当键（不手抄文案：文案改了要跟着改的是行为，不是这里的字面量）
        // 插入顺序：最先放"最旧的"，最后放"最新的" —— 这样超过上限时被丢的必须是最旧那条。
        val oldest = EventNotify.summarize(
            "meeting_stopped", mapOf("origin" to "mobile", "meeting" to "会议-0"),
        )!!.second
        for (i in 1 until 44) {
            EventNotify.summarize("meeting_stopped", mapOf("origin" to "mobile", "meeting" to "会议-$i"))
        }
        val newest = EventNotify.summarize(
            "meeting_stopped", mapOf("origin" to "mobile", "meeting" to "会议-44"),
        )!!.second

        @Suppress("UNCHECKED_CAST")
        val map = field.get(EventNotify) as Map<String, String>
        assertEquals("登记表必须有上限，否则常驻进程里会一直长", 40, map.size)
        assertNull("最旧的该被丢掉", EventNotify.rememberedMeetingFor(oldest))
        assertEquals(
            "最新的必须还在（否则通知点了找不到会议）",
            "会议-44",
            EventNotify.rememberedMeetingFor(newest),
        )
    }

    @Test
    fun `同一场会议再通知一次仍然记得住`() {
        // 同一场会议会连着发好几条（转写好了 → 纪要好了）：正文字面不同、会议名相同。
        // 两条都要能反查出同一个会议名，否则"纪要好了"点进去会落空。
        val a = EventNotify.summarize("meeting_transcribed", mapOf("origin" to "mobile", "meeting" to "同一场"))
        val b = EventNotify.summarize("meeting_summary_ready", mapOf("origin" to "mobile", "meeting" to "同一场"))
        assertEquals("同一场", EventNotify.rememberedMeetingFor(a!!.second))
        assertEquals("同一场", EventNotify.rememberedMeetingFor(b!!.second))
    }

    // ------------------------------------------------- 任务 B2：谁发起的任务才值得打扰手机

    /**
     * 用户 2026-10-16 的口径原话：**"手机侧发的任务，手机接受通知，电脑端发起的任务不用给手机"**。
     *
     * 判据是"**谁发起的任务**"，不是"哪台机器产生的数据"：一场手机发起的会议，转写/纪要是
     * 电脑算的，但结果仍然是手机在等 → 还是 mobile。反之电脑自己录的会议、在电脑上敲的指令，
     * **一条都不该到手机上**（否则手机变成电脑干活儿的播报器）。
     */
    private val 会打扰的种类 = listOf(
        "meeting_transcribed", "meeting_summary_ready", "meeting_stopped",
        "meeting_imported", "command_done", "review_ready",
    )

    @Test
    fun `电脑端发起的任务一律不弹`() {
        // 同一批"该弹的类型"，只把发起方换成电脑 —— 全部必须静默。
        // 这条是这次口径的**主判据**：以前它们全都会弹（不管谁发起的）。
        for (t in 会打扰的种类) {
            assertNull(
                "$t 是电脑发起的，不该打扰手机",
                EventNotify.summarize(t, mapOf("origin" to "pc", "meeting" to "电脑上自己录的那场")),
            )
        }
    }

    @Test
    fun `origin 缺失或认不出时保守不打扰`() {
        // 老事件（电脑侧还没带上 origin）与脏 payload：**宁可漏一条，不可多吵一次**。
        // 反过来兜底成 mobile 的话，一次字段没带上就是一场刷屏。
        val 认不出的 = listOf<Map<String, Any?>>(
            emptyMap(),                                        // 完全没有 origin
            mapOf("meeting" to "老事件的那场"),                    // 有别的字段、就是没有 origin
            mapOf("origin" to null),                           // 显式空
            mapOf("origin" to ""),                             // 空串
            mapOf("origin" to "   "),                          // 空白
            mapOf("origin" to 42),                             // 不是字符串（脏 payload 不该炸）
            mapOf("origin" to "laptop"),                       // 认不出的取值：只有 mobile 放行
        )
        for (t in 会打扰的种类) {
            for (p in 认不出的) {
                assertNull("$t 在 $p 下该静默（拿不到发起方就按电脑处理）", EventNotify.summarize(t, p))
            }
        }
    }

    @Test
    fun `手机发起但不值得打扰的种类仍然不弹`() {
        // **两层判据都生效**：发起方对了，第二层（种类）照样拦。
        // 这条是防"为了修电脑来源而把类型映射整层删掉"的护栏。
        for (t in listOf("command_received", "command_sent", "wake_fired", "whatever_new")) {
            assertNull("$t 即使手机发起也不该打扰", EventNotify.summarize(t, mapOf("origin" to "mobile")))
        }
    }

    @Test
    fun `电脑发起的会议事件不会进登记表`() {
        // 顺序的后果：发起方那道闸放在**取会议名/登记之前**，所以电脑的活儿连
        // rememberedMeeting 都不会碰（否则一张小表会被电脑自己的会议挤掉手机那几条）。
        @Suppress("UNCHECKED_CAST")
        val map = rememberedMeetingField().get(EventNotify) as Map<String, String>
        EventNotify.summarize("meeting_summary_ready", mapOf("origin" to "pc", "meeting" to "电脑的会"))
        assertTrue(
            "电脑发起的会议名不该出现在登记表里",
            map.values.none { it == "电脑的会" },
        )
    }

    private fun rememberedMeetingField(): java.lang.reflect.Field =
        EventNotify::class.java.getDeclaredField("rememberedMeeting").apply { isAccessible = true }
}
