package com.echo.contact.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.echo.contact.data.ApiClient

/**
 * 配对页：填后端地址 + 一次性配对码（码由后端面板/网页在回环上生成），
 * 换取令牌后立即写入加密存储。
 */
@Composable
fun PairScreen(onPaired: () -> Unit) {
    val vm: PairViewModel = viewModel()
    val state by vm.state.collectAsState()
    val checking by vm.checking.collectAsState()
    val connectionMessage by vm.connectionMessage.collectAsState()
    val lastBaseUrl by vm.initialBaseUrl.collectAsState()

    var baseUrl by remember { mutableStateOf(ApiClient.DEFAULT_BASE_URL) }
    var baseUrlTouched by remember { mutableStateOf(false) }
    var code by remember { mutableStateOf("") }
    var name by remember { mutableStateOf("mobile") }

    // 上次配对用过的地址：加载完再回填，且不覆盖用户已经改过的输入。
    LaunchedEffect(lastBaseUrl) {
        val saved = lastBaseUrl
        if (!saved.isNullOrBlank() && !baseUrlTouched) baseUrl = saved
    }

    LaunchedEffect(state) {
        if (state is PairUiState.Success) onPaired()
    }

    val submitting = state is PairUiState.Submitting

    Surface(modifier = Modifier.fillMaxSize()) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(24.dp),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text("ECHO 触点", style = MaterialTheme.typography.headlineMedium)
            Spacer(Modifier.height(8.dp))
            Text(
                "在这台手机上连接你的 ECHO",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(28.dp))

            OutlinedTextField(
                value = baseUrl,
                onValueChange = {
                    baseUrl = it
                    baseUrlTouched = true
                },
                label = { Text("后端地址") },
                placeholder = { Text("http://192.168.1.5:8970") },
                singleLine = true,
                enabled = !submitting,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                modifier = Modifier.fillMaxWidth(),
            )
            Text(
                "手机要填电脑的局域网 IP（127.0.0.1 在手机上指手机自己）。" +
                    "USB 调试可先执行 adb reverse tcp:8970 tcp:8970，再用 127.0.0.1。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(12.dp))
            OutlinedTextField(
                value = code,
                onValueChange = { raw -> code = raw.filter { it.isDigit() }.take(6) },
                label = { Text("配对码") },
                placeholder = { Text("6 位数字") },
                singleLine = true,
                enabled = !submitting,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(12.dp))
            OutlinedTextField(
                value = name,
                onValueChange = { name = it },
                label = { Text("设备名") },
                singleLine = true,
                enabled = !submitting,
                modifier = Modifier.fillMaxWidth(),
            )

            Spacer(Modifier.height(20.dp))
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                OutlinedButton(
                    onClick = { vm.verifyConnection(baseUrl) },
                    enabled = !submitting && !checking,
                    modifier = Modifier.weight(1f),
                ) {
                    Text(if (checking) "连接中…" else "测试连接")
                }
                Button(
                    onClick = { vm.claim(baseUrl, code, name) },
                    enabled = !submitting && baseUrl.isNotBlank() && code.isNotBlank(),
                    modifier = Modifier.weight(1f),
                ) {
                    Text(if (submitting) "配对中…" else "开始配对")
                }
            }

            if (submitting) {
                Spacer(Modifier.height(16.dp))
                CircularProgressIndicator()
            }

            connectionMessage?.let { msg ->
                Spacer(Modifier.height(16.dp))
                Text(
                    msg,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            (state as? PairUiState.Error)?.let { err ->
                Spacer(Modifier.height(16.dp))
                Text(
                    err.message,
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.error,
                )
            }

            Spacer(Modifier.height(24.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    "配对码在电脑端 ECHO 面板里生成",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.width(4.dp))
            }
        }
    }
}
