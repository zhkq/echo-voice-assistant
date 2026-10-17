package com.echo.contact.service

import com.echo.contact.audio.SegmentEncoding
import com.echo.contact.audio.SegmentFormat
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 「手机当麦克风」那条路的几个纯函数（`toPcmBytes` / `rms` / 通知那一行 / 格式那句如实的话）。
 *
 * 它们值一组用例，是因为**错了会静默**：
 *  * 字节序写反 → 传上去的音频在电脑里是噪声/静音，而手机这边一切"正常"；
 *  * RMS 忘了夹住 0..1 → 电平条会画出屏幕（或恒为满格）；
 *  * 通知那一行的优先级写错 → 在最该说话的时候沉默（"可能真的丢了"被套话盖掉）。
 */
class MeetingMicPcmTest {

    @Test
    fun `PCM 是小端两字节`() {
        // 0x0102 = 258：小端在文件里是 0x02 0x01
        val b = toPcmBytes(listOf(0x0102.toShort()))
        assertEquals(2, b.size)
        assertEquals(0x02.toByte(), b[0])
        assertEquals(0x01.toByte(), b[1])
    }

    @Test
    fun `负数采样按补码写`() {
        val b = toPcmBytes(listOf((-2).toShort()))          // 0xFFFE
        assertEquals(0xFE.toByte(), b[0])
        assertEquals(0xFF.toByte(), b[1])
    }

    @Test
    fun `长度是采样数的两倍`() {
        assertEquals(200, toPcmBytes(List(100) { 0 }).size)
        assertEquals(0, toPcmBytes(emptyList()).size)
    }

    @Test
    fun `静音的电平是 0`() {
        assertEquals(0f, rms(ShortArray(1600), 1600), 0.0001f)
    }

    @Test
    fun `满量程的电平夹在 1 以内`() {
        val loud = ShortArray(1600) { Short.MAX_VALUE }
        val v = rms(loud, 1600)
        assertTrue("越界了：$v", v in 0f..1f)
        assertTrue("满量程该很接近 1：$v", v > 0.9f)
    }

    @Test
    fun `n 为 0 时不该炸`() {
        assertEquals(0f, rms(ShortArray(10), 0), 0.0001f)
    }

    // ---- 通知里那一行字（"录音到底有没有丢东西"的唯一出口）----

    @Test
    fun `还没传上去的段数要出现在通知里`() {
        // ⚠️ 改写：合并成一条路之后不再有"在线待重传"这个第二套队列，
        // `pending` 换成了 `syncPending`（本地清单里还欠着的段数）—— 判据本身没放松：
        // 用户不在界面里时，通知是唯一能看到"还有段没送出去"的地方。
        val s = notificationText(MicState(active = true, sent = 4, syncPending = 2))
        assertTrue("还有几段没传必须写在通知里：$s", s.contains("已缓存 2 段"))
        assertTrue("已经传上去的也要在：$s", s.contains("等联网上传"))
    }

    @Test
    fun `错误优先于静音与还没传的段`() {
        // "有一段缓存不下来，可能真的丢了"绝不能被"已缓存 3 段"盖掉 ——
        // 那是真的丢音频，优先级写错就等于在最该说话的时候沉默
        val s = notificationText(
            MicState(active = true, paused = true, sent = 3, syncPending = 5,
                error = "有一段缓存不下来"))
        assertEquals("有一段缓存不下来", s)
    }

    @Test
    fun `静音优先于还没传的段`() {
        val s = notificationText(MicState(active = true, paused = true, syncPending = 5))
        assertTrue("静音这件事更要紧（这段时间本来就不会录进会议）：$s", s.contains("已静音"))
        assertTrue("但也不能把这个状态说成'在录'：$s", !s.contains("在录"))
    }

    @Test
    fun `一切正常时只报已传段数`() {
        val s = notificationText(MicState(active = true, sent = 7))
        assertTrue(s, s.contains("在录"))
        assertTrue(s, s.contains("已传 7 段"))
        assertTrue("没有欠的段就不该出现'已缓存'：$s", !s.contains("已缓存"))
    }

    @Test
    fun `退回 WAV 时那一行如实说清是哪一段 为什么`() {
        // 用户 2026-10-17 点名要求的："在界面/日志如实体现'这段用 WAV（FLAC 编码器不可用）'"
        val one = segmentFormatNote(2, SegmentFormat.WAV, SegmentEncoding.FLAC_UNAVAILABLE, 1)
        assertTrue(one, one.contains("第 2 段用 WAV"))
        assertTrue(one, one.contains(SegmentEncoding.FLAC_UNAVAILABLE))
        assertTrue("只有一段时别啰嗦本场累计", !one.contains("本场已有"))
        val many = segmentFormatNote(3, SegmentFormat.WAV, SegmentEncoding.FLAC_UNAVAILABLE, 3)
        assertTrue("有累计才说清'这场不止一段退了'：$many", many.contains("本场已有 3 段用 WAV"))
        assertEquals("编成 FLAC 时什么都不说（别用套话占住那一行）",
            "", segmentFormatNote(1, SegmentFormat.FLAC, "", 0))
    }

    // ---- 暂停语义（用户 2026-10-16 深夜推翻旧的"丢帧"口径；详见 MicCaptureLoop 的注释）----

