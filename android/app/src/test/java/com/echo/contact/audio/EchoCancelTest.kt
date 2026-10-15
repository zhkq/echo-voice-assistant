package com.echo.contact.audio

import android.media.MediaRecorder
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 「念回复时能不能继续听」的判据（`EchoCancel.decide` / `sourceFor`）—— 纯 JVM。
 *
 * 真机上那两行（`available()` / `attach()`）测不了，但**决策**必须钉住：
 *   * 用户没要全双工 → 半双工（任何设备都能跑）；
 *   * 用户要了、设备没有 AEC → **仍然是半双工**（悄悄当全双工 = 用户听到自激）；
 *   * 只有"要了 + 有 AEC"才全双工，并且采集源随之切换。
 */
class EchoCancelTest {

    @Test
    fun `没要全双工就是半双工`() {
        assertEquals(EchoCancel.Mode.HALF_DUPLEX, EchoCancel.decide(false, false))
        assertEquals(
            "设备有 AEC 也不能自作主张开",
            EchoCancel.Mode.HALF_DUPLEX,
            EchoCancel.decide(false, true),
        )
    }

    @Test
    fun `要了但设备没有 AEC 也退回半双工`() {
        assertEquals(EchoCancel.Mode.HALF_DUPLEX, EchoCancel.decide(true, false))
    }

    @Test
    fun `要了并且有 AEC 才给全双工`() {
        assertEquals(EchoCancel.Mode.FULL_DUPLEX, EchoCancel.decide(true, true))
    }

    @Test
    fun `采集源跟着模式走`() {
        assertEquals(
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
            EchoCancel.sourceFor(EchoCancel.Mode.FULL_DUPLEX),
        )
        assertEquals(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            EchoCancel.sourceFor(EchoCancel.Mode.HALF_DUPLEX),
        )
    }
}
