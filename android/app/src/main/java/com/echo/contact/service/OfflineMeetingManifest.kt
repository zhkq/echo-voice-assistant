package com.echo.contact.service

import java.io.File
import java.nio.charset.StandardCharsets
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter

/**
 * **离线会议录音的清单**（manifest）—— 用户 2026-10-16 的需求：
 * *"手机如果连不上 echo 也可以进入离线状态，离线状态可以开始录音，结束录音，
 * 录音文件缓存在 app，网络通了就可以传上去"*。
 *
 * ## 为什么要有这一份清单（而不是"看着目录里有哪些 wav"）
 *
 * 补传时必须回答三个**只有清单才能回答**的问题：
 *  1. 这些段属于**哪一场**会议（目录里可能有几场离线录音等着传）；
 *  2. 它们的**顺序**与**起止时间**（音频的先后顺序就是内容本身；开始时间是
 *     会议在电脑上的 `startedAt`，必须用**当时**的时间，不是补传时的时间）；
 *  3. 哪几段**已经被服务端认下**（可续传；已确认的段跳过，不再重传）。
 *
 * 还有一个不写在音频里的东西：**客户端生成的稳定 id**（[MeetingDraft.clientId]）。
 * 补传时把同一个 id 反复交给服务端，服务端只要按它幂等，就**不会建出两场会议**、
 * 也不会把同一段存两遍（见 [MeetingDraftStore] 与 `OfflineMeetingSync` 的说明）。
 *
 * ## 为什么手写 JSON 而不引 Gson
 *
 * 这一份刻意**不依赖 Android、也不依赖 Gson**：它是"断网时唯一的事实来源"，
 * 值得能在 JVM 单测里逐字读写的**纯逻辑**（序列化往返、损坏清单降级都有判据）。
 * 键名用下划线是给**服务端**看的（Lead 会把这份契约派给 PC 侧），
 * 而 [`com.echo.contact.service.OfflineMeetingJson`] 是唯一写法，别在别处拼字符串。
 */

/** 录音来源：目前只有"手机麦"这一档会离线录。 */
object MeetingSource {
    const val PHONE = "phone"
}

/**
 * 一份离线录音的清单。
 *
 * [serverMeetingId] 在补传时由服务端给出（`0` = 还没建/还没回来）。它**不是**幂等键 ——
 * 幂等键是 [clientId]（客户端自己生成的稳定 id）。
 */
data class MeetingDraft(
    /** 客户端生成的**稳定** id（幂等键；补传多少次都只发这一个）。 */
    val clientId: String,
    /** 开始时间（ISO-8601，**本地**时间，如 `2026-10-16T20:31:02`）。 */
    val startedAt: String,
    /** 来源；这一版只有 `phone`。 */
    val source: String = MeetingSource.PHONE,
    /** 结束时间（ISO，空 = 还在录）。 */
    val endedAt: String = "",
    /** 已录时长（秒，暂停那段不算）。 */
    val durationSeconds: Double = 0.0,
    /** 服务端那一侧的会议 id（0 = 还没建）。 */
    val serverMeetingId: Int = 0,
    /** 暂停区间（每按一次静音/恢复就是一条）。 */
    val pauses: List<DraftPause> = emptyList(),
    val segments: List<DraftSegment> = emptyList(),
    /** 服务端已认下的**最大**段序号（0 = 一段都没认）。 */
    val acknowledgedSegments: Int = 0,
    /** 全部段都被服务端认下、且对账通过。 */
    val uploaded: Boolean = false,
) {
    /** 还在录（没写结束时间）。 */
    val recording: Boolean get() = endedAt.isBlank()

    /** 待补传的段数（未确认的段）。 */
    val pendingSegments: Int get() = segments.count { it.seq > acknowledgedSegments }

    /** 已确认的段数。 */
    val confirmedSegments: Int get() = segments.count { it.seq <= acknowledgedSegments }

    /** 清单是否**自相矛盾/被截断**（损坏清单的降级判据之一）。 */
    fun looksDamaged(): Boolean = segments.any { it.seq <= 0 || it.file.isBlank() }
}

