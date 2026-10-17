package com.echo.contact.service

import com.echo.contact.audio.EncodedAudio
import com.echo.contact.audio.FlacStreamInfo
import com.echo.contact.audio.FlacTestBytes
import com.echo.contact.audio.SegmentAudioEncoder
import com.echo.contact.audio.SegmentFormat
import com.echo.contact.audio.WavWriter
import com.echo.contact.audio.encodeFlacSegment
import com.echo.contact.audio.looksLikeFlac
import com.echo.contact.data.ApiClient
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.util.concurrent.TimeUnit

/**
 * **封段时回填 STREAMINFO 的 `total_samples`** —— 从封段落盘一路钉到"上传的字节"。
 *
 * 真机现场（2026-10-17）：`MediaCodec` 写 FLAC 头时还不知道总长度，于是
 * `total_samples = 0`。规范里那是"未知"（合法），但 `libsndfile` 读成 `SF_COUNT_MAX (2^63-1)`
 * → `Internal psf_fseek() failed` → 服务端**整段解不出来**。文件本身没坏（ffmpeg 能解）。
 *
 * 于是客户端在**封段收尾**把真的总样本数回填进头里（[FlacStreamInfo]），回填不了就
 * **整段退回 WAV**。这一组用例钉四件事：
 *  1. 落盘的 FLAC 头里 `total_samples` == 真实样本数，而且**文件长度没变**；
 *  2. 回填失败 → 这一段**退回 WAV**、清单**如实记 WAV**、PCM **逐字节不少**；
 *  3. 端到端（真 MockWebServer）：上传的 segment body 里 `total_samples` 就是真的
 *     （服务端不会拿到一个 total_samples=0 的段）；
 *  4. 端到端反面对照：回填失败那一段上传的是 WAV，**不是**假 FLAC。
 *
 * 为什么要走真 HTTP：`Content-Type`、body 是不是"回填之后的那份字节"都是**字节层面的契约**，
 * 假的 transport 里这些是**我自己**拼的 —— 拼错了它照样绿。
 *
 * ⚠️ **`MediaCodec` 本身不在这一组里**（JVM 单测里它是桩）：这一组用一个假的"原始 FLAC"
 * 替它，形状严格照真机取证的那一份（`fLaC` + STREAMINFO(total_samples=0) + 帧字节）。
 * 真机复验这件事还没做，别把这里当成"真机验过了"。
 */
class MeetingSegmentFlacHeaderTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private val clientId = "phone-20261017120000-abcd"

    private lateinit var server: MockWebServer

    /**
     * 一个够真的假 ECHO：只留这一段需要的五条接口
     * （探活 / 会议状态 / 建会 / 传段 / 收尾），按 `clientId` 幂等、回累计段数。
     */
    private class EchoStub : Dispatcher() {
        var recording = false
        var statusClientId = ""
        var stops = 0
        val segments = LinkedHashMap<String, MutableSet<Int>>()

        override fun dispatch(request: RecordedRequest): MockResponse {
            val path = request.path!!.substringBefore('?')
            val q = request.requestUrl!!
            return when (path) {
                "/health" -> MockResponse().setResponseCode(200)
                "/api/meeting/status" -> json(
                    """{"active":$recording,"clientId":"$statusClientId",""" +
                        """"segments":${segments.values.sumOf { it.size }}}""")
                "/api/meeting/start" -> {
                    val cid = q.queryParameter("clientId").orEmpty()
                    recording = true
                    statusClientId = cid
                    json("""{"ok":true,"message":"开始了","meetingId":42}""")
                }
                "/api/meeting/segment" -> {
                    val cid = q.queryParameter("clientId").orEmpty()
                    val seq = q.queryParameter("seq")?.toIntOrNull() ?: -1
                    val set = segments.getOrPut(cid) { linkedSetOf() }
                    set.add(seq)
                    json("""{"ok":true,"meetingId":42,"segmentCount":${set.size}}""")
                }
                "/api/meeting/stop" -> {
                    stops++
                    recording = false
                    json("""{"ok":true,"message":"已开始转写"}""")
                }
                else -> MockResponse().setResponseCode(404)
            }
        }

        private fun json(body: String) = MockResponse().setResponseCode(200)
            .setHeader("Content-Type", "application/json").setBody(body)
    }

    @Before
    fun setUp() {
        server = MockWebServer()
        server.dispatcher = EchoStub()
        server.start()
        ApiClient.attachTokenProvider { null }
        ApiClient.clearCache()
    }

    @After
    fun tearDown() {
        ApiClient.attachTokenProvider { null }
        runCatching { server.shutdown() }
    }

    // ---------------------------------------------------------------- 造数据

    private fun store(name: String) = MeetingDraftStore(tmp.newFolder(name))

    /** 一段 PCM（16 kHz / 单声道 / 16 bit），内容可区分但不重要。 */
    private fun pcmOf(samples: Int): ByteArray =
        ByteArray(samples * 2) { (it * 31 % 251).toByte() }

    /**
     * 用**生产路径**的封段一步（[encodeFlacSegment]），只把 `MediaCodec` 换成 [raw]。
     *
     * [raw] 拿到 PCM，返回"编码器原样吐出来的字节"（真机上 `total_samples` 就是 0）。
     */
    private fun flacEncoder(raw: (ByteArray) -> ByteArray): SegmentAudioEncoder =
        object : SegmentAudioEncoder {
            override fun encode(pcm: ByteArray): EncodedAudio =
                encodeFlacSegment(pcm, 16_000, 1) { raw(pcm) }
        }

    /** 真机形状的原始输出：头里 `total_samples = 0`，帧字节按 PCM 的 1/4 造（长度可预测）。 */
    private fun mediaCodecShaped(pcm: ByteArray): ByteArray =
        FlacTestBytes.stream(totalSamples = 0, frameBytes = pcm.size / 4)

    private fun session(store: MeetingDraftStore, encoder: SegmentAudioEncoder) =
        MeetingRecordSession(store = store, encoder = encoder, sampleRate = 16_000, channelCount = 1)

    private fun sync(store: MeetingDraftStore): OfflineSyncResult = runBlocking {
        OfflineMeetingSync.run(
            transport = OfflineMeetingTransport(ApiClient.create(server.url("/").toString())),
            store = store,
        )
    }

    private fun drain(): List<RecordedRequest> {
        val out = mutableListOf<RecordedRequest>()
        while (true) {
            out += server.takeRequest(200, TimeUnit.MILLISECONDS) ?: break
        }
        return out
    }

    // ---------------------------------------------------------------- ① 落盘的那份头

    @Test
    fun `封段落盘的 FLAC 头里 total_samples 等于真实样本数（清单记 FLAC）`() {
        val store = store("flac")
        val samples = 48_000                                // 3 秒
        val pcm = pcmOf(samples)
        // 编码器原样吐出来的那份字节（长度就是落盘之后的长度 —— 回填**不许**改它）
        val shaped = mediaCodecShaped(pcm)
        val s = session(store, flacEncoder { shaped })
        s.start(clientId, "2026-10-17T12:00:00")

        val seal = s.seal(pcm)

        assertEquals("这一段真的是 FLAC（清单里那一份就是发出去时用的那一份）",
            SegmentFormat.FLAC, seal.format)
        val d = store.load(clientId)!!
        val seg = d.segments.single()
        assertEquals("文件名跟着**真的**格式走", "seg-0001.flac", seg.file)
        assertEquals(SegmentFormat.FLAC, seg.format)

        val f = store.audioFileOf(clientId, seg)
        val bytes = f.readBytes()
        assertTrue("fLaC 魔数还在", looksLikeFlac(bytes))
        assertEquals("头里必须写**真的**总样本数（服务端就是按它 seek 的）",
            samples.toLong(), FlacStreamInfo.parse(bytes)!!.totalSamples)
        assertEquals("回填是原地覆盖：文件长度一个字节都没变", shaped.size, bytes.size)
        assertEquals(shaped.size.toLong(), f.length())
        assertTrue("这一句不该出现（编成了 FLAC 就没什么要说的）：${seal.note}", seal.note.isBlank())
    }

    // ---------------------------------------------------------------- ② 回填失败 → 退回 WAV

    @Test
    fun `回填失败时这一段退回 WAV、清单如实记 WAV、PCM 一个字节不少`() {
        val store = store("wav")
        val samples = 24_000                                // 1.5 秒
        val pcm = pcmOf(samples)
        // 注入回填失败：编码器给回来的"FLAC"头不完整（真机上对应的情形：认不出 STREAMINFO）
        val s = session(store, flacEncoder {
            FlacTestBytes.stream(totalSamples = 0).copyOfRange(0, 20)
        })
        s.start(clientId, "2026-10-17T12:01:00")

        val seal = s.seal(pcm)

        assertEquals("回填不了就退回 WAV（绝不发 total_samples=0 的 FLAC）",
            SegmentFormat.WAV, seal.format)
        val d = store.load(clientId)!!
        val seg = d.segments.single()
        assertEquals("文件名必须跟着真的格式走（人肉排障最先看的就是文件名）",
            "seg-0001.wav", seg.file)
        assertEquals("清单里那份格式必须是真的（WAV）", SegmentFormat.WAV, seg.format)

        val bytes = store.audioFileOf(clientId, seg).readBytes()
        assertEquals("退回 WAV 也不丢音频：44 字节头 + PCM",
            WavWriter.HEADER_SIZE + pcm.size, bytes.size)
        assertTrue("得是 RIFF/WAVE 头", String(bytes, 0, 4) == "RIFF")
        assertTrue("PCM 必须逐字节一致",
            bytes.copyOfRange(WavWriter.HEADER_SIZE, bytes.size).contentEquals(pcm))
        assertTrue("界面/日志要有一句如实话：${seal.note}", seal.note.isNotBlank())
        assertTrue("要说清是回填那一步失败（不是编码器不可用）：${seal.note}",
            seal.note.contains("回填"))
    }

    // ---------------------------------------------------------------- ③ 端到端：上传的是回填过头的 FLAC

    @Test
    fun `端到端：上传的 segment body 是回填过头的 FLAC（真 HTTP 重新解析 STREAMINFO）`() {
        val store = store("e2e-flac")
        val samples = 24_000                                // 1.5 秒
        val pcm = pcmOf(samples)
        val shaped = mediaCodecShaped(pcm)
        val s = session(store, flacEncoder { shaped })
        s.start(clientId, "2026-10-17T12:02:00")
        s.seal(pcm)
        s.finish()

        val r = sync(store)

        assertEquals("传完 + 对账 + 收尾全成了才是 DONE：$r", OfflinePhase.DONE, r.phase)
        val req = drain().single { it.path!!.substringBefore('?') == "/api/meeting/segment" }
        assertEquals("Content-Type 必须按清单里那份格式发", "audio/flac", req.getHeader("Content-Type"))
        val body = req.body.readByteArray()
        assertEquals("body 长度 = 回填前的长度（原地覆盖，没多没少）",
            shaped.size, body.size)
        assertTrue("body 里得有 fLaC 魔数", looksLikeFlac(body))
        val info = FlacStreamInfo.parse(body)!!
        assertEquals("**上传的那份字节里** total_samples 就是真的（服务端不会拿到 0）",
            samples.toLong(), info.totalSamples)
        assertEquals("采样率没被写坏", 16_000, info.sampleRate)
        assertEquals("声道没被写坏", 1, info.channels)
        assertEquals("位深没被写坏", 16, info.bitsPerSample)
    }

    // ---------------------------------------------------------------- ④ 端到端：回填失败那一段发的是 WAV

    @Test
    fun `端到端：回填失败那一段上传的是 WAV（不是 total_samples=0 的 FLAC）`() {
        val store = store("e2e-wav")
        val samples = 16_000                                // 1 秒
        val pcm = pcmOf(samples)
        val s = session(store, flacEncoder {
            FlacTestBytes.stream(totalSamples = 0).copyOfRange(0, 20)
        })
        s.start(clientId, "2026-10-17T12:03:00")
        s.seal(pcm)
        s.finish()

        val r = sync(store)

        assertEquals("退回 WAV 也是好的一段：照常传完 + 收尾：$r", OfflinePhase.DONE, r.phase)
        val req = drain().single { it.path!!.substringBefore('?') == "/api/meeting/segment" }
        assertEquals("audio/wav", req.getHeader("Content-Type"))
        val body = req.body.readByteArray()
        assertFalse("**绝不许**把 total_samples=0 的 FLAC 发上去", looksLikeFlac(body))
        assertTrue("得是 WAV 头", String(body, 0, 4) == "RIFF")
        assertTrue("PCM 逐字节一致", body.copyOfRange(WavWriter.HEADER_SIZE, body.size)
            .contentEquals(pcm))
        assertEquals("清单里也要如实记 WAV",
            SegmentFormat.WAV, store.load(clientId)!!.segments.single().format)
    }
}
