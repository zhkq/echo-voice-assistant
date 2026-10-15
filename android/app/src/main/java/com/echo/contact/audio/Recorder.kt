package com.echo.contact.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import java.io.ByteArrayOutputStream
import kotlin.concurrent.thread

/**
 * 录音 → 16 kHz 单声道 PCM → wav 字节。
 *
 * 为什么要自己录再上传（而不是用系统识别）：手机侧那套 `SpeechRecognizer`
 * 在**这台设备上不可用**（有识别服务，但一个回调都不给、也没有识别界面，
 * 2026-10-09 真机实测）。而 ECHO 侧的转写又快又准（实测 139–203 ms、语义全对），
 * 所以正解是**把音频交给 ECHO**（设计文档 §2 的 A 档）。
 *
 * 采样率固定 **16 kHz 单声道**：ECHO 的 `_audio_to_wav16k` 认得各种格式，
 * 但传已经是 16 k 的 wav 最省事（不用它在服务端重采样）。
 */
class Recorder {

    private var record: AudioRecord? = null
    private var worker: Thread? = null
    private var pcm: ByteArrayOutputStream? = null

    @Volatile private var recording = false

    /**
     * 开始录音。返回 false 表示设备不给开麦（原因由调用方去看权限/占用）。
     *
     * `device` = 用户在「设置 → 设备」里指定的那支麦（null = 跟随系统）。
     * `setPreferredDevice` 的语义是**偏好**，系统可以不听 —— 所以这里不承诺"一定用它"，
     * 调用方（界面）负责把"现在实际用哪支"说清楚（见 `InputDevices.describe`）。
     */
    @SuppressLint("MissingPermission")   // 权限由界面先要到；这里只负责开麦
    fun start(device: android.media.AudioDeviceInfo? = null): Boolean {
        if (recording) return true
        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL, ENCODING)
        if (minBuf <= 0) return false
        val bufSize = minBuf * 2
        val ar = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                SAMPLE_RATE,
                CHANNEL,
                ENCODING,
                bufSize,
            )
        } catch (e: Exception) {
            return false
        }
        if (ar.state != AudioRecord.STATE_INITIALIZED) {
            runCatching { ar.release() }
            return false
        }
        // 用户指定的那支麦（null = 跟随系统）。失败不当错误：偏好而已，系统可以不听
        if (device != null) runCatching { ar.setPreferredDevice(device) }

        val out = ByteArrayOutputStream(SAMPLE_RATE * 2 * 8)
        record = ar
        pcm = out
        recording = true

        ar.startRecording()
        worker = thread(name = "echo-recorder", isDaemon = true) {
            val buf = ByteArray(bufSize)
            while (recording) {
                val n = try {
                    ar.read(buf, 0, buf.size)
                } catch (_: Exception) {
                    -1
                }
                if (n > 0) out.write(buf, 0, n) else if (n < 0) break
            }
        }
        return true
    }

    /** 已录到多少毫秒（界面显示计时用）。 */
    fun elapsedMs(): Long {
        val bytes = pcm?.size() ?: 0
        return bytes * 1000L / (SAMPLE_RATE * 2L)
    }

    /** 停止并返回 **wav 字节**（PCM 太短则返回 null）。 */
    fun stop(): ByteArray? {
        if (!recording && record == null) return null
        recording = false
        runCatching { worker?.join(1500) }
        worker = null

        val ar = record
        record = null
        runCatching { ar?.stop() }
        runCatching { ar?.release() }

        val data = pcm?.toByteArray() ?: ByteArray(0)
        pcm = null
        // 太短（< 0.3 秒）不是"说了一句话"，直接当没录到，别让后端白跑一趟
        if (data.size < SAMPLE_RATE * 2 * 3 / 10) return null
        return wrapWav(data, SAMPLE_RATE, CHANNELS, BITS)
    }

    /** 立刻丢弃（用户取消）。 */
    fun cancel() {
        recording = false
        runCatching { worker?.join(500) }
        worker = null
        val ar = record
        record = null
        runCatching { ar?.stop() }
        runCatching { ar?.release() }
        pcm = null
    }

    val isRecording: Boolean get() = recording

    companion object {
        const val SAMPLE_RATE = 16000
        private const val CHANNEL = AudioFormat.CHANNEL_IN_MONO
        private const val ENCODING = AudioFormat.ENCODING_PCM_16BIT
        private const val CHANNELS = 1
        private const val BITS = 16

        /** 给 PCM 套一个 44 字节的标准 wav 头（实现与用例都在 [WavWriter]）。 */
        fun wrapWav(pcm: ByteArray, sampleRate: Int, channels: Int, bits: Int): ByteArray =
            WavWriter.wrap(pcm, sampleRate, channels, bits)
    }
}
