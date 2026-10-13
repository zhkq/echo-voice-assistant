package com.echo.contact.ui

/**
 * 「引擎没给最终结果时，到底用什么」——决定这句话的规则。
 *
 * 为什么单独抽出来：2026-10-09 用户真机上点「说完了」**没有任何反应**。
 * 三个原因里最要命的是这一个 —— `stopListening()` 之后引擎**可能什么都不回调**
 * （它可能早就自己结束了那次会话），于是界面永远停在"在听…"。
 * 规则改成"**必须有结论**"：最终结果优先，其次用中途那段临时结果，都没有才算没听清。
 */
internal data class RecognizeOutcome(
    val text: String,
    /** true = 这是拿临时结果兜的底，界面要**如实说明**。 */
    val usedPartial: Boolean,
)

internal fun resolveRecognizeOutcome(final: String, partial: String): RecognizeOutcome? {
    val f = final.trim()
    if (f.isNotEmpty()) return RecognizeOutcome(f, usedPartial = false)
    val p = partial.trim()
    if (p.isNotEmpty()) return RecognizeOutcome(p, usedPartial = true)
    return null
}
