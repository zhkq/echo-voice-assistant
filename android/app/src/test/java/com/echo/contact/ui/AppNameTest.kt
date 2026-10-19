package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * **桌面名 = `超级助理`，App 内标题 = `ECHO 超级助理`**（两处**有意**不同值）。
 *
 * 时间线（别把这两件事合成一件）：
 *  * 2026-10-18 中午：用户要"app 的名字改成 ECHO 超级助理" → 桌面与界面一起叫这个；
 *  * 2026-10-19 晚：用户装了新包之后又说*"app 还是叫 echo 超级助理，我今天不是让你改成超级助理
 *    了吗，因为 app logo 已经有 echo 了"* → **只改桌面/启动器显示名**：`app_name` → `超级助理`；
 *    App 内标题**不动**（[APP_TITLE] 仍是 `ECHO 超级助理`）。
 *
 * 为什么要钉这么细：应用名在手机上出现的地方不止一处 ——
 *  **桌面图标 label**、**系统设置里的应用名**、**通知头部的应用名**（这三处都读
 * `AndroidManifest.xml` 的 `android:label` → `@string/app_name`），以及**App 内自己画的**
 * （主页顶部那张卡、配对页标题、设置页"关于"那一行，走 [APP_TITLE]）。
 * 只改一处是最典型的"改名改一半"；可反过来把两处**强行同步**又是把用户刚提的要求抹掉 ——
 * 所以下面几条断言方向相反：`app_name` 是 `超级助理`，`APP_TITLE` 仍是 `ECHO 超级助理`，
 * 而且两者**必须不相等**。
 *
 * 还有两条**不许动**的：
 *  * `applicationId` / `namespace`（改包名 = 换一个 App，手机上会多一个图标；
 *    配对凭据与本地录音目录都绑在旧包上）；
 *  * `app_name` **只在一处定义**（`values` 的限定目录里不能有第二份把它盖回去 ——
 *    AAPT 会按语言/地区挑一份，桌面名就跟仓库里这份对不上）。
 */
class AppNameTest {

    /** 最早那一版的名字（2026-10-18 之前）。**只**出现在本用例里，用来搜残留。 */
    private val oldName = "ECHO 触点"

    private val sourceExtensions = setOf("kt", "java", "xml")

    /** 资源文件里那个键：`<string name="app_name">值</string>`。 */
    private val appNamePattern = Regex("""<string name="app_name">([^<]*)</string>""")

    @Test
    fun `资源里的 app_name 就是 超级助理`() {
        val strings = moduleText("src/main/res/values/strings.xml")
        val m = appNamePattern.find(strings)
        assertTrue("strings.xml 里找不到 app_name：$strings", m != null)
        assertEquals(
            "桌面/启动器显示名（系统设置里的应用名、通知头部的应用名也读这一个值）：" +
                "2026-10-19 晚用户要求去掉前面的 ECHO —— LOGO 本身已经是 ECHO 品牌位",
            "超级助理",
            m!!.groupValues[1].trim(),
        )
    }

    @Test
    fun `App 内标题仍是 ECHO 超级助理 且有意与桌面名不同`() {
        // App 内标题只认 APP_TITLE：进了 App 之后没有图标陪着，标题得自己把品牌带上
        assertEquals("App 内标题（首页顶部卡 / 配对页标题 / 设置页关于）", "ECHO 超级助理", APP_TITLE)

        val strings = moduleText("src/main/res/values/strings.xml")
        val m = appNamePattern.find(strings)
        assertTrue("strings.xml 里找不到 app_name：$strings", m != null)
        val desktopName = m!!.groupValues[1].trim()

        assertNotEquals(
            "两处**有意不同值**（桌面「超级助理」/ App 内「ECHO 超级助理」）。" +
                "把 app_name 改回 ECHO 超级助理 = 抹掉用户 2026-10-19 晚的要求；" +
                "把 APP_TITLE 也去掉 ECHO 又会让 App 内标题看不出品牌",
            APP_TITLE,
            desktopName,
        )
        assertFalse(
            "桌面名不该再带 ECHO（LOGO 已经有一个了）：$desktopName",
            desktopName.contains("ECHO"),
        )
    }

    @Test
    fun `app_name 只在一处定义（没有被 values 限定目录的第二份盖回去）`() {
        val res = File(moduleRoot(), "src/main/res")
        assertTrue("找不到 $res", res.isDirectory)

        // 注释里提到这个键不算"第二份定义" —— 先把 XML 注释剔掉再找
        val xmlComment = Regex("(?s)<!--.*?-->")
        val definition = Regex("""<string\s+name="app_name"\s*>""")

        val hits = res.walkTopDown()
            .filter { it.isFile && it.extension == "xml" && it.parentFile?.name?.startsWith("values") == true }
            .filter { f ->
                val body = f.readText(Charsets.UTF_8).replace(xmlComment, "")
                definition.containsMatchIn(body)
            }
            .map { it.relativeTo(moduleRoot()).path.replace(File.separatorChar, '/') }
            .sorted()
            .toList()

        assertEquals(
            "app_name 只该定义在 src/main/res/values/strings.xml 一处。`values-zh` / `values-night`" +
                " 这类限定目录里再放一份，AAPT 会按语言/地区挑一份 → 装到手机上桌面名跟仓库里这份" +
                "对不上，而且这种偏差在门禁里看不出来。命中的文件：$hits",
            listOf("src/main/res/values/strings.xml"),
            hits,
        )
    }

    @Test
    fun `桌面图标与通知用的 label 仍指向 app_name 且包名一个字没动`() {
        val manifest = moduleText("src/main/AndroidManifest.xml")
        assertTrue(
            "android:label 必须仍指向 @string/app_name（这样系统各处才一起改到）",
            manifest.contains("android:label=\"@string/app_name\""),
        )
        // 只许有一处：多写一处 label（比如给 Activity 也钉一个）就会出现"桌面改了、别处没改"
        assertEquals(
            "整份清单里 android:label 只该有一个（application 那一个），实际：$manifest",
            1,
            occurrencesOf(manifest, "android:label="),
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
