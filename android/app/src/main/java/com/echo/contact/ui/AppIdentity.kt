package com.echo.contact.ui

/**
 * 应用名（用户 2026-10-18 中午定的新名字：*"app 的名字改成 ECHO 超级助理"*）。
 *
 * ## 为什么要有一个常量，而不是到处写字符串
 *
 * 这个名字会出现在三个地方：**桌面图标 label**（`res/values/strings.xml` 的 `app_name`，
 * `AndroidManifest.xml` 的 `android:label` 指向它）、**通知头部的应用名**（系统取同一个 label）、
 * 以及**界面里自己画的那几处**（主页顶部那张卡、配对页标题、设置页"关于"那一行）。
 * 四处各写一份，迟早只改到其中一处 —— 于是手机上图标叫新名字、界面里还叫旧名字。
 *
 * 所以：界面里**只**用 [APP_TITLE]，资源里只用 `app_name`；两者的值由
 * `AppNameTest` 钉成同一个字符串。
 *
 * ## 为什么改名字不改包名
 *
 * `applicationId` / `namespace` 一个字都没动（还是 `com.echo.contact`）：
 * 改包名 = 换一个 App —— 手机上会**多出一个图标**（旧的那个还在），
 * 而配对凭据、本地录音清单（`MeetingDraftStore` 的 app 私有目录）都绑在旧包上，
 * 用户升级完会发现"配对没了、录的音也不见了"。名字是给人看的，包名是给系统认的，
 * 这两件事不能一起改。
 */
internal const val APP_TITLE = "ECHO 超级助理"