/** 一个音频段。 */
data class DraftSegment(
    /** 段序号，**1 起、连续**（服务端与本地的对账基准）。 */
    val seq: Int,
    val file: String,
    val startedAt: String,
    /** 相对**开始时间**的秒数（不是绝对时间：暂停之后时间轴不能跳）。 */
    val startOffsetSeconds: Double,
    val durationSeconds: Double,
    /** 服务端已认下这一段。 */
    val uploaded: Boolean = false,
)

/**
 * 一次"清本地音频"**真的**干了什么（文件数 + 字节数）。
 *
 * 为什么不是一个 Int：只回"删了几个"的话，界面那句"已清理 42 MB"只能靠**计划里估的数** ——
 * 而计划是删之前算的（文件可能已经不在了）。删完的实话只能由删除动作自己回答。
 */
data class PurgeOutcome(val files: Int, val bytes: Long) {
    val nothingDeleted: Boolean get() = files == 0
}

/** 一条暂停区间（与电脑侧 `meta["pauses"]` 同一个语义：暂停 = 不写盘、不产生音频）。 */
data class DraftPause(
    /** 相对**开始时间**的秒数。 */
    val from: Double,
    val to: Double,
    /** `local` = 手机自己在静音；`remote` = 通过接口让电脑暂停。 */
    val by: String = BY_LOCAL,
) {
    companion object {
        const val BY_LOCAL = "local"
        const val BY_REMOTE = "remote"
    }
}

/**
 * 当前在录的那一场 —— 进程级（和 [AboutToRecord] 同口径）。
 *
 * 为什么要放进程级：采集循环在 `MeetingMicService` 里跑，而界面（全屏录音界面）
 * 与补传逻辑都要知道"现在这一场是哪个 id、录到第几段、确认到第几段"。
 * 复用服务里的 `StateFlow` 做不到这一点 —— 那是给人看的**文案状态**，不是数据。
 */
object ActiveDraft {
    @Volatile var current: MeetingDraft? = null
        private set

    fun set(draft: MeetingDraft?) {
        current = draft
    }

    fun clear() {
        current = null
    }
}

/**
 * 「这一场已经决定**要录**了」的那一次交接（进程级）。
 *
 * `MeetingMicService.start()` 是 `startForegroundService`，**异步**；界面紧接着要打开
 * 全屏录音界面，而全屏界面必须知道"这次录的是哪一种、离线那一场是哪个 id"
 * （否则它只能在服务真的开麦之后才敢显示，用户看到的是"点了没反应"）。
 * 和 `MeetingOpenStore` 同一个套路：写入在主线程、读取在组合里，中间没有异步。
 */
object AboutToRecord {
    @Volatile var plan: RecordPlan? = null
        private set

    fun put(plan: RecordPlan) {
        this.plan = plan
    }

    fun consume(): RecordPlan? {
        val it = plan
        plan = null
        return it
    }

    fun clear() {
        plan = null
    }
}

/**
 * 一次「开始录音」被分到哪条路（**纯数据**，由 `MeetingMicService` 在起服务时算出来）。
 *
 * 用户 2026-10-16 的原话是"手机如果连不上 echo 也可以进入离线状态"——
 * 于是"连不上"不再是失败，而是一个**正当的分支**：离线录音。
 * 界面上那句"ECHO 连不上，已在手机上离线录音"就是从 [offline] 这一档来的。
 */
sealed interface RecordPlan {
    /** 联网那条路（与以前完全一致：先在电脑上开一场"手机麦"会议）。 */
    data class Online(val note: String = "") : RecordPlan

    /** 离线那条路：**完全本地**，先在手机上把清单立起来。 */
    data class Offline(val clientId: String, val reason: String) : RecordPlan
}

/** [MeetingDraftStore.loadOrCreate] 的结局。 */
sealed interface DraftLoad {
    data class Ok(val draft: MeetingDraft) : DraftLoad

