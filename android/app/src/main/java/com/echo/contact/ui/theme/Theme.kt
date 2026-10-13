package com.echo.contact.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * 面板的调色板（逐值抄自 `web/app.css` 的 `:root`）。
 *
 * 抄它而不是自己配一套的理由：手机上要"看着就是同一个 ECHO"。
 * 面板是**纯深色、没有浅色模式**，所以这里也固定深色，不跟系统走
 * （跟系统走会在浅色手机上突然变成一个白底 App，跟电脑上完全是两个东西）。
 */
object EchoPalette {
    /** 页面底（面板 `--bg`，也是 PWA 的 theme-color）。 */
    val Bg = Color(0xFF0F1117)

    /** 卡片底 / 顶栏底（`--card`）。 */
    val Card = Color(0xFF171A23)

    /** 卡内嵌套块、按钮底、输入框底（`--card2`）。 */
    val Card2 = Color(0xFF1E2230)

    /** 所有 1px 描边（`--border`）。 */
    val Border = Color(0xFF2A2F3E)

    /** 正文（`--text`）。 */
    val Text = Color(0xFFE6E9F0)

    /** 次要文字（`--muted`）。 */
    val Muted = Color(0xFF8B93A7)

    /** 蓝：主按钮、品牌字、聚焦边框（`--accent`）。 */
    val Accent = Color(0xFF4F8CFF)

    /** 在线（`--green`）。 */
    val Green = Color(0xFF3ECF8E)

    /** 空闲 / 待处理（`--yellow`）。 */
    val Yellow = Color(0xFFE6B84C)

    /** 错误 / 危险（`--red`）。 */
    val Red = Color(0xFFEF5B5B)

    /** 进行中：`running / transcribing / sent`（`--purple`）。 */
    val Purple = Color(0xFFA78BFA)

    /** 错误容器的深底（面板没有这一档，为 Compose 卡片补一个不刺眼的）。 */
    val ErrorContainer = Color(0xFF3A1F22)

    /**
     * 状态 → 灯色，**照面板 `.dot` / `.badge` 的规则**：
     * `online|active`=绿、`offline|error`=红、`idle|disabled|paused`=黄、
     * `transcribing|sent|running`=紫、`unknown`=灰。
     *
     * 两处刻意偏离面板（都写在注释里，不是随手改的）：
     *  - `ready`（实测 stt 会报这个值）算绿 —— 它就是"可用"；
     *  - `skipped` 算灰不算黄 —— 实测 dsh 报 `skipped` 时 detail 是
     *    「未使用（你选的是 标准版 harness）」，那是"没启用"不是"待处理"。
     */
    fun statusColor(status: String): Color = when (status.lowercase()) {
        "online", "active", "ready", "ok", "done" -> Green
        "offline", "error", "failed" -> Red
        "idle", "disabled", "paused" -> Yellow
        "transcribing", "sent", "running", "pending" -> Purple
        else -> Muted
    }
}

private val EchoDark = darkColorScheme(
    primary = EchoPalette.Accent,
    onPrimary = Color.White,
    secondary = EchoPalette.Purple,
    onSecondary = Color.White,
    tertiary = EchoPalette.Purple,
    onTertiary = Color.White,
    background = EchoPalette.Bg,
    onBackground = EchoPalette.Text,
    surface = EchoPalette.Card,
    onSurface = EchoPalette.Text,
    surfaceVariant = EchoPalette.Card2,
    onSurfaceVariant = EchoPalette.Muted,
    outline = EchoPalette.Border,
    error = EchoPalette.Red,
    onError = Color.White,
    errorContainer = EchoPalette.ErrorContainer,
    onErrorContainer = Color(0xFFFFD9D9),
)

/** 固定深色（面板只有深色），不跟随系统。 */
@Composable
fun EchoContactTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = EchoDark,
        content = content,
    )
}
