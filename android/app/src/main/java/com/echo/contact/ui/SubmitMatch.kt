package com.echo.contact.ui

import com.echo.contact.data.dto.CommandItem

/**
 * 「哪一行才是我刚发的」——轮询的认领规则。
 *
 * 为什么需要它：文字指令那条路（`POST /api/assistant/command`）**不回 commandId**
 * （只有语音那条 `/assistant/voice-command` 才回），所以发完只能去 `GET /api/commands`
 * 里认领。而 `_dispatch` 是在**后台线程**里插这一行的（app/assistant.py:792），
 * 插入时刻不确定 —— 所以不能"发完立刻查一次"，必须**反复查、直到认领到**。
 *
 * 三条判据缺一不可：
 *  - `id > maxIdBefore`：**只有**比发送前更新的行才可能是这一条
 *    （否则用户重复发同一句话时，会认领到上一次那条已经 done 的旧记录，
 *      表现就是"刚发完立刻显示旧结果"）；
 *  - `source == "mobile"`：别认领别人（面板 / 语音 / 热键）刚发的同文本记录；
 *  - `text == text`：文本必须一模一样。
 *
 * 抽成纯函数是为了能用单测钉住 —— 它是这次"点了没反馈"那个 bug 的核心。
 */
internal fun pickSubmitted(items: List<CommandItem>, text: String, maxIdBefore: Int): CommandItem? =
    items.firstOrNull { item ->
        (item.id ?: 0) > maxIdBefore &&
            item.source == "mobile" &&
            item.text == text
    }
