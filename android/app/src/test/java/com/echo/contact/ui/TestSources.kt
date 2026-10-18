package com.echo.contact.ui

import java.io.File

/**
 * 用例读**源码 / 资源**用的小工具（**只读**，不改任何东西）。
 *
 * 为什么这一轮需要它：用户 2026-10-18 的三条要求里，有两条是"界面/配置事实"，
 * 而 Compose 在 JVM 门禁里渲染不了、`Context` 也没有 —— 能断言的就只剩两样：
 * ① 抽出来的纯函数（卡片动作、状态文案、组件概要）；② **文件里到底写了什么**
 * （`app_name` 是不是新名字、`android:label` 还指不指向它、旧名字有没有残留）。
 *
 * ②这一类必须读源码，所以这里用"从 `user.dir` 往上找模块根"而不是写死相对路径：
 * Gradle 跑单测时 `user.dir` 是模块目录（`android/app`），但我不想让用例在
 * 换一个执行方式（IDE / 命令行在仓库根跑）时变成一条假红。
 */
internal fun moduleRoot(): File {
    var dir: File? = File(System.getProperty("user.dir") ?: ".").absoluteFile
    while (dir != null) {
        // Gradle 的常规工作目录：android/app
        if (File(dir, "src/main/AndroidManifest.xml").isFile) return dir
        // 从仓库根 / android 那一层跑的时候
        if (File(dir, "app/src/main/AndroidManifest.xml").isFile) return File(dir, "app")
        if (File(dir, "android/app/src/main/AndroidManifest.xml").isFile) return File(dir, "android/app")
        dir = dir.parentFile
    }
    error("找不到 android/app 模块目录（user.dir = ${System.getProperty("user.dir")}）")
}

/** 读模块里的一个文本文件（路径相对 `android/app`）。 */
internal fun moduleText(relative: String): String {
    val file = File(moduleRoot(), relative)
    check(file.isFile) { "没有这个文件：$relative（模块根 = ${moduleRoot()}）" }
    return file.readText(Charsets.UTF_8)
}

/** 从模块根往上找仓库根（`android` 目录的上一层）；找不到返回 null（纯安卓检出）。 */
internal fun repoRootOrNull(): File? {
    var dir: File? = moduleRoot().absoluteFile
    repeat(4) {
        dir = dir?.parentFile
        if (dir != null && File(dir, "app/daily_review.py").isFile) return dir
    }
    return null
}

/** [text] 里 [needle] 出现了几次（源码断言用；`indexOf` 循环，不依赖正则转义）。 */
internal fun occurrencesOf(text: String, needle: String): Int {
    if (needle.isEmpty()) return 0
    var count = 0
    var at = text.indexOf(needle)
    while (at >= 0) {
        count += 1
        at = text.indexOf(needle, at + needle.length)
    }
    return count
}

/**
 * 取一段源码：从 [signature] 那一行到 [nextSignature] 之前。
 *
 * 为什么按"下一个函数签名"截断：源码断言要能说"**这个函数里**不许再有静默出口"，
 * 而不是"整个文件里搜不到某段字"——后者很容易被别处的同名写法糊过去。
 */
internal fun functionBody(source: String, signature: String, nextSignature: String): String {
    val start = source.indexOf(signature)
    check(start >= 0) { "源码里找不到 `$signature`" }
    val end = source.indexOf(nextSignature, start)
    check(end > start) { "在 `$signature` 之后找不到 `$nextSignature`" }
    return source.substring(start, end)
}