    /**
     * 目录里有清单，但**读不动**（被截断/乱码）。此时**不改动**目录里的任何东西，
     * 而新建一份全新的清单（新的 clientId）。宁可多留一份可疑的旧目录，也不要
     * 在损坏的清单上继续写 —— 那会把还能救的音频连同定义一起毁掉。
     */
    data class Recovered(val draft: MeetingDraft, val note: String) : DraftLoad
}

/**
 * 清单的落盘（**纯逻辑**：只依赖 `java.io.File` + [OfflineMeetingJson]，JVM 可测）。
 *
 * ## 三条硬口径
 *
 *  1. **原子写**（先写 `.tmp` 再 `renameTo`）：manifest 是"断网时唯一的事实来源"，
 *     写到一半掉电 = 整场录音失去定义。进程被杀/重启后还能接着录、接着传，靠的就是它。
 *  2. **音频与清单都在 app 私有目录**（调用方传进来的 root 就是 `filesDir/offline-meetings`）：
 *     不依赖服务端、不依赖外置存储权限。离线录音**绝不能**依赖服务端 —— 这正是需求本身。
 *  3. **确认后再删**（默认策略）：对账通过只把清单标成 [MeetingDraft.uploaded]，
 *     **不动音频**（[MeetingDraftStore.retainUploadedAudio] 写明这条并给出显式清理入口）。
 *     宁可多留一会儿，也别丢音频。
 */
class MeetingDraftStore(private val root: File) {

    /** 一份清单的目录。 */
    fun dirOf(clientId: String): File = File(root, safeName(clientId))

    fun manifestFile(clientId: String): File = File(dirOf(clientId), MANIFEST_NAME)

    /** 这一段的音频文件。 */
    fun segmentFile(clientId: String, seq: Int): File =
        File(dirOf(clientId), segmentName(seq))

    /** 这个 id 对应的清单（不存在/读不动就给 null —— **别伪造一份空的**）。 */
    fun load(clientId: String): MeetingDraft? = read(manifestFile(clientId).takeIf { it.isFile })

    /**
     * 磁盘上所有的清单，按**开始时间升序**（补传必须按这个顺序，不然会议顺序会乱）。
     * 读不动的目录**跳过**（降级：别因为一个坏文件让整场补传都不动了）。
     */
    fun list(): List<MeetingDraft> =
        (root.listFiles() ?: emptyArray())
            .filter { it.isDirectory }
            .mapNotNull { d -> read(File(d, MANIFEST_NAME).takeIf { it.isFile }) }
            .sortedWith(compareBy({ it.startedAt }, { it.clientId }))

    /** 有东西要传的清单（**补传入口**）。 */
    fun pending(): List<MeetingDraft> = list().filter { needsSync(it) }

    /**
     * 落盘（原子）。返回 false = **写不进去**（调用方必须如实报出来 —— 这是真的可能丢音频）。
     */
    fun save(draft: MeetingDraft): Boolean {
        val dir = dirOf(draft.clientId)
        if (!dir.isDirectory && !dir.mkdirs()) return false
        val text = OfflineMeetingJson.encode(draft)
        val tmp = File(dir, "$MANIFEST_NAME.tmp")
        return try {
            // 显式 UTF-8：清单里有中文（来源/备注），默认字符集会跟着设备语言漂
            tmp.writeBytes(text.toByteArray(StandardCharsets.UTF_8))
            // renameTo 在同目录内是原子的（跨目录不保证，所以 tmp 就放在同一目录里）
            if (tmp.renameTo(manifestFile(draft.clientId))) return true
            // 极少数文件系统不允许覆盖式 rename：退一步写正文，但仍**不写半截**（先截断再写）
            manifestFile(draft.clientId).writeBytes(text.toByteArray(StandardCharsets.UTF_8))
            tmp.delete()
            true
        } catch (e: Exception) {
            runCatching { tmp.delete() }
            false
        }
    }

