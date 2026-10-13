package com.echo.contact

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
import com.echo.contact.ui.HomeScreen
import com.echo.contact.ui.PairScreen
import com.echo.contact.ui.theme.EchoContactTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
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
