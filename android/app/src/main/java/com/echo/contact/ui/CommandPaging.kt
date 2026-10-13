package com.echo.contact.ui

import com.echo.contact.data.dto.CommandItem

/**
 * 把"更早的一页"并到已有列表上。
 *
 * 为什么要一条规则：`GET /api/commands` 是**按 id 倒序**分页的，而翻页期间
 * 随时可能**插进来新指令**（面板发的、语音发的）—— 那样第二页会和第一页**重叠**，
 * 直接 `existing + page` 就会在界面上出现重复行，而且 `LazyColumn` 的 key 撞了会崩。
 *
 * 规则：按 id 去重（保留先出现的那个，也就是较新的那批），
 * **没有 id 的行照样留着**（宁可多显示一行，也不要静默丢掉一条）。
 */
internal fun mergeCommandPage(
    existing: List<CommandItem>,
    page: List<CommandItem>,
): List<CommandItem> {
    val seen = existing.mapNotNull { it.id }.toHashSet()
    val appended = page.filter { item ->
        val id = item.id
        id == null || seen.add(id)
    }
    return existing + appended
}

/**
 * **刷新头部**：拿最新一页覆盖列表开头，同时保留用户已经翻出来的更早那些行。
 *
 * 用在轮询上（发完指令后每 1.5 秒拉一次最新一页）：直接 `_commands.value = page` 会把
 * 用户翻了半天的历史**一下打回 30 条**；而直接 `mergeCommandPage` 又相反 ——
 * 旧的那份在前，状态永远停在旧值（"已完成"刷不出来）。所以头部用**新**的，
 * 更早的行按 id 去重接在后面。
 */
internal fun mergeFreshHead(
    fresh: List<CommandItem>,
    existing: List<CommandItem>,
): List<CommandItem> {
    val freshIds = fresh.mapNotNull { it.id }.toHashSet()
    return fresh + existing.filter { item ->
        val id = item.id
        id == null || id !in freshIds
    }
}