    /**
     * 补传入口那一份"先立起来再录"：没有就建一份**新的**（新的 clientId）。
     *
     * 有清单就**接着它录**（这就是"App 重开能接着录"的实现：id、开始时间、段序号都续上）。
     * 读不动时**不覆盖**，另起一份并把原因写在 [DraftLoad.Recovered] 里（如实降级）。
     */
    fun loadOrCreate(
        clientId: String,
        startedAt: String,
        source: String = MeetingSource.PHONE,
    ): DraftLoad {
        val existing = load(clientId)
        if (existing != null && !existing.looksDamaged()) return DraftLoad.Ok(existing)
        if (existing != null) {
            return DraftLoad.Recovered(
                MeetingDraft(clientId = newClientId(), startedAt = startedAt, source = source),
                "上一份清单读不懂（${manifestFile(clientId).name}），已另起一份；旧目录原样留着",
            )
        }
        return DraftLoad.Ok(
            MeetingDraft(clientId = clientId, startedAt = startedAt, source = source))
    }

    /**
     * 标记"这几段被认下了"，并保存。返回真正改动的**最大**段序号（0 = 没有变化）。
     *
     * 为什么要返回改动：对账与续传都靠 `acknowledgedSegments` 这一条单调递增的水位，
     * 调用方需要知道"这一轮有没有前进"（没有前进就不该假装在推进）。
     */
    fun markAcknowledged(draft: MeetingDraft, upTo: Int): MeetingDraft {
        if (upTo <= draft.acknowledgedSegments) return draft
        return draft.copy(
            acknowledgedSegments = upTo,
            segments = draft.segments.map { if (it.seq <= upTo) it.copy(uploaded = true) else it },
        )
    }

    /** 对账通过：把这一份整份标成已上传（**不删音频** —— 见类注释第 3 条）。 */
    fun markUploaded(draft: MeetingDraft, serverMeetingId: Int = draft.serverMeetingId): MeetingDraft =
        draft.copy(
            uploaded = true,
            serverMeetingId = if (serverMeetingId > 0) serverMeetingId else draft.serverMeetingId,
            acknowledgedSegments = maxOf(draft.acknowledgedSegments, maxSeq(draft)),
            segments = draft.segments.map { it.copy(uploaded = true) },
        )

    /**
     * 占位说明：**补传成功时绝不顺手删音频**。
     *
     * 用户的验收口径是"宁可多留一会儿也别丢音频"，所以对账通过（并让服务端收尾）之后，
     * 音频仍然留在 `filesDir/offline-meetings/<clientId>/` 里 —— 清单里 `uploaded=true`
     * 只表示"**可以**清了"。
     *
     * 真正的清理是**另一条有界保留策略**（[OfflineMeetingRetention]）：只有
     * "已对账通过 + 已收尾 + 超过保留天数"三条同时成立才允许清，
     * 且默认保留 [OfflineMeetingRetention.DEFAULT_RETENTION_DAYS] 天
     * （`0` = 永不自动清）。本方法保留成常量 `true` 只是为了让
     * "这一层不会自己删东西"这件事继续是个**能被读出来**的事实。
     */
    fun retainUploadedAudio(): Boolean = true

    /**
     * **清掉这些清单的本地音频**（保留清单本身）。
     *
     * ⚠️ 调用方**必须**是 [OfflineMeetingRetention.plan] 算出来的那些（它才是"绝不早删"的判据）。
     * 这一层只负责"删文件"这一个动作，不自己判该不该删 —— 判据分散在两处就一定会漂。
     *
     * 为什么留着清单（而不是 `deleteDraft` 整个目录）：清单是那一场会**唯一**的本地事实
     * （开始/结束时间、段数、暂停区间），"已经传上去了"这件事本身也值得留个痕 ——
     * 而它只有几 KB。真要连痕都不要，用 [deleteDraft]。
     *
     * 返回**真的删掉了什么**（文件数 + 字节数；0 个文件 = 一个都没删，
     * 调用方据此如实说话，别谎报"清完了"）。
     */
    fun purgeAudio(draft: MeetingDraft): PurgeOutcome {
        var deleted = 0
        var bytes = 0L
        draft.segments.forEach { seg ->
            val f = segmentFile(draft.clientId, seg.seq)
            try {
                if (f.isFile) {
                    val n = f.length()
                    if (f.delete()) {
                        deleted++
                        bytes += n
                    }
                }
            } catch (e: Exception) {
                // 删不掉就留着（外置存储/权限问题）：**不许**因为一个文件删不掉就报成功
            }
        }
        return PurgeOutcome(files = deleted, bytes = bytes)
    }

