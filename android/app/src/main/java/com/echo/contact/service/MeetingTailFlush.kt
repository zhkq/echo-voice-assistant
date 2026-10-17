package com.echo.contact.service

import java.util.Locale

/**
 * 结束录音时**最后不足一段**的收尾（纯逻辑，JVM 逐条可测）。
 *
 * ## 这次修的是一次真实的退步（用户 2026-10-17 真机要验的第一个场景）
 *
 * 段长默认 600 秒 = 10 分钟（`AppPrefs.segmentSeconds`，是**用户设置**，15/30/60/300/600）。以前"结束录音"
 * 只做 `session.finish()`：**还攒在缓冲里的那部分样本被直接丢掉**。于是
 * "**录 20 秒就结束**"（用户真机要验的就是这个）既不封段、也不建会、也不上传，
 * 整段录音凭空消失；而旧版"电脑麦"那条路是能录的，所以这是一次**真实的退步**。
 *
 * ## 口径（三条，缺一不可）
 *
 *  1. **有音频就必须留下**：≥ 1 个样本就封成一段（时长按**真的字节数**算）；
 *     **只有真的一个样本都没有**（一按就停 / 立刻失败 / 没给权限）才允许不建会；
 *  2. **补齐尾段 ≠ 改段长**：段长仍然是用户设置的那个（默认 600 秒 = 10 分钟），
 *     这里只在**结束时**把尾巴补成一段 —— 正常录音的段边界一个都不动；
 *  3. **暂停中结束照样封**：暂停期间写进去的是零样本（用户 2026-10-16 深夜的口径：
 *     *"暂停区间就是空白录音，不影响会议总时长"*），它们**照样算时长**、照样封段
 *     —— 尾段里对应的那段内容就是空白。
 *
 * ## 为什么不设一个"太短就扔掉"的下限
 *
 * [VERY_SHORT_SECONDS] 只用于**文案**（"很短的一段也留下了"），**不参与取舍**：
 * 0.1 秒也是用户真的录到的声音，扔掉它就违背了"有音频就必须留下"。
 * 唯一允许不留下的是"真的一个样本都没有"。
 *
 * ## 进程被杀时做不到这件事（如实说，绝不谎报）
 *
 * 这个函数只在**用户按结束**那条路上被调用。进程被系统杀掉时没有机会跑它 ——
 * 那不足一段的缓冲**那一小段是真的丢了**。所以补传/收尾的文案里必须如实说一句
 * （见 `OfflineMeetingSync.inferredEndNote`），并且**不许**因此说"已上传"。
 */
internal object TailFlush {

    /**
     * 0.5 秒：**低于它算"极短"**（只影响文案），但**照样留下**。
     *
     * 用户给的口径是"只要 ≥ 某个极短阈值（比如 0.5 秒）就必须落成一段 + 建会"。
     * 这里把门槛再收紧到"**有样本就落**"：判据只有 0 与 >0 两档 ——
     * 0.3 秒同样是用户录到的内容，没有理由丢。
     */
    const val VERY_SHORT_SECONDS = 0.5

    /** 这些样本算不算"有音频"（**唯一**的取舍判据：0 才算没有）。 */
    fun hasAudio(samples: Int): Boolean = samples > 0

    /** 样本数 → 秒数（按采样率算，**不拿设置里的段长当答案**）。 */
    fun secondsOf(samples: Int, sampleRate: Int): Double {
        if (samples <= 0 || sampleRate <= 0) return 0.0
        return samples.toDouble() / sampleRate
    }

    /** 低于 [VERY_SHORT_SECONDS] 的尾段（**只影响文案**，不影响要不要留下）。 */
    fun isVeryShort(samples: Int, sampleRate: Int): Boolean =
        hasAudio(samples) && secondsOf(samples, sampleRate) < VERY_SHORT_SECONDS

    /** 那一小段秒数的显示（一位小数；固定用 `.`，文案/日志里要稳定）。 */
    fun secondsText(samples: Int, sampleRate: Int): String =
        String.format(Locale.US, "%.1f", secondsOf(samples, sampleRate))
}

/**
 * 尾段封存的结局（[samples] / [seconds] 是**缓冲里真的有多少**，不是设置里的段长）。
 */
