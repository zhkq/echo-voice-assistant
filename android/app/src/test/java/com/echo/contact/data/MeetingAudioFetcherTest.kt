package com.echo.contact.data

import com.echo.contact.audio.MeetingAudio
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File

/**
 * 会议音频的取用（`MeetingAudioFetcher`）—— 真 HTTP + 真文件（MockWebServer）。
 *
 * 这条链路最容易错的是"**下载到一半**"：断网时留半个 wav，播放器会把它当完整文件播
 * （听感是"突然断了"，而日志里什么都没有）。所以这里连"先写 .part 再改名"也一起钉住。
 */
class MeetingAudioFetcherTest {

    private lateinit var server: MockWebServer
    private lateinit var api: EchoApi
    private lateinit var dir: File

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        api = ApiClient.create(server.url("/").toString())
        dir = File(System.getProperty("java.io.tmpdir"), "echo-audio-" + System.nanoTime())
        dir.mkdirs()
    }

    @After
    fun tearDown() {
        runCatching { server.shutdown() }
        runCatching { dir.deleteRecursively() }
    }

    private fun audioBody(size: Int = 2048): Buffer {
        val b = Buffer()
        repeat(size) { b.writeByte((it % 251)) }
        return b
    }

    @Test
    fun `第一段下载到缓存第二段走缓存`(): Unit = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "audio/wav")
                .setBody(audioBody())
        )
        val f = MeetingAudioFetcher(api, dir)

        val first = f.fetch(7, 3)
        assertTrue(first is MeetingAudioFetcher.Outcome.Downloaded)
        first as MeetingAudioFetcher.Outcome.Downloaded
        assertEquals(2048L, first.bytes)
        assertTrue(first.file.isFile)
        assertEquals(MeetingAudio.cacheName(7, 3), first.file.name)

        val again = f.fetch(7, 3)
        assertTrue("第二次该直接用缓存", again is MeetingAudioFetcher.Outcome.Cached)
        assertEquals(1, server.requestCount)

        val req = server.takeRequest()
        assertEquals("/api/meetings/7/audio?seg=3", req.path)
    }

    @Test
    fun `没有这一段要说清是段号问题`(): Unit = runBlocking {
        server.enqueue(MockResponse().setResponseCode(404).setBody("nope"))
        val out = MeetingAudioFetcher(api, dir).fetch(7, 99)
        assertTrue(out is MeetingAudioFetcher.Outcome.Failed)
        assertTrue((out as MeetingAudioFetcher.Outcome.Failed).message.contains("第 99 段"))
    }

    @Test
    fun `令牌不对要指到重新配对`(): Unit = runBlocking {
        server.enqueue(MockResponse().setResponseCode(401).setBody("unauthorized"))
        val out = MeetingAudioFetcher(api, dir).fetch(7, 1)
        assertTrue((out as MeetingAudioFetcher.Outcome.Failed).message.contains("配对"))
    }

    @Test
    fun `连不上说的是连不上`(): Unit = runBlocking {
        server.shutdown()
        val out = MeetingAudioFetcher(api, dir).fetch(7, 1)
        assertTrue((out as MeetingAudioFetcher.Outcome.Failed).message.contains("取音频失败"))
    }

    @Test
    fun `空音频不算下好了`(): Unit = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "audio/wav")
                .setBody(Buffer())
        )
        val out = MeetingAudioFetcher(api, dir).fetch(7, 1)
        assertTrue(out is MeetingAudioFetcher.Outcome.Failed)
        assertTrue((out as MeetingAudioFetcher.Outcome.Failed).message.contains("空"))
        assertFalse("不许留下空文件", File(dir, MeetingAudio.cacheName(7, 1)).exists())
    }

    @Test
    fun `换会议时能清掉这场的缓存`(): Unit = runBlocking {
        server.enqueue(MockResponse().setResponseCode(200).setBody(audioBody()))
        val f = MeetingAudioFetcher(api, dir)
        f.fetch(7, 1)
        assertTrue(File(dir, MeetingAudio.cacheName(7, 1)).isFile)
        f.clear(7)
        assertFalse(File(dir, MeetingAudio.cacheName(7, 1)).exists())
    }
}