    /** 磁盘上所有"有音频的"清单（设置页"当前占用"与保留策略的输入）。 */
    fun withAudio(): List<MeetingDraft> = list().filter { it.segments.isNotEmpty() }

    /** 显式清理一份（只在**用户明确要求**时调；别在补传成功时顺手调）。 */
    fun deleteDraft(clientId: String): Boolean {
        val dir = dirOf(clientId)
        return dir.deleteRecursively()
    }

    private fun read(f: File?): MeetingDraft? {
        if (f == null || !f.isFile) return null
        return try {
            OfflineMeetingJson.decode(String(f.readBytes(), StandardCharsets.UTF_8))
        } catch (e: Exception) {
            // 读不动就当没有：**别抛**（补传是后台动作，抛出去会让整场都停下）
            null
        }
    }

    private fun safeName(clientId: String): String =
        clientId.map { c -> if (c.isLetterOrDigit() || c == '-' || c == '_') c else '_' }
            .joinToString("")

    companion object {
        const val MANIFEST_NAME = "manifest.json"

        /** 段文件名：补零 → 目录里按名字排就是**正确顺序**（人肉排障时很有用）。 */
        fun segmentName(seq: Int): String = String.format("seg-%04d.wav", seq)

        /**
         * 新的客户端 id。格式 `phone-<yyyyMMddHHmmss>-<4 位随机>`：
         *  * 前缀说明"这是手机离线录的"（服务端/日志里一眼看得出）；
         *  * 时间是**开始录音的那一刻**（不是生成 id 的那一刻也有意义，便于人肉对账）；
         *  * 尾随机是为了"同一秒里建两份"（极少见）不会撞成同一个幂等键。
         */
        fun newClientId(now: LocalDateTime = LocalDateTime.now(), rnd: Int = (0..0xFFFF).random()): String =
            "phone-" + now.format(ID_STAMP) + "-" + String.format("%04x", rnd and 0xFFFF)

        /** 清单里的时间戳格式（**不带时区**：与电脑侧 `MeetingStatus.startedAt` 同一形状）。 */
        val ISO: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss")

        private val ID_STAMP: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyyMMddHHmmss")

        /** epoch 毫秒 → 清单里的 ISO 字符串。 */
        fun isoOf(epochMs: Long): String =
            LocalDateTime.ofInstant(java.time.Instant.ofEpochMilli(epochMs),
                java.time.ZoneId.systemDefault()).format(ISO)

        fun isoOf(at: LocalDateTime): String = at.format(ISO)

        /** ISO 字符串 → epoch 毫秒；**解析不了回 null**（别编一个 0 出来，那会显示"1970 年"）。 */
        fun epochOf(iso: String?): Long? = try {
            val s = iso?.trim().orEmpty()
            if (s.isEmpty()) null
            else LocalDateTime.parse(s, ISO)
                .atZone(java.time.ZoneId.systemDefault()).toInstant().toEpochMilli()
        } catch (e: Exception) {
            null
        }

        /** 一份清单里**最大**的段序号（0 = 没有段）。 */
        fun maxSeq(draft: MeetingDraft): Int = draft.segments.maxOfOrNull { it.seq } ?: 0
    }
}

/** 编码/解码清单的**唯一**实现（纯逻辑）。 */
object OfflineMeetingJson {

