package com.echo.contact.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 「App 自己的选项」那条落盘的口径（`AppPrefs`）。
 *
 * 它值一组用例，是因为**判据只有一处**（[AppPrefs.normalizeDeviceId]）：
 * 库里可能存着负数、手改过的怪值，也可能某次写入失败 —— 这些都必须落回
 * "跟随系统"，而不是变成一个谁也不认识的设备 id（那会让"麦克风"直接开不出来，
 * 而界面上只会显示"开不了麦"，查起来极贵）。
 */
class AppPrefsTest {

    @Test
    fun `负数一律算跟随系统`() {
        assertEquals(AppPrefs.FOLLOW_SYSTEM, AppPrefs.normalizeDeviceId(-1))
        assertEquals(AppPrefs.FOLLOW_SYSTEM, AppPrefs.normalizeDeviceId(-99))
    }

    @Test
    fun `非负的设备 id 原样保留`() {
        assertEquals(0, AppPrefs.normalizeDeviceId(0))
        assertEquals(7, AppPrefs.normalizeDeviceId(7))
    }

    @Test
    fun `哨兵值本身是负数`() {
        assertTrue(AppPrefs.FOLLOW_SYSTEM < 0)
    }

    // ---- 输出设备（ECHO 念优先用哪支）与"哨兵值本身是负数"同一个口径 ----

    @Test
    fun `输出设备的 id 与输入设备同一套夹取`() {
        // 两条路都存 AudioDeviceInfo.id，判据必须**只有一处**（normalizeDeviceId）：
        // 输出侧要是各判一次，迟早出现"输入跟随系统、输出却去开一个不存在的设备"
        assertEquals(AppPrefs.FOLLOW_SYSTEM, AppPrefs.normalizeDeviceId(-1))
        assertEquals(AppPrefs.FOLLOW_SYSTEM, AppPrefs.normalizeDeviceId(-7))
        assertEquals(3, AppPrefs.normalizeDeviceId(3))
    }

    // ---- 「手机当麦克风」每段多长 ----

    @Test
    fun `三个可用档位原样保留`() {
        for (sec in AppPrefs.SEGMENT_SECONDS_CHOICES) {
            assertEquals("$sec 秒是界面上真实存在的档，必须原样留着", sec,
                AppPrefs.normalizeSegmentSeconds(sec))
        }
    }

    @Test
    fun `认不出的秒数夹到最近的一档而不是夹到 0`() {
        // 0 / 负数会让"每 0 秒切一段" → 空段刷屏；判据必须是"落到某一档"，
        // 而不是"夹到上下界"（夹到下界 15 就丢掉了 60 那一档的意图）
        assertEquals(15, AppPrefs.normalizeSegmentSeconds(0))
        assertEquals(15, AppPrefs.normalizeSegmentSeconds(-30))
        assertEquals(15, AppPrefs.normalizeSegmentSeconds(14))
        assertEquals(15, AppPrefs.normalizeSegmentSeconds(7))
        // 18 距 15 是 3、距 30 是 12 → 取 15（**不是**"超过 15 就跳 30"）
        assertEquals(15, AppPrefs.normalizeSegmentSeconds(18))
        assertEquals(30, AppPrefs.normalizeSegmentSeconds(45))
        assertEquals(60, AppPrefs.normalizeSegmentSeconds(55))
        assertEquals(60, AppPrefs.normalizeSegmentSeconds(9999))
    }

    @Test
    fun `默认是 30 秒`() {
        // 为什么是 30：一段 ≈1MB（16kHz 单声道 16bit），走中继（单次上限 12MB）很稳。
        // 这个默认值写在界面上（"默认 30 秒 ≈1 MB"），所以它也是一个判据
        assertEquals(30, AppPrefs.DEFAULT_SEGMENT_SECONDS)
        assertTrue("默认那一档必须在可选档里（否则界面会显示一个选不中的值）",
            AppPrefs.SEGMENT_SECONDS_CHOICES.contains(AppPrefs.DEFAULT_SEGMENT_SECONDS))
    }

    @Test
    fun `夹取过的值再过一遍不变`() {
        // 幂等：读出来 → 存回去 不该漂移（否则用户每进一次设置页档位就"自己变了"）
        for (raw in listOf(-1, 0, 7, 15, 18, 30, 45, 60, 9999)) {
            val once = AppPrefs.normalizeSegmentSeconds(raw)
            assertEquals("raw=$raw 再夹一次变了", once, AppPrefs.normalizeSegmentSeconds(once))
        }
    }
}
