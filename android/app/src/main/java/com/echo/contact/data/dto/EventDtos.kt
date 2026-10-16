package com.echo.contact.data.dto

/**
 * 事件流（`GET /api/events`）—— 手机订阅"电脑上发生了什么"。
 *
 * 服务端语义（`app/api.py::api_events`，2026-10-15 加）：
 *  * `since < 0`（**默认**）：不推历史，只给当前游标（新装的手机不该被历史事件淹一遍）；
 *  * `since >= 0`：给 `id > since` 的新事件（**正序**，老的在前）；
 *  * `wait`：没新事件时长轮询等一会儿（上限 25 秒），有就立刻回。
 *
 * `payload` 用 `Map<String, Any?>` 而不是具体类型：事件种类会加，而这里只挑几个字段用
 * （`meeting` / `name` / `id`），硬编码一个"大而全"的 DTO 只会每次加事件都要改它。
 */
data class EventItem(
    val id: Long = 0,
    val ts: String? = null,
    val type: String = "",
    val payload: Map<String, Any?> = emptyMap(),
)

data class EventFeed(
    val items: List<EventItem> = emptyList(),
    val cursor: Long = 0,
    val serverTime: String? = null,
)
