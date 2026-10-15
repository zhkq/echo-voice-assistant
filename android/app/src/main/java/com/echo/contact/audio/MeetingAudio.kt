package com.echo.contact.audio

/**
 * 会议音频的**分段导航**（纯逻辑，可 JVM 单测）。
 *
 * 为什么要有它：会议录音在主机上是**按段存的**（`01.flac`、`02.flac`…），
 * 后端的 `GET /api/meetings/{id}/audio?seg=N` 一次只给一段（1 起、还支持 Range）。
 * 手机上"听一段"是合理的事，"把一小时会议从头播到尾"不是（那是主机上的事）——
 * 所以这里只做"第几段 / 上一段 / 下一段"，并把边界钉住。
 *
 * 两个刻意的取舍：
 *   * 到边界**夹住，不循环**：循环会让人以为"这里还没完"，把一段听成两遍；
 *   * 段号从 **1** 起（与后端 `{seg:02d}` 的文件命名一致），内部不换算。
 */
object MeetingAudio {

    /** 段号是否落在 `1..total` 里。 */
    fun valid(seg: Int, total: Int): Boolean = total > 0 && seg in 1..total

    /** 下一段（到末段就停在末段）。 */
    fun next(seg: Int, total: Int): Int = if (total <= 0) 0 else (seg + 1).coerceAtMost(total)

    /** 上一段（到首段就停在首段）。 */
    fun prev(seg: Int, total: Int): Int = if (total <= 0) 0 else (seg - 1).coerceAtLeast(1)

    /** 给界面看的标题："第 3/12 段"；不合法时如实说（别显示"第 0/0 段"）。 */
    fun label(seg: Int, total: Int): String = when {
        total <= 0 -> "这场会议没有可播放的分段"
        !valid(seg, total) -> "第 $seg 段超出范围（共 $total 段）"
        else -> "第 $seg/$total 段"
    }

    /** 缓存文件名：同一场会议的同一段复用本地文件，免得每次按"下一段"都重下。 */
    fun cacheName(mid: Int, seg: Int): String = "meeting-$mid-seg$seg.wav"
}
