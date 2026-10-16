package com.echo.contact.service

import java.io.File
import java.time.LocalDateTime

/**
 * **本地音频的保留策略**（有界保留；纯函数，JVM 逐条可测）。
 *
 * 用户 2026-10-16 的两句口径把这里夹在中间：
 *  * *"录音文件缓存在 app，网络通了就可以传上去"* —— 传之前**一个字节都不许删**；
 *  * 而"永不删"是**上一版**的做法（安全，但手机会被塞满：一段 ≈1 MB，一小时 ≈120 MB，
 *    连着几场就把手机挤爆了）。
 *
 * ## 铁律：**绝不早删**
 *
 * 允许清理的**唯一**条件是：
 * ```
 * 对账通过（draft.uploaded） **且** POST /api/meeting/stop 已经成功（见 stopped 的说明）
 *  **且** 距离`结束时间`已经超过保留天数
 * ```
 * 三条是**与**关系，任何一条不成立就一个音频文件都不动：
 *  * 没对账通过 = 音频还没上去（删了就是真丢）；
 *  * 没收尾 = 服务端还没开始转写（这一版先要求 `uploaded` 即可 —— 见
 *    [OfflineMeetingRetention.plan] 的参数说明）；
 *  * 没到期 = 用户可能还要重听/补传（"宁可多留一会儿也别丢音频"）。
 *
 * ## 为什么它是纯函数
 *
 * 它决定"删哪些文件" —— 这是这条路上**唯一不可逆**的动作。判断写错的话，
 * 表现是静默的（用户几天后才发现某场会没有音频），而且真机上没法回放验证。
 * 所以判据全部收在这里：输入是（清单 + 现在时刻 + 保留天数），输出是"该删哪些"，
 * 中间不许读硬盘、不许读设置、不许看时钟。
 */

/** 一条"可以清掉的本地音频"（界面据此显示"删的是什么"，不许只显示一个数字）。 */
data class RetentionItem(
    /** 清单（含开始/结束时间与段数 —— 界面显示的就是它）。 */
    val draft: MeetingDraft,
    /** 这一份能清出多少字节（= 它目录里那些段文件的大小之和）。 */
    val bytes: Long,
    /** 清的理由（如实：`"结束于 2026-10-09 20:33，已超过 7 天"`）。 */
    val why: String,
) {
    /** 界面上"删的是什么"的第一行（客户端 id + 时间 + 段数 + 大小）。 */
    val label: String get() = RetentionPlan.itemLabel(this)
}

/** 一次清理的**计划**（纯数据：还没动任何文件）。 */
data class RetentionPlan(
    /** 该清的（按结束时间升序 = 最早的最先清）。 */
    val items: List<RetentionItem>,
    /** 有音频、但**还不到期**（或还没传完）的那些份数。 */
    val kept: Int,
    /** 这次能清出多少字节。 */
    val bytes: Long,
) {
    val count: Int get() = items.size
    val nothingToDo: Boolean get() = items.isEmpty()

    companion object {
        /**
         * 一条的说明文字（**唯一**拼法：设置页与用例共用，免得两处说法不一致）。
         *
         * 为什么要把 clientId 与开始时间都写上：删除是**不可逆**的，用户点之前必须
         * 看得出"要删的是哪一场"（只说"3 场、42 MB"等于什么都没说）。
         */
        fun itemLabel(item: RetentionItem): String = buildString {
            val d = item.draft
            append(d.startedAt.ifBlank { "时间不明" })
            append(" · ").append(d.segments.size).append(" 段")
            append(" · ").append(humanBytes(item.bytes))
            if (d.endedAt.isNotBlank()) append(" · 结束于 ").append(d.endedAt)
        }

        /** 字节 → 人话（设置页与对话框都用它；**不引 Android 的 Formatter**，那没法在 JVM 里测）。 */
        fun humanBytes(bytes: Long): String {
            val b = if (bytes < 0) 0L else bytes
            return when {
                b >= 1024L * 1024 * 1024 ->
                    String.format("%.2f GB", b / 1024.0 / 1024.0 / 1024.0)
                b >= 1024L * 1024 -> String.format("%.1f MB", b / 1024.0 / 1024.0)
                b >= 1024L -> String.format("%.0f KB", b / 1024.0)
                else -> "$b B"
            }
        }
    }
}