internal data class TailSealOutcome(
    val samples: Int,
    val seconds: Double,
    /** 真的封下去的那一段（null = 没有音频，或没有这一场）。 */
    val seal: SegmentSeal? = null,
    /** 这一段是**结束时代替"攒满一段"补出来的**（日志/文案据此说明）。 */
    val isTail: Boolean = false,
    /** 如实的一句话（空 = 没什么要说的）。 */
    val note: String = "",
    /** 真的可能丢音频（编码/落盘/写清单失败）。 */
    val error: String = "",
) {
    /** 一个样本都没有：**允许不建会**，但必须如实说（绝不静默）。 */
    val noAudio: Boolean get() = samples <= 0
}

/**
 * 把缓冲里最后不足一段的样本**封成一段**（本地落盘 + 记清单，不碰网络）。
 *
 * 调用点只有一处：`MeetingMicService.stopEverything`，且在 `session.finish()` **之前**。
 * 顺序不能反：`finish()` 要写 `ended_at` 与总时长，而总时长必须包含这一段尾音频。
 *
 * [samples] 是缓冲里剩下的样本（**不是** PCM 字节）；空 = 没有尾段。
 */
internal fun sealFinalTail(
    session: MeetingRecordSession?,
    samples: List<Short>,
    sampleRate: Int,
): TailSealOutcome = sealFinalTail(session, samples.toShortArray(), sampleRate)

/**
 * [ShortArray] 版本（生产路径）：**不装箱**。
 *
 * 为什么要有它：段长默认 600 秒，尾段最多也有 960 万个样本；`List<Short>` 那条会
 * 装箱 960 万次（≈150 MB 临时对象）。生产代码拿 `PcmBuffer.toShortArray()` 调这里，
 * 测试用小的 `List` 调上面那个重载 —— 语义完全一样（同一个实现）。
 */
internal fun sealFinalTail(
    session: MeetingRecordSession?,
    samples: ShortArray,
    sampleRate: Int,
): TailSealOutcome {
    val count = samples.size
    val seconds = TailFlush.secondsOf(count, sampleRate)
    if (session == null) return TailSealOutcome(count, seconds)
    if (!TailFlush.hasAudio(count)) {
        // 一个样本都没有：不建会（没有音频就没有这场会），但原因必须能说出来
        return TailSealOutcome(0, 0.0, note = "这次没有录到音频")
    }
    return try {
        val seal = session.seal(toPcmBytes(samples))
        val short = if (TailFlush.isVeryShort(count, sampleRate)) "（很短）" else ""
        TailSealOutcome(
            samples = count,
            seconds = seconds,
            seal = seal,
            isTail = true,
            // ⚠️ 落盘失败时**不许**说"留下了"（那只在这一档是假话，见下面的 error）
            note = if (seal.error.isBlank())
                "最后不足一段的 ${TailFlush.secondsText(count, sampleRate)} 秒$short" +
                    "也留下了（第 ${seal.seq} 段）"
            else "",
            error = seal.error,
        )
    } catch (e: Exception) {
        // 连封段都抛了：**绝不假装留下**（这是真的可能丢音频）
        TailSealOutcome(
            samples = count,
            seconds = seconds,
            isTail = true,
            error = "最后不足一段的音频没封存下来（可能真的丢了）：${e.message}",
        )
    }
}

/**
 * 结束录音时**必须说给人听的那句话**（纯函数，JVM 可测）。
 *
 * 三种情形，优先级不许换：
 *  1. **尾段真的没封存下来**（可能丢音频）—— 这是最要紧的事实，不能被"没有音频"盖掉；
 *  2. 这一场**一个样本都没有**（一按就停 / 立刻失败）→ 不建会，但要**如实说**；
 *  3. 其余情况没什么要说的（空串 —— 别用套话占住界面上那一行）。
 *
 * 为什么这条不能塞进 `syncNote`：`syncNote` 是**补传那一轮**的状态，前台 tick 每几秒
 * 就会按真实进度重写它；而这句话是"结束那一刻的事实"，被下一轮 tick 抹掉就是**静默**
 * （用户会以为录上了，只是还没转写）。所以它落在 `MicState.stopNote` 这个单独的字段里。
 */
internal fun tailStopNote(
    tail: TailSealOutcome,
    totalSegments: Int,
    hadSession: Boolean,
): String = when {
    tail.error.isNotBlank() -> tail.error
    hadSession && totalSegments == 0 ->
        "这次没有录到音频：一个样本都没录到，所以没有建会议，也不会有转写"
    else -> ""
}
