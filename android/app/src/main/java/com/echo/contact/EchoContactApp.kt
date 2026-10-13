package com.echo.contact

import android.app.Application
import com.echo.contact.data.ApiClient
import com.echo.contact.data.PairStore

/** 应用入口：把令牌来源接到 PairStore，让所有 API 调用带上 Bearer。 */
class EchoContactApp : Application() {

    override fun onCreate() {
        super.onCreate()
        ApiClient.attachTokenProvider { PairStore.token(this) }
    }
}