object OfflineMeetingRetention {

    /**
     * 默认保留天数（**7 天**）。
     *
     * 为什么是 7：补传最迟在"网络恢复"那一刻发生（小时级），而"7 天"是给
     * **人**留的缓冲 —— 用户可能想回头核对某场会的音频，也可能发现转写不对想重传。
     * 再长（例如 30 天）对手机会有实际压力：一小时会议 ≈120 MB，7 天最多也就几百 MB；
     * 而服务端那份音频**一直都在**（清本地副本不等于丢会议）。
     */
    const val DEFAULT_RETENTION_DAYS = 7

    /**
     * 可选档位（界面上是单选；`0` = **永不自动清**）。
     *
     * 为什么留一个 `0`：有人就是要"手机上一直留着"。而 `0` 的语义必须写死成
     * **"不清理"**（不是"立刻清"）—— 见 [isEligible]："永远不清"和"立刻清"
     * 搞反了会当场删光用户的音频。
     */
    val RETENTION_DAY_CHOICES = intArrayOf(0, 1, 7, 14, 30)

    /** 天数 → 人话（界面与文案共用；`0` 那档必须说"永不自动清理"）。 */
    fun label(days: Int): String = when {
        days <= 0 -> "永不自动清理"
        days == 1 -> "保留 1 天"
        else -> "保留 $days 天"
    }

    /**
     * 把库里那个天数**夹到最近的一档**（与 `AppPrefs.normalizeSegmentSeconds` 同一个套路）。
     *
     * 为什么是"夹到最近的档"而不是"夹到上下界"：库里可能躺着 3（手改过/旧版本写过），
     * 夹到下界 0 = **永不清**（与用户意图相反），夹到上界 30 = 多占一个月。
     * 距离平手时取**小的那一档**（档位表升序，`minByOrNull` 返回第一个最小值）——
     * 平手时倾向"少删一点"，这与"绝不早删"是同一个方向。
     */
    fun normalizeDays(raw: Int): Int =
        RETENTION_DAY_CHOICES.minByOrNull { kotlin.math.abs(it - raw) } ?: DEFAULT_RETENTION_DAYS

    /** 这一档是不是"永不自动清理"。 */
    fun neverCleans(days: Int): Boolean = days <= 0

    /**
     * 一份清单**允不允许**被清（纯函数；三条与关系）。
     *
     * [stopped] 是"服务端已经收尾（转写已经开始）"。⚠️ 当前调用方传的是
     * `draft.uploaded`（对账通过）—— 保留策略**要求**的是"传完并且服务端收尾"，
     * 所以这里把两件事分开写、由调用方明确表态；不清楚时就传 false
     * （**多留一份永远比删错一份便宜**）。
     */
    fun isEligible(draft: MeetingDraft, nowMs: Long, days: Int, stopped: Boolean): Boolean {
        if (neverCleans(days)) return false
        if (!draft.uploaded) return false          // 没传上去 → 一个字节都不许动
        if (!stopped) return false                 // 没收尾（没转写）→ 也不动
        if (draft.recording) return false          // 居然还在录 → 绝不能清
        if (draft.segments.isEmpty()) return false // 没有音频 → 没什么可清（也别显示成一场）
        val end = endedAtMs(draft) ?: return false // 读不懂时间 → 不清（宁可多留）
        return nowMs >= end + days.toLong() * DAY_MS
    }

