package com.echo.contact.ui

import com.echo.contact.data.dto.CommandItem
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 钉住指令历史的两条合并规则。
 *
 * 背景：`GET /api/commands` 是**按 id 倒序**分页的，翻页期间随时可能插进来新指令
 * （面板发的、语音发的）—— 于是第二页会和第一页重叠。直接 `existing + page`
 * 会在界面上出现重复行，而 `LazyColumn` 的 key 撞了会**直接崩**。
 */
class CommandPagingTest {

    private fun row(id: Int, text: String = "t$id", status: String = "done") =
        CommandItem(id = id, text = text, source = "mobile", status = status)

    // ---------------------------------------------------------------- 追加更早一页

    @Test
    fun `两页不重叠时按顺序接在后面`() {
        val merged = mergeCommandPage(listOf(row(5), row(4)), listOf(row(3), row(2)))

        assertEquals(listOf(5, 4, 3, 2), merged.map { it.id })
    }

    @Test
    fun `翻页期间插进来新指令 也不会出现重复行`() {
        // 第一页拿的是 5,4,3；翻第二页前又插进来一条 6，于是第二页成了 4,3,2 —— 与第一页重叠
        val existing = listOf(row(5), row(4), row(3))
        val page = listOf(row(4), row(3), row(2))

        val merged = mergeCommandPage(existing, page)

        assertEquals("重复的 id 只留一份，且顺序不乱", listOf(5, 4, 3, 2), merged.map { it.id })
    }

    @Test
    fun `整页都重复时等于没变`() {
        val existing = listOf(row(5), row(4))

        val merged = mergeCommandPage(existing, listOf(row(5), row(4)))

        assertEquals(listOf(5, 4), merged.map { it.id })
    }

    @Test
    fun `空页不改动`() {
        val existing = listOf(row(5))

        assertEquals(existing, mergeCommandPage(existing, emptyList()))
    }

    @Test
    fun `没有 id 的行照样留着（宁可多显示一行 也不要静默丢数据）`() {
        val orphan = CommandItem(id = null, text = "没有 id 的一行", source = "mobile")
        val merged = mergeCommandPage(listOf(row(5)), listOf(orphan, row(5)))

        assertEquals(2, merged.size)
        assertEquals(null, merged[1].id)
    }

    // ---------------------------------------------------------------- 刷新头部

    @Test
    fun `刷新头部时 重叠的行取新的状态`() {
        val existing = listOf(row(5, status = "running"), row(4), row(3))
        val fresh = listOf(row(5, status = "done"), row(4))

        val merged = mergeFreshHead(fresh, existing)

        assertEquals(listOf(5, 4, 3), merged.map { it.id })
        assertEquals("状态必须是新的那个", "done", merged[0].status)
    }

    @Test
    fun `刷新头部不会把翻出来的更早那些行打回 30 条`() {
        val older = (1..40).map { row(it) }                 // 用户翻出来的更早 40 条
        val existing = listOf(row(100), row(99)) + older    // 头部是 100, 99，后面接 1..40
        val fresh = listOf(row(101), row(100))               // 轮询拿回最新两条

        val merged = mergeFreshHead(fresh, existing)

        // 新的在前（101, 100），existing 里没被覆盖的按原顺序跟在后面（99 然后 1..40）
        assertEquals(listOf(101, 100, 99) + (1..40).toList(), merged.map { it.id })
        assertEquals("一条都没丢", 43, merged.size)
    }

    @Test
    fun `没有 id 的行在刷新头部时也保留`() {
        val orphan = CommandItem(id = null, text = "孤儿行")
        val merged = mergeFreshHead(listOf(row(9)), listOf(orphan))

        assertEquals(2, merged.size)
        assertEquals(9, merged[0].id)
        assertEquals(null, merged[1].id)
    }

    @Test
    fun `全新一页与空列表合并就是它自己`() {
        val fresh = listOf(row(3), row(2))

        assertEquals(fresh, mergeFreshHead(fresh, emptyList()))
    }
}
