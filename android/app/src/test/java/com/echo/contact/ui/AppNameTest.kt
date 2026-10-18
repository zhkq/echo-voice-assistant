package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * **应用名 = ECHO 超级助理**（用户 2026-10-18 中午："app 的名字改成 ECHO 超级助理"）。
 *
 * 为什么要一起钉住这四件事：应用名在手机上出现的地方不止一处 ——
 *  **桌面图标 label**、**系统设置里的应用名**、**通知头部的应用名**（这三处都读
 * `AndroidManifest.xml` 的 `android:label` → `@string/app_name`），以及**界面里自己画的**
 * （主页顶部那张卡、配对页标题、设置页"关于"那一行）。
 * 只改一处是最典型的"改名改一半"：图标叫新名字、界面里还叫旧名字。
 *
 * 还有一条**不许动**的：`applicationId` / `namespace`（改包名 = 换一个 App，
 * 手机上会多一个图标；配对凭据与本地录音目录都绑在旧包上）。这里也钉住了。
 */
class AppNameTest {

    /** 旧名字（用户 2026-10-18 要求换掉的那个）。**只**出现在本用例里，用来搜残留。 */
    private val oldName = "ECHO 触点"

    private val sourceExtensions = setOf("kt", "java", "xml")

    @Test
    fun `资源里的 app_name 就是 ECHO 超级助理`() {
        val strings = moduleText("src/main/res/values/strings.xml")
        val m = Regex("""<string name="app_name">([^<]*)</string>""").find(strings)
        assertTrue("strings.xml 里找不到 app_name：$strings", m != null)
        assertEquals(
            "用户要的新名字（桌面图标 label、通知头部的应用名都读这一个值）",
            "ECHO 超级助理",
            m!!.groupValues[1].trim(),
        )
    }

    @Test
    fun `界面上用的常量与资源同值`() {
        // 界面里的应用名只认 APP_TITLE；它必须与资源同值，否则同一天里图标与界面会不一样
        assertEquals("ECHO 超级助理", APP_TITLE)
    }

    @Test
    fun `桌面图标与通知用的 label 仍指向 app_name 且包名一个字没动`() {
        val manifest = moduleText("src/main/AndroidManifest.xml")
        assertTrue(
            "android:label 必须仍指向 @string/app_name（这样系统各处才一起改到）",
            manifest.contains("android:label=\"@string/app_name\""),
        )

        val gradle = moduleText("build.gradle.kts")
        assertTrue(
            "改名字**不改包名**：applicationId 必须还是 com.echo.contact（改了就是另一个 App，" +
                "手机上会多一个图标，配对凭据/本地录音也都读不到了）",
            gradle.contains("applicationId = \"com.echo.contact\""),
        )
        assertTrue(
            "namespace 同样不许动（资源与 R 类都绑在它上面）",
            gradle.contains("namespace = \"com.echo.contact\""),
        )
    }

    @Test
    fun `main 源码与资源里没有残留旧名`() {
        val root = moduleRoot()
        val main = File(root, "src/main")
        assertTrue("找不到 $main", main.isDirectory)

        val hits = main.walkTopDown()
            .filter { it.isFile && it.extension in sourceExtensions }
            .filter { it.readText(Charsets.UTF_8).contains(oldName) }
            .map { it.relativeTo(root).path }
            .sorted()
            .toList()

        assertEquals("用户能看到的地方还有旧名残留：$hits", emptyList<String>(), hits)
    }

    @Test
    fun `用户能看到的那几处都改到（配对页 关于行 首页那张卡）`() {
        // 配对页标题
        assertTrue(
            "配对页还画着写死的应用名",
            moduleText("src/main/java/com/echo/contact/ui/PairScreen.kt").contains("APP_TITLE"),
        )
        // 设置页「关于」那一行
        assertTrue(
            "设置页「关于」还画着写死的应用名",
            moduleText("src/main/java/com/echo/contact/ui/SettingsScreen.kt").contains("APP_TITLE"),
        )
        // 首页顶部那张合并卡（第一行）
        assertTrue(
            "首页顶部那张卡没画应用名",
            moduleText("src/main/java/com/echo/contact/ui/StatusCard.kt").contains("APP_TITLE"),
        )
    }

    @Test
    fun `通知里不会冒出写死的应用名`() {
        // 通知头部的应用名由系统取 android:label（已由上面那条钉住），
        // 通知里**自己写**的那几处文案（频道名 / 标题）不许再写应用名 —— 写了就存在两份。
        val serviceDir = File(moduleRoot(), "src/main/java/com/echo/contact/service")
        val offenders = serviceDir.walkTopDown()
            .filter { it.isFile && it.extension == "kt" }
            .filter { it.readText(Charsets.UTF_8).contains("APP_TITLE") }
            .map { it.name }
            .toList()
        assertEquals("通知/服务里写死了应用名（系统本来就会显示它）：$offenders", emptyList<String>(), offenders)

        // 并且确实没有旧名（服务里那几处频道名是"ECHO 事件 / 会议录音"这类**功能名**，不是应用名）
        val oldNameHits = serviceDir.walkTopDown()
            .filter { it.isFile && it.extension == "kt" }
            .filter { it.readText(Charsets.UTF_8).contains(oldName) }
            .map { it.name }
            .toList()
        assertFalse("服务里还残留旧名：$oldNameHits", oldNameHits.isNotEmpty())
    }
}