    /**
     * 这一份清单的格式版本。加字段时**不用**动它（老读法会忽略不认识的键）；
     * 只有当**语义**变了（例如段序号从 0 起改成 1 起）才需要跟着变，
     * 让旧版本 App 能判出"这不是我能读的清单"而不是读歪。
     */
    const val FORMAT = 1

    fun encode(draft: MeetingDraft): String = buildString {
        append("{\n")
        append("  \"format\": ").append(FORMAT).append(",\n")
        append("  \"client_id\": ").append(str(draft.clientId)).append(",\n")
        append("  \"source\": ").append(str(draft.source)).append(",\n")
        append("  \"started_at\": ").append(str(draft.startedAt)).append(",\n")
        append("  \"ended_at\": ").append(str(draft.endedAt)).append(",\n")
        append("  \"duration_seconds\": ").append(num(draft.durationSeconds)).append(",\n")
        append("  \"server_meeting_id\": ").append(draft.serverMeetingId).append(",\n")
        append("  \"acknowledged_segments\": ").append(draft.acknowledgedSegments).append(",\n")
        append("  \"uploaded\": ").append(draft.uploaded).append(",\n")
        append("  \"pauses\": [")
        draft.pauses.forEachIndexed { i, p ->
            if (i > 0) append(", ")
            append("{ \"from\": ").append(num(p.from))
            append(", \"to\": ").append(num(p.to))
            append(", \"by\": ").append(str(p.by)).append(" }")
        }
        append("],\n")
        append("  \"segments\": [\n")
        draft.segments.forEachIndexed { i, s ->
            append("    { \"seq\": ").append(s.seq)
            append(", \"file\": ").append(str(s.file))
            append(", \"started_at\": ").append(str(s.startedAt))
            append(", \"start_offset\": ").append(num(s.startOffsetSeconds))
            append(", \"duration\": ").append(num(s.durationSeconds))
            append(", \"uploaded\": ").append(s.uploaded).append(" }")
            append(if (i < draft.segments.size - 1) ",\n" else "\n")
        }
        append("  ]\n")
        append("}\n")
    }

    /**
     * 解析清单。**解析不了回 null**（调用方如实降级），**绝不**抛。
     *
     * 为什么容忍缺字段：清单是"断网时唯一的事实来源"，而它可能来自**旧版本 App**
     * （我们后来加了字段）。缺字段按默认值收，比整份读不动要好得多。
     * 但**读到半截**（写到一半掉电那种）必须认出来 —— 半截清单里的段序号是不可信的，
     * 按它补传会把整个顺序搞乱，所以那种一律回 null（调用方另起一份，旧目录原样留着）。
     */
    fun decode(text: String?): MeetingDraft? {
        val raw = text?.trim().orEmpty()
        if (raw.isEmpty()) return null
        val p = P(raw)
        p.ws()
        val value = p.value()
        p.ws()
        // 只接受"整份都读完了"的解析结果：尾部还有东西 = 截断/混进了别的内容
        if (!p.ok || p.i < raw.length) return null
        @Suppress("UNCHECKED_CAST")
        val obj = value as? Map<String, Any?> ?: return null
        val clientId = (obj["client_id"] as? String).orEmpty()
        if (clientId.isBlank()) return null
        return MeetingDraft(
            clientId = clientId,
            startedAt = (obj["started_at"] as? String).orEmpty(),
            source = (obj["source"] as? String)?.takeIf { it.isNotBlank() } ?: MeetingSource.PHONE,
            endedAt = (obj["ended_at"] as? String).orEmpty(),
            durationSeconds = (obj["duration_seconds"] as? Number)?.toDouble() ?: 0.0,
            serverMeetingId = (obj["server_meeting_id"] as? Number)?.toInt() ?: 0,
            pauses = (obj["pauses"] as? List<*>).orEmpty().mapNotNull { pauseOf(it) },
            segments = (obj["segments"] as? List<*>).orEmpty().mapNotNull { segmentOf(it) },
            acknowledgedSegments = (obj["acknowledged_segments"] as? Number)?.toInt() ?: 0,
            uploaded = obj["uploaded"] as? Boolean ?: false,
        )
    }

