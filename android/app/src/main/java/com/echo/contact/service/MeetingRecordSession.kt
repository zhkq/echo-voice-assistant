package com.echo.contact.service

import com.echo.contact.audio.EncodedAudio
import com.echo.contact.audio.SegmentAudioEncoder
import com.echo.contact.audio.SegmentEncoding
import com.echo.contact.audio.SegmentFormat

/**
 * **本地先写**那一半的全部动作（纯逻辑：给一个清单目录 + 一个编码器就能在 JVM 里跑）。
 *
 * ## 为什么"本地先写"是这条路的本体（用户 2026-10-17 拍板的单路）
 *
 * 用户原话：*"离线录音和在线录音就没什么本质区别了"*。于是开始录音**不再问服务端**
 * （不探活、不建会、不看网络），只做一件事：把这一场在本地立起来，然后**每一段都先落盘**
 * （`filesDir/offline-meetings/<clientId>/seg-%04d.<flac|wav>` + 清单）。
 * 传上去是**后面**的事（`OfflineMeetingSync`），而且传不上去也不影响录音。
 *
 * 为什么这样更安全（这是"为什么单路比双路安全"的正面理由）：
 *  * **双路**里"在线那条"把音频**先发给服务端、本地不留** —— 于是网络的每一次抖动都可能
 *    把一段音频变成"只存在于网络上"的状态，而手机这边没有任何副本可以重来；
 *  * 双路还有两个队列（在线失败的段放 `cacheDir/meeting-mic`、离线那一场放 `filesDir/offline-meetings`），
 *    两套账、两个恢复路径，谁先谁后全靠调用方记得 —— 这正是"录到一半网断了"最容易出错的形状；
 *  * **单路**只有一份音频（本地），而且它是**先落地再上传**：任何时刻把电拔了，
 *    已经封存的那几段都在磁盘上、清单里都记着格式与顺序。上传只是一个**幂等的可重试动作**。
 *
 * ## 它不干什么
 *
 * 不碰网络（一个请求都不发）、不读设置、不看时钟（除了注入进来的 [clock]）。
 * 于是"清单里的格式/段号/时间轴对不对"这些**只能靠真机验**的事，在这里都能用用例钉住。
 */
