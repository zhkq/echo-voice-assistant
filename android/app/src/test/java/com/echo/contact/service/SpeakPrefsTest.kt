package com.echo.contact.service

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 「谁念回复」这条偏好的判据。
 *
 * 它值一条用例，是因为**判据只有一处**（[SpeakPrefs.normalize]）：
 * 存的可能是空、是老版本写的怪值、是用户手改过的 prefs ——
 * 这些都必须落回**手机念**（默认），而不是变成一个谁也认不出的引擎名
 * （那样 `speak()` 会走进 ECHO 分支去要网，表现成"设了个东西之后反而念不出来了"）。
 */
class SpeakPrefsTest {

    @Test
    fun `echo 是唯一会被认出来的非默认值`() {
        assertEquals(SpeakPrefs.ENGINE_ECHO, SpeakPrefs.normalize(SpeakPrefs.ENGINE_ECHO))
        assertEquals(SpeakPrefs.ENGINE_SYSTEM, SpeakPrefs.normalize(SpeakPrefs.ENGINE_SYSTEM))
    }

    @Test
    fun `认不出来的值一律回默认`() {
        for (raw in listOf(null, "", "ECHO", "Echo", "echo ", "system ", "sonic", "1")) {
            assertEquals("raw=$raw 该回默认", SpeakPrefs.ENGINE_SYSTEM, SpeakPrefs.normalize(raw))
        }
    }

    @Test
    fun `中文标签跟着判据走`() {
        assertEquals("ECHO 念", SpeakPrefs.label(SpeakPrefs.ENGINE_ECHO))
        assertEquals("手机念", SpeakPrefs.label(SpeakPrefs.ENGINE_SYSTEM))
        assertEquals("手机念", SpeakPrefs.label(null))
        assertEquals("手机念", SpeakPrefs.label("乱写的"))
    }
}
