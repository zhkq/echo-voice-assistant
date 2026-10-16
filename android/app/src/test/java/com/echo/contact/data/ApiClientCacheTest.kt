package com.echo.contact.data

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertSame
import org.junit.Test

/**
 * 钉住 [ApiClient] 的**缓存与超时**语义。
 *
 * 为什么这两条值得单测：
 *  - **缓存**：轮询指令结果时每 1.5 秒发一次请求，每次新建 `OkHttpClient`
 *    会连带新建连接池与线程池 —— 那是泄漏。但缓存键里**必须带超时**，
 *    否则"给 submit 用长超时"会被第一次创建时的短超时顶掉，
 *    表现是"回顾提交永远 60 秒超时，而后端其实还在干"。
 *  - 令牌是**每次请求现取**（拦截器里调 tokenProvider），所以缓存实例不会把令牌钉死 ——
 *    这一条由 `EchoApiWireTest` 的 Bearer 用例覆盖（同一个 baseUrl 两次不同令牌）。
 */
class ApiClientCacheTest {

    @After
    fun tearDown() {
        ApiClient.clearCache()
    }

    @Test
    fun `同一个地址同一个超时 复用同一个实例`() {
        val a = ApiClient.create("http://10.0.0.1:8970")
        val b = ApiClient.create("http://10.0.0.1:8970")

        assertSame("同样参数应命中缓存（否则轮询会不停建连接池）", a, b)
    }

    @Test
    fun `地址写法不同但归一化后相同 也复用`() {
        val a = ApiClient.create("http://10.0.0.1:8970")
        val b = ApiClient.create("http://10.0.0.1:8970/")
        val c = ApiClient.create("http://10.0.0.1:8970/api")

        assertSame(a, b)
        assertSame("带 /api 也会被归一化掉", a, c)
    }

    @Test
    fun `同一个地址不同超时 必须是两个实例`() {
        val short = ApiClient.create("http://10.0.0.1:8970", ApiClient.DEFAULT_READ_TIMEOUT_S)
        val long = ApiClient.create("http://10.0.0.1:8970", ApiClient.LONG_READ_TIMEOUT_S)

        assertNotSame(
            "超时是缓存键的一部分 —— 否则回顾提交会被 60 秒的实例顶掉",
            short, long,
        )
        // 再取一次长超时的，仍然是同一个（缓存对两种超时各自生效）
        assertSame(long, ApiClient.create("http://10.0.0.1:8970", ApiClient.LONG_READ_TIMEOUT_S))
    }

    @Test
    fun `清缓存之后会新建实例`() {
        val a = ApiClient.create("http://10.0.0.1:8970")
        ApiClient.clearCache()
        val b = ApiClient.create("http://10.0.0.1:8970")

        assertNotSame(a, b)
    }

    @Test
    fun `长超时确实比默认长`() {
        // 这条看着废话，但它把"为什么需要长超时"的数字钉在代码里：
        // 实测一次回顾整理要 122 秒，默认 60 秒必然先放弃。
        assertEquals(60, ApiClient.DEFAULT_READ_TIMEOUT_S)
        assertEquals(330, ApiClient.LONG_READ_TIMEOUT_S)
        org.junit.Assert.assertTrue(
            "长超时必须显著大于 122 秒那次实测",
            ApiClient.LONG_READ_TIMEOUT_S > 122,
        )
    }
}
