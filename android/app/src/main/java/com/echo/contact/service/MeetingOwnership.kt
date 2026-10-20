package com.echo.contact.service

/**
 * 服务端"当前这场录音"到底**是谁的** —— 手机侧**唯一一处**归属判定。
 *
 * ## 为什么需要它（2026-10-20 真机：手机说「电脑在录音」，其实那是它自己那场在补传）
 *
 * 现场：服务端 `/api/meeting/status` 明明是
 * `active=true, source=phone, clientId=phone-20261020094624-e1b6, segments=1`，
 * 电脑面板也说"手机录音中"（措辞不准，但归属对），**手机却说成「电脑在录音」** ✗。
 *
 * 根因有两半，都在手机这侧：
 *  1. **身份认不出来**：手机的会话 id 是**每次开录音时新生成**的
 *     （`MeetingMicService` 开录时 `s.start(MeetingDraftStore.newClientId(), …)`，
 *     格式见 `MeetingDraftStore.newClientId()`：`phone-<yyyyMMddHHmmss>-<4 位随机>`）。
 *     于是"App 重开 / 再开一场"之后，服务端上那场是**上一次启动**留下的 id，
 *     而旧判据（`OfflineMeetingSync` 里那份 `mineRecording`，已删除）只认"**这一刻**正在录的那份清单"
 *     （`activeClientId`）—— 对不上 → 判成"不是我" → 落到"有别人在录"。
 *     为什么"每次启动会变"是**设计如此**、不能靠它认自己：id 同时是服务端的幂等键，
 *     一场录音必须有一个只属于它的 id；拿"当前会话 id"去比历史，注定认不出来。
 *  2. **显示没有归属信息**：`ui/MeetingsTab` 原来只判 `meeting.active` 就写「电脑在录音」，
 *     而 `/api/status` 与 `/api/meeting/status` 其实**都带**
 *     `source` 与 `clientId`（`app/meeting.py::meeting_status()`），只是安卓那边的 DTO
 *     从来没接这两个字段（见 `data/dto/StatusDtos.kt` 的 `MeetingStatus`）——
 *     信息在网络上走过，界面上却当它不存在。
 *
 * ## 判据（只认"我自己的历史会话"）
 *
 *  * 服务器 `source=phone` 且那个 `clientId` **在我本地清单里**（`MeetingDraftStore.list()`）
 *    → **我的**：还没停就在录（[MeetingOwner.MINE_RECORDING]），停了就是**还在传**
 *    （[MeetingOwner.MINE_UPLOADING]，文案是"手机上这场的录音还在上传"，**不是**"录音中"）；
 *  * 服务器 `clientId` 非空但**不在我清单里** → **另一台手机**（服务端只在"手机补传"这条路上
 *    才会带 `clientId`：电脑本机麦那条路 `meeting.py` 是**显式清空**它的）；
 *  * `source=pc` → 电脑那支麦；
 *  * `active=false` → 谁都没在录（[MeetingOwner.NONE]，**不许**说"在录"）。
 *
 * ## 一处实现、两处使用（**不许各写一遍**）
 *
 *  * **显示**：`ui/MeetingsTab` 的标题行与说明（用 [meetingOwnerTrailing] / [meetingOwnerNote]）；
 *  * **"别抢占"闸**：`OfflineMeetingSync.run` 在传段之前判"ECHO 现在录的是不是我自己那场"
 *    （用 [MeetingOwner.isMine]）。
 *
 * 这两处判据原来**各写了一套**：闸看"当前会话 id"，显示只看 `active`。
 * 于是同一个事实在两处能给出相反的结论 —— 2026-10-20 的真机现象正是这样：
 * 闸把"我自己那场"当成别人的（拒绝续传 = 自己等自己），
 * 显示又把"我自己在补传"说成"电脑在录"。**判据只许有一份**，理由与
 * `MeetingSyncEntry` 那份"入口只在会议页画一次"是同一条。
 *
 * ⚠️ 这一份**只影响显示与归属判定**：不碰录音、不碰传段、不改任何流程。
 */
internal enum class MeetingOwner {
    /** 服务端没有在录任何会（`active=false`）。 */
    NONE,

    /** **这台手机**这一场正录着（本机麦克风在录，服务端也在收段）。 */
    MINE_RECORDING,

    /** **这台手机**录过的那一场：录音已经停了，段还在上传/补传（服务端仍显示 active）。 */
    MINE_UPLOADING,

