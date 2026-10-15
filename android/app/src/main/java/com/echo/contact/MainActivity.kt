package com.echo.contact

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import com.echo.contact.data.PairStore
import com.echo.contact.service.EventNotify
import com.echo.contact.ui.HomeScreen
import com.echo.contact.ui.MeetingOpenStore
import com.echo.contact.ui.PairScreen
import com.echo.contact.ui.theme.EchoContactTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * 应用唯一的 Activity。
 *
 * ## 通知点进来要落到"那一场会议"（任务 B8，2026-10-16）
 *
 * `EventNotify.post` 会把会议名塞进 intent 的 [EventNotify.EXTRA_MEETING]，
 * 这里负责把那个 extra 读出来、交给 `MainViewModel`：
 *
 *  * **冷启动**（App 没在跑）：系统用通知的 intent 新起一个 Activity →
 *    走 [onCreate]，在 `setContent` **之前**读取（`savedInstanceState == null`）。
 *  * **热启动**（App 在跑，无论前台还是后台）：Activity 已在任务栈里，
 *    通知 intent 会走 [onNewIntent]（`FLAG_ACTIVITY_SINGLE_TOP` 在
 *    `EventNotify` 里已经加了）。**两条路都调同一个** [handOffMeetingTarget]，
 *    所以不存在"冷启动能到、热启动只能回首页"这种分叉。
 *
 * ⚠️ 这里**没有**动 `AndroidManifest.xml`（本轮只允许改 5 个 Kotlin 文件）：
 * Activity 因此仍是默认的 `standard` launchMode。后果与边界：
 *  * 正常点通知（App 没跑、或已在最前/后台）都能落到那场会议；
 *  * 但"App 就在最前、再点一条通知"时，系统可能**再叠一个 MainActivity 实例**
 *    （默认 launchMode 下 `FLAG_ACTIVITY_SINGLE_TOP` 只在目标已在栈顶、
 *    且任务被它领回时才复用）。新实例照样会打开那场会议（走的是 onCreate 那条路），
 *    只是返回栈里可能多一层 —— 想彻底消掉需要 manifest 的
 *    `android:launchMode="singleTop"`，那是另一个文件的事，已在报告里写明。
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // 只在**真正的新启动**（不是配置变化重建）时读通知 extra：
        // 否则转屏会把几小时前那条通知的目标又打开一遍。
        if (savedInstanceState == null) handOffMeetingTarget(intent)
        setContent {
            EchoContactTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    EchoRoot()
                }
            }
        }
    }

    /**
     * 热启动那条路（App 已在跑）：通知 intent 从这里进来。
     *
     * `FLAG_ACTIVITY_SINGLE_TOP` 让系统复用位于栈顶的实例并回调这里；
     * 若系统选择了"新建实例"，则走 [onCreate] —— 两条路都写了，谁被调用都能到会议。
     * **必须** `setIntent(intent)`：否则 `getIntent()` 仍是旧的那个，
     * 后续任何读 intent 的地方都会看到过期目标。
     */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handOffMeetingTarget(intent)
    }

    /**
     * 通知 extra → 进程级的"要打开哪场会议"（[MeetingOpenStore]）。
     *
     * 放在 `HomeScreen` 之外的**唯一**原因是：ViewModel 是 `viewModel()` 就地建的，
     * Activity 拿不到它；而这个交接只是**一次读字符串**，不涉及任何加载逻辑 ——
     * 真正的"怎么打开那场会议"仍在 `MainViewModel.openMeetingByName`（复用它，不新写）。
     */
    private fun handOffMeetingTarget(intent: Intent?) {
        val name = intent?.getStringExtra(EventNotify.EXTRA_MEETING)?.trim().orEmpty()
        if (name.isEmpty()) return
        MeetingOpenStore.put(name)
    }
}

/**
 * 顶层路由：已配对 → 主页；未配对 → 配对页。
 * 配对状态从 [PairStore] 读（加密存储，读取放到 IO 线程，避免起屏卡顿）。
 */
@Composable
private fun EchoRoot() {
    val context = LocalContext.current
    var epoch by remember { mutableIntStateOf(0) }

    val paired by produceState<Boolean?>(null, epoch) {
        value = withContext(Dispatchers.IO) { PairStore.isPaired(context) }
    }

    when (paired) {
        null -> Box(
            modifier = Modifier.fillMaxSize(),
            contentAlignment = Alignment.Center,
        ) {
            CircularProgressIndicator()
        }

        false -> PairScreen(onPaired = { epoch++ })

        true -> HomeScreen(onUnpair = { epoch++ })
    }
}