    /**
     * 该删哪些（**纯函数**：清单 + 现在时刻 + 保留天数 → 清理计划）。
     *
     * 判据只有 [isEligible] 一处（"绝不早删"那条铁律）；这里只负责把
     * "该清哪些、能省多少"算出来。**不碰硬盘** —— 真删是 [MeetingDraftStore.purgeAudio]。
     *
     * [stopped] 的语义见 [isEligible]。调用方（`MeetingMicService`）用
     * `draft.uploaded` 当它的判据：`uploaded` 就是"对账通过 **且** stop 成功"之后
     * 才被写上的那个标记（见 `OfflineMeetingSync.syncOne` 的第 ④ 步）。
     */
    fun plan(
        drafts: List<MeetingDraft>,
        nowMs: Long,
        days: Int,
        stopped: (MeetingDraft) -> Boolean = { it.uploaded },
        sizeOf: (MeetingDraft) -> Long = { 0L },
    ): RetentionPlan {
        val eligible = drafts.filter { isEligible(it, nowMs, days, stopped(it)) }
            .sortedWith(compareBy({ endedAtMs(it) ?: Long.MAX_VALUE }, { it.clientId }))
            .map { d ->
                RetentionItem(
                    draft = d,
                    bytes = maxOf(0L, sizeOf(d)),
                    why = whyText(d, nowMs, days),
                )
            }
        return RetentionPlan(
            items = eligible,
            kept = drafts.count { it.segments.isNotEmpty() } - eligible.size,
            bytes = eligible.sumOf { it.bytes },
        )
    }

    /** "为什么它可以清了"的**如实**说法（删之前给用户看的那句话）。 */
    fun whyText(draft: MeetingDraft, nowMs: Long, days: Int): String {
        val end = endedAtMs(draft)
        val ended = if (end == null) "结束时间读不懂"
        else "结束于 ${MeetingDraftStore.isoOf(end)}"
        val age = if (end == null) 0L else maxOf(0L, (nowMs - end) / DAY_MS)
        return "$ended，已过 $age 天（保留 $days 天）：已对账通过并已让服务端收尾"
    }

    /** 这一份能清出多少字节（**按清单里真的有的那些段文件算**；不数目录里多出来的东西）。 */
    fun audioBytes(store: MeetingDraftStore, draft: MeetingDraft): Long =
        draft.segments.sumOf { seg ->
            val f: File = store.segmentFile(draft.clientId, seg.seq)
            try {
                if (f.isFile) f.length() else 0L
            } catch (e: Exception) {
                0L
            }
        }

    /** 所有清单的本地音频总量（设置页"当前占用"那一行）。 */
    fun totalAudioBytes(store: MeetingDraftStore, drafts: List<MeetingDraft>): Long =
        drafts.sumOf { audioBytes(store, it) }

    /** 清单的**结束时刻**（epoch ms）：`ended_at` 优先，它读不出来时退到"最后一段的结束时刻"。 */
    fun endedAtMs(draft: MeetingDraft): Long? {
        MeetingDraftStore.epochOf(draft.endedAt)?.let { return it }
        // `ended_at` 读不懂（空/写坏）：用最后一段的 开始+时长 兜底，**不编 0**（那是 1970 年，
        // 会让"已经过了 20000 天"这种荒唐结论直接放行）
        val last = draft.segments.maxByOrNull { it.seq } ?: return null
        val start = MeetingDraftStore.epochOf(last.startedAt)
            ?: MeetingDraftStore.epochOf(draft.startedAt) ?: return null
        return start + (last.durationSeconds * 1000).toLong()
    }

    /**
     * 设置页/通知上那句"本地还留着多久"（**纯函数**）。
     *
     * 用户 2026-10-16 点名要的如实文案：**"已上传，本地还留着 N 天的备份"** ——
     * 不能只说"已上传"（那会让人以为本地已经没了，于是不敢清、或以为手机不会满）。
     */
    fun retentionNote(days: Int): String = if (neverCleans(days)) {
        "已上传，本地还有一份备份（设置里选的是「永不自动清理」）"
    } else {
        "已上传，本地还留着 $days 天的备份（到期后会自动清）"
    }

    /** ISO 时间字符串 → 可读的"日期 时间"（删之前给人看的那一句里用）。 */
    fun readableAt(iso: String): String = try {
        if (iso.isBlank()) "" else LocalDateTime.parse(iso, MeetingDraftStore.ISO).toString()
    } catch (e: Exception) {
        iso
    }

    private const val DAY_MS = 24L * 60 * 60 * 1000
}

/** 顶层别名：设置页那行文案（读起来像一句话）。 */
internal fun audioRetentionNote(days: Int): String = OfflineMeetingRetention.retentionNote(days)

/** 顶层别名：本地音频占用的人话（设置页那一行）。 */
internal fun humanAudioBytes(bytes: Long): String = RetentionPlan.humanBytes(bytes)