    /** 电脑那支麦（`source=pc`）。 */
    PC,

    /** **另一台手机**（`source=phone`，但那个 `clientId` 不在本机清单里）。 */
    OTHER_PHONE,

    /** `active=true`，但服务端没说是谁录的、本机也认不出（老服务端缺 `source`/`clientId`）。 */
    UNKNOWN,
    ;

    /**
     * 服务器上那一场是不是**这台手机自己**的。
     *
     * 这是"别抢占"闸**唯一**该问的问题（闸只需要"是我的/不是我的"，
     * 不关心我是在录还是在传）。原来它自己写了一套比较，现在只读这一格。
     */
    val isMine: Boolean get() = this == MINE_RECORDING || this == MINE_UPLOADING
}

/**
 * 本机清单里那一场的**身份**（只用来判归属；不含音频、不含进度）。
 *
 * 为什么只留两个字段：判"是不是我的"只需要"我建过哪些 id / 认下过哪些会议 id"。
 * 把整份 [MeetingDraft] 递给界面会把"这一场还有几段没传"之类的**易变**事实
 * 混进一个本该稳定的身份判据里（判据一旦易变，显示就会自己抖）。
 */
internal data class LocalDraftRef(
    /** 客户端稳定 id（与服务端 `clientId` 逐字比较）。 */
    val clientId: String,
    /** 服务端那一侧的会议 id（0 = 还没建/还没回来；老服务端不给 clientId 时的退路）。 */
    val serverMeetingId: Int = 0,
)

/**
 * 服务端那场"正在录"的会是**谁的**（纯函数；`MeetingOwnershipTest` 逐档钉住）。
 *
 * @param active 服务端 `active`（唯一决定"有没有在录"的东西 —— `false` 一律 [MeetingOwner.NONE]）。
 * @param source 服务端 `source`（`pc` / `phone`；老服务端可能没有 → 空串）。
 * @param clientId 服务端 `clientId`（**只有**手机补传那条路才有）。
 * @param serverMeetingId 服务端 `meetingId`（没有则 0）。
 * @param local 本机清单里那几场的身份（`MeetingDraftStore.list()` → [LocalDraftRef]）。
 *   **`null` = 还没从磁盘读出来** —— 那一刻先别断言"是谁的"（见函数体里的说明）。
 * @param localRecording 这台手机**此刻**在不在录（只影响 [MeetingOwner.MINE_RECORDING]
 *   与 [MeetingOwner.MINE_UPLOADING] 的区分 —— 也就是"在录"还是"还在传"那句文案）。
 *   ⚠️ 用**运行中的服务状态**（`MicState.active`）而不是清单里的 `recording` 标记：
 *   进程被杀之后清单里那个标记会一直停在"还在录"，而麦克风其实早就关了 ——
 *   那样界面会一直说"这台手机在录音"（又一个"手机以为自己还在录"的谎）。
 */
internal fun meetingOwnership(
    active: Boolean,
    source: String = "",
    clientId: String = "",
    serverMeetingId: Int = 0,
    local: List<LocalDraftRef>? = emptyList(),
    localRecording: Boolean = false,
): MeetingOwner {
    if (!active) return MeetingOwner.NONE
    val cid = clientId.trim()
    val mine = if (localRecording) MeetingOwner.MINE_RECORDING else MeetingOwner.MINE_UPLOADING
    if (cid.isNotEmpty()) {
        // 本机清单还没读出来：那一刻**先别断言**是谁的。说成"另一台设备在录音"
        // 就是又编了一句话（读一次清单是微秒级，界面第一帧就可能撞上这个窗口）。
        if (local == null) return MeetingOwner.UNKNOWN
        // 最强判据：这个 id 是我本地清单里建过的（**包括上一次启动留下的那一场**）→ 我的。
        return if (local.any { it.clientId == cid }) mine else MeetingOwner.OTHER_PHONE
    }
    // 老服务端不给 clientId：退到"会议 id 相等"（清单里真记过 server_meeting_id 才算）。
    // 认不出来时**宁可判不是** —— 判错成"是我的"会把段灌进别人正在录的会里
    // （两路音频混进同一场 = 转写里两个人同时说话），判错成"不是我的"最多多等一会儿。
    if (local != null && serverMeetingId > 0 &&
        local.any { it.serverMeetingId > 0 && it.serverMeetingId == serverMeetingId }
    ) {
        return mine
    }
    return when (source.trim().lowercase()) {
        "pc" -> MeetingOwner.PC
        // 手机发起、但没给 id：认不出是哪台手机 → 按"不是我的"处理（宁可多等，绝不抢占）
        "phone" -> MeetingOwner.OTHER_PHONE
        else -> MeetingOwner.UNKNOWN
    }
}

