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
}