    private fun segmentOf(v: Any?): DraftSegment? {
        val o = v as? Map<*, *> ?: return null
        val seq = (o["seq"] as? Number)?.toInt() ?: return null
        val file = (o["file"] as? String).orEmpty()
        if (file.isBlank()) return null
        return DraftSegment(
            seq = seq,
            file = file,
            startedAt = (o["started_at"] as? String).orEmpty(),
            startOffsetSeconds = (o["start_offset"] as? Number)?.toDouble() ?: 0.0,
            durationSeconds = (o["duration"] as? Number)?.toDouble() ?: 0.0,
            uploaded = o["uploaded"] as? Boolean ?: false,
        )
    }

    private fun pauseOf(v: Any?): DraftPause? {
        val o = v as? Map<*, *> ?: return null
        val from = (o["from"] as? Number)?.toDouble() ?: return null
        val to = (o["to"] as? Number)?.toDouble() ?: return null
        return DraftPause(from = from, to = to,
            by = (o["by"] as? String)?.takeIf { it.isNotBlank() } ?: DraftPause.BY_LOCAL)
    }

    private fun str(s: String): String = buildString {
        append('"')
        for (c in s) {
            when (c) {
                '"' -> append("\\\"")
                '\\' -> append("\\\\")
                '\n' -> append("\\n")
                '\r' -> append("\\r")
                '\t' -> append("\\t")
                else -> if (c < ' ') append(String.format("\\u%04x", c.code)) else append(c)
            }
        }
        append('"')
    }

    private fun num(d: Double): String =
        if (d == d.toLong().toDouble()) d.toLong().toString() else d.toString()

    // ---- 一个**够用**的 JSON 读取器（不引 Gson：见文件头注释） ----

    private class P(val s: String) {
        var i = 0

        /**
         * 读到现在为止**没遇到"读不懂"的东西**。
         *
         * 为什么需要它：一个"容错"的解析器很容易把**写到一半**的 JSON 读成一份合法但缺字段的
         * 清单（例如 `{"client_id":"x","segments":[{`）—— 而那份清单里的段序号是不可信的，
         * 按它补传会把顺序搞乱。所以"读不懂"必须是一个能被上层看见的事实。
         */
        var ok = true

        fun ws() {
            while (i < s.length && s[i].isWhitespace()) i++
        }

        fun value(): Any? {
            ws()
            if (i >= s.length) {
                ok = false
                return null
            }
            return when (s[i]) {
                '{' -> obj()
                '[' -> arr()
                '"' -> str()
                't' -> lit("true", true)
                'f' -> lit("false", false)
                'n' -> lit("null", null)
                else -> number()
            }
        }

        private fun lit(word: String, v: Any?): Any? {
            if (s.startsWith(word, i)) {
                i += word.length
                return v
            }
            ok = false
            i = s.length
            return null
        }

        private fun obj(): Map<String, Any?> {
            val out = LinkedHashMap<String, Any?>()
            i++                                     // '{'
            ws()
            if (i < s.length && s[i] == '}') {
                i++
                return out
            }
            while (i < s.length) {
                ws()
                if (i >= s.length) break
                if (s[i] != '"') {                  // 键必须是字符串：不是就说明这份 JSON 坏了
                    ok = false
                    break
                }
                val k = str()
                ws()
                if (i < s.length && s[i] == ':') i++ else {
                    ok = false
                    break
                }
                out[k] = value()
                ws()
                if (i < s.length && s[i] == ',') {
                    i++
                    continue
                }
                if (i < s.length && s[i] == '}') {
                    i++
                    break
                }
                ok = false
                break
            }
            return out
        }

        private fun arr(): List<Any?> {
            val out = ArrayList<Any?>()
            i++                                     // '['
            ws()
            if (i < s.length && s[i] == ']') {
                i++
                return out
            }
            while (i < s.length) {
                out.add(value())
                ws()
                if (i < s.length && s[i] == ',') {
                    i++
                    continue
                }
                if (i < s.length && s[i] == ']') {
                    i++
                    break
                }
                ok = false
                break
            }
            return out
        }