/**
 * 会议页标题行右侧那一句（纯函数）。
 *
 * ⚠️ 这一格是 2026-10-20 那次真机 bug 的**正脸**：旧写法是"`active` 就直接写「电脑在录音」"，
 * 于是"我自己那场还在补传"被说成"电脑在录"。所以：
 *  * 我的那场**已经停了、还在传** → 「手机上这场的录音还在上传」（**不许**说"录音中"，
 *    用户会以为还在录，于是等着"结束"一个早就结束的场子）；
 *  * 只有 `source=pc` 或 `clientId` 真不在我清单里，才说"电脑/另一台设备在录音"。
 */
internal fun meetingOwnerTrailing(owner: MeetingOwner): String = when (owner) {
    MeetingOwner.NONE -> "未在录音"
    MeetingOwner.MINE_RECORDING -> "这台手机在录音"
    MeetingOwner.MINE_UPLOADING -> "手机上这场的录音还在上传"
    MeetingOwner.PC -> "电脑在录音"
    MeetingOwner.OTHER_PHONE -> "另一台设备在录音"
    MeetingOwner.UNKNOWN -> "会议在录音"
}

/**
 * 首页那张状态卡「会议」那一格用的**短**状态词（纯函数）。
 *
 * 为什么它也要走同一处判据：那一格原来写的是 `meeting?.active == true -> "录音中"` ——
 * 手机上那场**已经录完、还在传**的时候，首页照样说"录音中"（2026-10-20 同一个 bug 的
 * 另一张脸）。三格宽的地方放不下刚才那句完整说明，所以只换"哪一档词"，判断仍只有一处。
 */
internal fun meetingOwnerCell(owner: MeetingOwner): String = when (owner) {
    MeetingOwner.NONE -> "未在录音"
    MeetingOwner.MINE_RECORDING -> "录音中"
    MeetingOwner.MINE_UPLOADING -> "上传中"
    // 电脑/另一台设备那几档：**确实**有会正在录，只是不在这台手机上（详情去会议页看）。
    MeetingOwner.PC -> "录音中"
    MeetingOwner.OTHER_PHONE -> "录音中"
    MeetingOwner.UNKNOWN -> "录音中"
}

/**
 * 会议页标题下面那一行**完整**说明（纯函数；空串 = 没什么要说的）。
 *
 * [confirmedSegments] / [totalSegments] 只用于 [MeetingOwner.MINE_UPLOADING] 那句进度
 * （来自磁盘摘要 `PendingSyncSummary`：`已传 M/T 段`）。`totalSegments == 0` = 不知道，
 * 那就**只**说"还在上传"，绝不编一个分母出来。
 */
internal fun meetingOwnerNote(
    owner: MeetingOwner,
    confirmedSegments: Int = 0,
    totalSegments: Int = 0,
): String = when (owner) {
    MeetingOwner.NONE -> ""
    MeetingOwner.MINE_RECORDING ->
        "正在用这台手机的麦克风录音：声音先存手机里，网通就顺手传给 ECHO"
    MeetingOwner.MINE_UPLOADING -> buildString {
        append("手机上这场的录音还在上传")
        if (totalSegments > 0) {
            append("：已传 ${confirmedSegments.coerceIn(0, totalSegments)}/$totalSegments 段")
        }
        // 为什么必须补这一句：用户看到"上传中"会想"那我要不要点结束？"——
        // 录音早就停了，这里要说清"不用你做什么，传完自动开始转写"。
        append("（这场是手机录的、录音已经停了：不用再按「结束」，传完就会开始转写）")
    }
    MeetingOwner.PC ->
        "ECHO 那台电脑正在录音（不是这台手机）：那场在电脑上开始、也在电脑上结束"
    MeetingOwner.OTHER_PHONE ->
        "另一台手机在录音（不是这台手机）：那场是别的设备发起的，这里不遥控它"
    MeetingOwner.UNKNOWN ->
        "ECHO 上有一场会议在录，但这台手机认不出是不是自己那场（服务端没说是谁录的）：先不碰它"
}
