package com.echo.contact.service

import android.view.KeyEvent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * 耳机/车机媒体键的映射（`ListenKeys`）—— 纯 JVM。
 *
 * 开车时人只会按这一个键，所以映射必须**确定**：按一下到底是"暂停"还是"继续"，
 * 取决于现在是不是暂停态；而"没在听"的时候**什么都不做**（绝不允许"按一下自己开始录音"）。
 */
class ListenKeysTest {

    @Test
    fun `没在听时按什么键都不动`() {
        val keys = listOf(
            KeyEvent.KEYCODE_MEDIA_PLAY,
            KeyEvent.KEYCODE_MEDIA_PAUSE,
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
            KeyEvent.KEYCODE_HEADSETHOOK,
            KeyEvent.KEYCODE_MEDIA_STOP,
        )
        for (k in keys) {
            assertNull("没在听就不该响应键 $k", ListenKeys.actionFor(k, running = false, userPaused = false))
            assertNull("没在听就不该响应键 $k", ListenKeys.actionFor(k, running = false, userPaused = true))
        }
    }

    @Test
    fun `在听时播放暂停键是暂停`() {
        for (k in listOf(
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
            KeyEvent.KEYCODE_HEADSETHOOK,
            KeyEvent.KEYCODE_MEDIA_PLAY,
            KeyEvent.KEYCODE_MEDIA_PAUSE,
        )) {
            assertEquals(
                "键 $k",
                ListenKeys.Action.PAUSE,
                ListenKeys.actionFor(k, running = true, userPaused = false),
            )
        }
    }

    @Test
    fun `已暂停时同一个键是继续`() {
        for (k in listOf(
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
            KeyEvent.KEYCODE_HEADSETHOOK,
            KeyEvent.KEYCODE_MEDIA_PLAY,
            KeyEvent.KEYCODE_MEDIA_PAUSE,
        )) {
            assertEquals(
                "键 $k",
                ListenKeys.Action.RESUME,
                ListenKeys.actionFor(k, running = true, userPaused = true),
            )
        }
    }

    @Test
    fun `停止键就是停掉整个听写`() {
        assertEquals(
            ListenKeys.Action.STOP,
            ListenKeys.actionFor(KeyEvent.KEYCODE_MEDIA_STOP, running = true, userPaused = false),
        )
        assertEquals(
            ListenKeys.Action.STOP,
            ListenKeys.actionFor(KeyEvent.KEYCODE_MEDIA_STOP, running = true, userPaused = true),
        )
    }

    @Test
    fun `别的键一律忽略`() {
        assertNull(ListenKeys.actionFor(KeyEvent.KEYCODE_VOLUME_UP, running = true, userPaused = false))
        assertNull(ListenKeys.actionFor(KeyEvent.KEYCODE_BACK, running = true, userPaused = false))
    }
}