    /**
     * 这一组钉的是**暂停 = 照常计数、但写全零**。
     *
     * 为什么这条最要紧：段是按**累计样本数**切的，暂停要是少算（旧的 `continue` 丢帧），
     * 后面所有段的边界都会整体前移 —— 一场 40 分钟、中间暂停 5 分钟，音频就只有 35 分钟，
     * "第 N 段 = 挂钟第 N 个段长"这条对齐关系当场失效（转写时间轴与音频对不上）。
     * 所以"暂停帧一个样本都不许少"必须是一条判据，而不是一句注释。
     */
    @Test
    fun `暂停帧与真实帧同长度且内容是零`() {
        val loop = MicCaptureLoop(100)
        val real = ShortArray(10) { (it + 1).toShort() }
        val buffer = PcmBuffer()

        val paused = loop.feed(buffer, real, 10, active = false)

        assertEquals("暂停帧照样贡献样本（少一个都会让后面的段时间轴前移）", 10, paused.added)
        assertEquals("长度必须是同长度", 10, buffer.size)
        assertTrue("暂停写进去的必须全是零：$buffer", buffer.all { it == 0.toShort() })
        assertFalse("没攒够一段不该封", paused.sealed)
    }

    @Test
    fun `暂停期间照样封段（段边界落在同一刻度上）`() {
        val loop = MicCaptureLoop(30)
        val real = ShortArray(10) { 7 }
        val buffer = PcmBuffer()
        var seals = 0

        repeat(9) {
            if (loop.feed(buffer, real, 10, active = false).sealed) {
                seals++
                assertEquals("封段那一刻缓冲里必须正好是一段（多了就说明没在刻度上封）",
                    30, buffer.size)
                buffer.clear()          // 清的人**必须是封段那个人**（见下面那条用例）
            }
        }

        assertEquals("90 个零样本按 30 一段切，必须正好封 3 段（丢帧的话这一段就没了）",
            3, seals)
        assertTrue("每段封完由调用方清空（于是每段恰好 30 个样本）", buffer.isEmpty())
    }

    @Test
    fun `不暂停时样本照原样进缓冲（别把真音频弄丢了）`() {
        val loop = MicCaptureLoop(4)
        val real = shortArrayOf(1, 2, 3, 4)
        val buffer = PcmBuffer()

        val feed = loop.feed(buffer, real, 4, active = true)

        assertTrue(feed.sealed)
        assertEquals(listOf<Short>(1, 2, 3, 4), buffer.toList())
    }

    @Test
    fun `一帧跨过段边界时进位不丢样本`() {
        // 真机参数（100ms 帧 vs 15/30/60 秒段）是整除的，所以这条用不上；但"样本一个都不丢"
        // 这条不变量要一直成立 —— 这也是旧代码的形状（整帧加进去、满了才封）。
        // ⚠️ 满了之后 `feed` **不清缓冲**：封段的人要先读它拿到 PCM（纯逻辑里清了就等于丢一段）
        val loop = MicCaptureLoop(8)
        val buffer = PcmBuffer()

        val one = loop.feed(buffer, ShortArray(6) { 1 }, 6, active = true)
        assertFalse("6 个还没满 8", one.sealed)
        assertEquals(6, one.added)
        val two = loop.feed(buffer, ShortArray(6) { 2 }, 6, active = true)

        assertTrue("6 + 6 跨过 8：这一段要在这一帧封出去", two.sealed)
        assertEquals("12 个样本一个都不许丢", 12, one.added + two.added)
        assertEquals("缓冲里就是那 12 个（封完由调用方读走再清）", 12, buffer.size)
        assertEquals(listOf<Short>(1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2), buffer.toList())
    }

    @Test
    fun `封段之后缓冲不清（清的人必须是封段那个人）`() {
        // 这条挡的是一次真实踩到的错法：`feed` 里顺手 `clear()` → 调用方读到空缓冲 →
        // 封下去的是一段**全零**（真的丢音频），而手机上一切"看着正常"。
        val loop = MicCaptureLoop(4)
        val buffer = PcmBuffer()

        val feed = loop.feed(buffer, shortArrayOf(1, 2, 3, 4), 4, active = true)

        assertTrue(feed.sealed)
        assertEquals("满了也要留着那 4 个样本让调用方去封", listOf<Short>(1, 2, 3, 4), buffer.toList())
    }

    /**
     * `PcmBuffer` 必须与原来的 `List<Short>` 那条路产出**同一份 PCM**。
     *
     * 为什么要这条：默认段长改成 600 秒后，缓冲从 `ArrayList<Short>` 换成了原始 `ShortArray`
     * （装箱的 960 万样本 ≈190 MB，手机上会 OOM）。这属于"换实现"，
     * 一旦字节序/顺序写错，传上去的音频就是噪声，而手机这边一切"正常" —— 所以必须钉住。
     */
    @Test
    fun `PcmBuffer 与 List 那条路产出同一份 PCM`() {
        val buffer = PcmBuffer(8)
        buffer.addSamples(shortArrayOf(1, 2, 3), 0, 3)
        buffer.addZeros(2)
        buffer.addSample((-2).toShort())

        assertEquals(6, buffer.size)
        assertEquals(listOf<Short>(1, 2, 3, 0, 0, -2), buffer.toList())
        assertArrayEquals(
            "原生缓冲与 List 版本必须逐字节一致",
            toPcmBytes(listOf<Short>(1, 2, 3, 0, 0, -2)),
            toPcmBytes(buffer),
        )
        assertEquals("本段进度：6 个样本 / 16kHz", 6.0 / 16_000, progressOf(buffer, 16_000), 1e-12)

        val copy = buffer.toShortArray()
        buffer.clear()
        assertArrayEquals("clear 不许改到已经拷出去的尾段", shortArrayOf(1, 2, 3, 0, 0, -2), copy)
        assertEquals("clear 之后是空的", 0, buffer.size)
    }
}