class MeetingRecordSession(
    private val store: MeetingDraftStore,
    private val encoder: SegmentAudioEncoder,
    /** PCM 的参数（算时长用：时长必须由**真的字节数**推出来，不是拿设置里的段长当答案）。 */
    private val sampleRate: Int = 16_000,
    private val channelCount: Int = 1,
    /** 现在几点（注入是为了用例能造出确定的暂停区间/时间轴）。 */
    private val clock: () -> Long = { System.currentTimeMillis() },
) {

    @Volatile
    var draft: MeetingDraft? = null
        private set

    /** 暂停区间的起点（相对开始时间的秒）；null = 没在暂停。 */
    @Volatile
    private var pauseFrom: Double? = null

    val clientId: String? get() = draft?.clientId

    val paused: Boolean get() = pauseFrom != null

    /**
     * 开始这一场：**只动本地**。有清单就接着它录（App 重开能接着录的实现）。
     *
     * 返回 [SessionStart.note] 非空时表示"上一份清单读不懂、另起了一份"这类**如实**的降级说明。
     */
    fun start(
        clientId: String,
        startedAt: String = MeetingDraftStore.isoOf(clock()),
    ): SessionStart {
        val load = store.loadOrCreate(clientId, startedAt)
        val d = when (load) {
            is DraftLoad.Ok -> load.draft
            // 上一份清单读不懂：**不覆盖**它，另起一份（原因照实带出去给界面）
            is DraftLoad.Recovered -> load.draft
        }
        draft = d
        // 开始就把清单写下来：进程在这一刻被杀，也留得下"这一场开始了"
        store.save(d)
        ActiveDraft.set(d)
        return SessionStart(
            draft = d,
            note = when (load) {
                is DraftLoad.Recovered -> load.note
                is DraftLoad.Ok -> ""
            },
        )
    }

    /**
     * 封存一段：**编码 → 落盘 → 记进清单**（不碰网络，绝不移交给别人）。
     *
     * 三件事的顺序不许换：
     *  1. **先编码再决定文件名**（`seg-%04d.flac` 还是 `.wav`）—— 文件名必须跟着**真的**格式走，
     *     否则清单里那份格式与磁盘上的文件名会互相矛盾（人肉排障时最先看的就是文件名）；
     *  2. **先落音频再写清单**：清单里写了一条"第 N 段在 seg-000N.flac"，而那个文件不存在，
     *     补传就会卡在这一段（`第 N 段的音频不在手机里`），而且下一段还会继续往前排 ——
     *     整场录音就断了。反过来（文件在、清单还没写）最多是"这一段暂时没被记账"，
     *    不会让后面的段错位；
     *  3. **清单落盘失败要如实说**：硬盘满了/权限坏了是真会丢音频的事。
     */
    fun seal(pcm: ByteArray): SegmentSeal {
        val d = base()
            ?: return SegmentSeal(null, error = "本地清单丢了，这一段没地方存（真的可能丢音频）")

        // ① 编码：想编 FLAC，编不出来**退回 WAV**（绝不丢音频、绝不谎报格式 —— 见 SegmentEncoding）
        val enc = encoder.encode(pcm)

        // 段序号接着**水位**往下排（不是"本地最大序号 + 1"）：水位是"服务端已认下到第几段"，
        // 它一定 >= 本地已有的最大序号，取两者的大值才不会造出重号（重号 = 服务端按
        // (clientId, seq) 幂等，会把新的一段当成旧的丢掉 —— 那一段音频就永远上不去了）
        val seq = maxOf(d.segments.maxOfOrNull { it.seq } ?: 0, d.acknowledgedSegments) + 1
        val now = clock()
        val sealedAt = offsetSeconds(d.startedAt, now)
        val seconds = secondsOf(pcm)
        // 这一段的**起点**是"封存那一刻减去它自己的长度"：封存发生在这一段录完之后，
        // 所以 `sealedAt` 是它的**结束**时刻。老代码把起点也记成 `sealedAt`
        // （于是每一段的起点都晚了一个段长、总时长也多算一个段长），那是错的 ——
        // 会议的段边界（"第 N 段 = 挂钟第 N 个段长"）一旦错位，转写与音频就对不上了。
        val startOffset = maxOf(0.0, sealedAt - seconds)
        val name = MeetingDraftStore.segmentName(seq, enc.format)

        return try {
            val f = store.segmentFile(d.clientId, seq, enc.format)
            f.parentFile?.mkdirs()
            f.writeBytes(enc.bytes)          // ② 先落音频
            val seg = DraftSegment(
                seq = seq,
                file = name,
                startedAt = MeetingDraftStore.isoOf(now),
                startOffsetSeconds = startOffset,
                durationSeconds = seconds,
                format = enc.format,         // **真的**那一份（清单里就是发出去时用的那一份）
            )
            val updated = d.copy(
                segments = d.segments + seg,
                durationSeconds = maxOf(d.durationSeconds, startOffset + seconds),
            )
            draft = updated
            ActiveDraft.set(updated)
            val saved = store.save(updated)  // ③ 再写清单
            val note = formatNoteOf(seq, enc, updated)
            if (!saved) {
                return SegmentSeal(updated, seq, enc.format, note,
                    error = "第 $seq 段存下来了，但清单没写进去（重启后可能对不上账）")
            }
            SegmentSeal(updated, seq, enc.format, note)
        } catch (e: Exception) {
            // 连音频都写不进去：如实报出来（这是真的丢音频）。
            // ⚠️ 这时**不**报"这段用 WAV"那句格式话：那一段根本没落下来，
            // 说格式等于把"没录到"说成"录到了、只是格式不同"。
            SegmentSeal(d, seq, enc.format, "", error = "有一段缓存不下来，可能真的丢了：${e.message}")
        }
    }

    /**
     * 这一份清单的**最新事实**：磁盘上那份优先。
     *
     * ## 为什么每次改动前都要重新读一遍（这一条错过一次，表现是"重复上传"）
     *
     * 这条路上有**两个**写者：录音这一侧（本类，加段/加暂停区间/写结束时间）与同步那一侧
     * （`OfflineMeetingSync`，推进 `acknowledged_segments` 水位、记 `server_meeting_id`、
     * 收尾后标 `uploaded`）。如果本类只拿内存里的那份当底稿，它就会把**同步刚推进的水位**
     * 一起写回去（内存里那还是旧值）—— 后果是下一轮同步以为"前面那些段还没传"，
     * 把已经确认过的段**再传一遍**（服务端虽然按 `(clientId, seq)` 幂等挡掉了重复存，
     * 但白耗流量；更糟的是水位来回退会让"传完了没有"这件事永远说不准）。
     *
     * 磁盘上那份是**真的**（两边都往它写、而且都是原子写），所以以它为准、内存只作兜底。
     */
    private fun base(): MeetingDraft? {
        val mem = draft ?: return null
        val onDisk = store.load(mem.clientId)
        if (onDisk != null) draft = onDisk
        return onDisk ?: mem
    }

    /**
     * 开始一段暂停（用户按了静音）：记下**相对开始时间**的秒数。
     *
     * 幂等：已经在暂停中就什么都不做（用户可能连按两下静音）。
     */
    fun beginPause(): Double? {
        val d = base() ?: return null
        if (pauseFrom != null) return pauseFrom
        val at = offsetSeconds(d.startedAt, clock())
        pauseFrom = at
        return at
    }

    /**
     * 结束这段暂停（用户按了恢复，或**直接结束了录音**）：收一条区间并落盘。
     *
     * 为什么"结束时还静音着"也要记一条：那段静音**确实是暂停**（这段时间没采到音），
     * 不记的话会议里就会凭空多出一段"没人说话"的时长，而不是标着"被暂停过"。
     */
    fun endPause(): DraftPause? {
        val from = pauseFrom ?: return null
        val d = base() ?: return null
        pauseFrom = null
        val to = maxOf(from, offsetSeconds(d.startedAt, clock()))
        val pause = DraftPause(from = from, to = to)
        val updated = d.copy(pauses = d.pauses + pause)
        draft = updated
        ActiveDraft.set(updated)
        store.save(updated)
        return pause
    }

    /**
     * 录完了：写结束时间与总时长，落盘。
     *
     * 为什么结束也必须落盘：`ended_at` 是补传时要交给服务端的（会议时长/时间轴靠它），
     * 而"结束"这一下只改了内存的话，进程一被杀这场会就**永远停在"还在录"** ——
     * 而"还在录"的那一份在这条路上会被**拒绝收尾**（见 `SyncScope`），
     * 于是它永远传不完、永远不出文字。
     */
    fun finish(): MeetingDraft? {
        val d = base() ?: return null
        val now = clock()
        // 结束时还静音着：先把那段暂停收进清单（否则总时长/区间会与音频对不上）
        endPause()
        val cur = base() ?: d
        val done = cur.copy(
            endedAt = MeetingDraftStore.isoOf(now),
            durationSeconds = maxOf(cur.durationSeconds, offsetSeconds(cur.startedAt, now)),
        )
        draft = done
        ActiveDraft.set(done)
        store.save(done)
        return done
    }

    /** 这一段 PCM 的**真实**时长（秒）：按字节数算，不拿设置里的段长当答案。 */
    private fun secondsOf(pcm: ByteArray): Double {
        val bytesPerSecond = sampleRate * channelCount * 2      // 16 bit
        if (bytesPerSecond <= 0) return 0.0
        return pcm.size.toDouble() / bytesPerSecond
    }

    private fun formatNoteOf(seq: Int, enc: EncodedAudio, d: MeetingDraft): String =
        segmentFormatNote(
            seq = seq,
            format = enc.format,
            reason = enc.note.ifBlank { SegmentEncoding.FLAC_UNAVAILABLE },
            wavSegments = d.segments.count { it.format == SegmentFormat.WAV },
        )
}

/** [MeetingRecordSession.start] 的结局（`note` 非空 = 有如实要说的降级）。 */
data class SessionStart(val draft: MeetingDraft, val note: String = "")

/**
 * 一段封存的结局。
 *
 * [format] 是**真的**写下去的那一份；[note] 是"这段用 WAV（FLAC 编码器不可用）"那句如实话；
 * [error] 非空 = 真的可能丢音频（调用方必须原样报出来）。
 */
data class SegmentSeal(
    val draft: MeetingDraft?,
    val seq: Int = 0,
    val format: SegmentFormat = SegmentFormat.WAV,
    /** 退回 WAV 时的如实说明（编成 FLAC 时为空 —— 别用套话占住界面那一行）。 */
    val note: String = "",
    /** 落盘/写清单失败（**这是真的可能丢音频**）。 */
    val error: String = "",
)
