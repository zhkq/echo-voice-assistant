package com.echo.contact.ui

import com.echo.contact.data.dto.CommandItem
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * 钉住「认领我刚发的那一行」这条规则 —— 它是"点完没反馈"那个 bug 的核心。
 *
 * 认领错了会有两种很误导的表现：
 *  - 认领到**上一次**同文本的旧记录 → 刚发完立刻显示旧结果（看着像"没执行"）；
 *  - 认领到**别人**（面板/语音）发的同文本记录 → 结果张冠李戴。
 */
class SubmitMatchTest {

    private fun row(
        id: Int,
        text: String,
        source: String = "mobile",
        status: String = "pending",
        reply: String? = null,
    ) = CommandItem(id = id, text = text, source = source, status = status, reply = reply)

    @Test
    fun `认领比发送前更新的那一条`() {
        val items = listOf(row(7, "把客厅灯打开"), row(8, "把客厅灯打开"))

        val hit = pickSubmitted(items, "把客厅灯打开", maxIdBefore = 7)

        assertEquals(8, hit?.id)
    }

    @Test
    fun `发送前就存在的同文本旧记录绝不认领`() {
        // 用户第二次发同一句话：旧的那条（id=5）已经是 done，绝不能拿来当结果
        val items = listOf(
            row(5, "把客厅灯打开", status = "done", reply = "上一次的结果"),
            row(6, "别的事"),
        )

        val hit = pickSubmitted(items, "把客厅灯打开", maxIdBefore = 5)

        assertNull("不该认领旧记录", hit)
    }

    @Test
    fun `别人发的同文本记录不认领`() {
        val items = listOf(row(9, "把客厅灯打开", source = "web"), row(10, "把客厅灯打开", source = "hotkey"))

        val hit = pickSubmitted(items, "把客厅灯打开", maxIdBefore = 8)

        assertNull("source 不是 mobile 就不是我发的", hit)
    }

    @Test
    fun `文本不同的新记录不认领`() {
        val items = listOf(row(9, "关掉客厅灯"))

        val hit = pickSubmitted(items, "把客厅灯打开", maxIdBefore = 8)

        assertNull(hit)
    }

    @Test
    fun `后端还没插进那一行时返回 null（所以必须继续轮询）`() {
        val items = listOf(row(3, "别的事"))

        val hit = pickSubmitted(items, "把客厅灯打开", maxIdBefore = 3)

        assertNull("_dispatch 是后台线程，插入时刻不确定，这一轮没有很正常", hit)
    }

    @Test
    fun `id 为空的记录不算数`() {
        val items = listOf(CommandItem(id = null, text = "把客厅灯打开", source = "mobile"))

        assertNull(pickSubmitted(items, "把客厅灯打开", maxIdBefore = 0))
    }
}