        private fun str(): String {
            val sb = StringBuilder()
            if (i < s.length && s[i] == '"') i++ else ok = false
            var closed = false
            while (i < s.length) {
                val c = s[i]
                if (c == '"') {
                    i++
                    closed = true
                    break
                }
                if (c == '\\' && i + 1 < s.length) {
                    i++
                    when (val e = s[i]) {
                        'n' -> sb.append('\n')
                        'r' -> sb.append('\r')
                        't' -> sb.append('\t')
                        'b' -> sb.append('\b')
                        'f' -> sb.append('\u000C')
                        'u' -> {
                            val hex = s.substring(i + 1, minOf(i + 5, s.length))
                            sb.append(hex.toIntOrNull(16)?.toChar() ?: '?')
                            i += 4
                        }
                        else -> sb.append(e)
                    }
                    i++
                } else {
                    sb.append(c)
                    i++
                }
            }
            if (!closed) ok = false              // 字符串没闭合 = 写在了一半
            return sb.toString()
        }

        private fun number(): Number? {
            val start = i
            while (i < s.length && (s[i].isDigit() || s[i] == '-' || s[i] == '+' ||
                        s[i] == '.' || s[i] == 'e' || s[i] == 'E')) i++
            val t = s.substring(start, i)
            if (t.isEmpty()) {
                ok = false
                i = s.length
                return null
            }
            val v = t.toDoubleOrNull()
            if (v == null) ok = false
            return v
        }
    }
}

/**
 * 音频段的**文件命名与时间轴**（纯函数）。
 *
 * 为什么时间要按"相对开始时间的偏移"记：暂停期间不产生音频，若用绝对时间，
 * 段之间会凭空出现一段空洞（转发给服务端后，转写会把暂停那几分钟算进会议时长，
 * 会议详情里的时间轴就全错了）。与电脑侧"暂停不写盘"是同一个语义。
 */
internal fun offsetSeconds(startedAtIso: String, atEpochMs: Long): Double {
    val start = MeetingDraftStore.epochOf(startedAtIso) ?: return 0.0
    return maxOf(0.0, (atEpochMs - start) / 1000.0)
}

/**
 * 补传时的段选择（**纯函数**）：要不要传、传哪几段。
 *
 * 三条硬口径：
 *  1. **顺序**：按 `seq` 升序，**不跳段** —— 会议音频的先后顺序就是内容本身；
 *  2. **续传**：`seq <= acknowledgedSegments` 的段**跳过**（那些已被服务端认下）；
 *  3. **不编造**：本地没有的段不"补空段"（宁可这一轮停在这里，让下次重试）。
 */
object DraftUploadPlan {

    /** 这一份清单的补传计划（按**顺序**排好的段）。 */
    fun segmentsFor(draft: MeetingDraft): List<DraftSegment> =
        draft.segments
            .filter { it.seq > draft.acknowledgedSegments }
            .sortedBy { it.seq }

    /** 整份要不要补传（已对账完成的不再动它）。 */
    fun needsSync(draft: MeetingDraft): Boolean = !draft.uploaded && draft.segments.isNotEmpty()
}

/** 顶层别名，让调用点读起来是「这份清单要不要传」。 */
internal fun needsSync(draft: MeetingDraft): Boolean = DraftUploadPlan.needsSync(draft)

/**
 * 顶层别名：补传时该传哪几段（顺序已在里面保证）。
 *
 * ⚠️ 名字与 `OfflineMeetingState.kt` 里那个 `segmentsToUpload(sequences, acked)` **故意不同**：
 * 那个是"给一串序号、回该传的序号"（纯数学，给对账与用例用），
 * 这个是"给一份清单、回该传的段对象"（带着文件名与时间）。同名会让人以为是一件事。
 */
internal fun segmentsToUploadOf(draft: MeetingDraft): List<DraftSegment> =
    DraftUploadPlan.segmentsFor(draft)
