# Android App：锁屏继续录音 + 调用 PC 版 TTS 实现指南

> 版本：2026-10-31（源码基线：ECHO 3.0；Android 参考实现见 `android/`，PC 端见 `app/`）
> 目的：给另一个负责 App 开发的 agent 一份可直接照做的实现说明。
> 参考实现：ECHO 现有 Android App（`android/`）与 PC 端（`app/`）。
> 本文只讲两件事：
> 1. **锁屏后继续录音/听写**（不是“锁屏界面显示”，而是后台采集不中断）；
> 2. **调用 PC 版 TTS，把文字变成音频在手机上播放**（以及何时回退到手机系统 TTS）。
>
> 先给结论：
> - 锁屏录音靠的是 **前台 Service（foreground service）+ 麦克风前台类型 + Service 内的 AudioRecord**，
>   **不是** Activity、悬浮窗或“保活黑科技”。上传/补传时再额外拿 `PARTIAL_WAKE_LOCK + WifiLock`。
> - PC TTS 是 **`POST /api/tts` 返回音频字节**，手机把字节写成临时文件，用 `MediaPlayer` 异步播放；
>   PC 端不负责在电脑喇叭上播。手机系统 TTS 是回退档。

---

## 0. 给另一个 Agent 的任务边界

### 0.1 必须做到

- App 退到后台、按电源键锁屏、屏幕熄灭后：
  - 会议录音继续采集、继续按段落盘；
  - 或“常驻听写”继续听，说完一句仍能发指令、收到回复并朗读；
  - 通知栏/锁屏上能看到当前状态，并能暂停/继续/停止。
- 录音不能在 `Activity` / `ViewModel` / Compose 页面里持有。
- 网络断、进程被系统回收、手机重启后：
  - 已落盘的音频不能丢；
  - **不能**在用户没重新点开关的情况下自动开麦。
- “ECHO 念”时：
  - 调 `POST /api/tts` 拿 `audio/mpeg` 或 `audio/wav` 字节；
  - 手机本地播放，音色与 PC 一致；
  - 失败时如实告诉用户，并在语音链路里回退手机系统 TTS（不能静默没声音）。

### 0.2 不要做

- 不要把录音放在 Activity 生命周期里（锁屏、切后台、旋转都会出问题）。
- 不要用普通 `startService()` 去启动带麦克风的前台服务（Android 8+ 会抛/被限制）。
- 不要在 `onStartCommand(null)` 时自动重新开麦（用户没同意）。
- 不要无限期持有 `PARTIAL_WAKE_LOCK`；不要在 `finally`/`onDestroy` 忘记释放。
- 不要拿 `/api/control/tts/test` 给手机用——那是让 **PC 本机喇叭**响的测试接口。
- 不要把 `8900/8901` 当 ECHO API 端口：手机连的是 ECHO 面板/API 的 **8970**（可配置，以 `data/echo-port.txt` 为准）。
- 不要假设 TTS 一定是 MP3：`edge-tts` 是 `audio/mpeg`，离线引擎是 `audio/wav`，按响应头判断。
- 不要对 `409`（朗读被关闭）、`422`（空文本）、`401`（令牌无效）做无脑重试。

### 0.3 参考源文件（建议按顺序读）

| 模块 | 文件 |
|---|---|
| 清单/权限/服务声明 | `android/app/src/main/AndroidManifest.xml` |
| 常驻听写服务 | `android/app/src/main/java/com/echo/contact/service/ListenService.kt` |
| 会议手机麦服务 | `android/app/src/main/java/com/echo/contact/service/MeetingMicService.kt` |
| 补传前台/锁的纯逻辑 | `android/app/src/main/java/com/echo/contact/service/SyncForeground.kt` |
| Android 锁实现 | `android/app/src/main/java/com/echo/contact/service/SyncForegroundAndroid.kt` |
| 回声消除/采集源 | `android/app/src/main/java/com/echo/contact/audio/EchoCancel.kt` |
| TTS 播放器 | `android/app/src/main/java/com/echo/contact/audio/EchoTtsPlayer.kt` |
| 手机系统 TTS | `android/app/src/main/java/com/echo/contact/audio/SystemTts.kt` |
| “谁念回复”偏好 | `android/app/src/main/java/com/echo/contact/service/SpeakPrefs.kt` |
| 朗读入口/路由 | `android/app/src/main/java/com/echo/contact/ui/MainViewModel.kt` |
| Retrofit 接口 | `android/app/src/main/java/com/echo/contact/data/EchoApi.kt` |
| TTS 入参 DTO | `android/app/src/main/java/com/echo/contact/data/dto/TtsDtos.kt` |
| PC TTS 端点 | `app/api.py::post_tts`、`app/audio/tts.py::synthesize` |
| PC TTS 设置 | `app/config.py::DEFAULTS["ttsEngine"]` |

---

## 1. 总体架构

```
┌────────────────────────── Android App 进程 ──────────────────────────┐
│                                                                       │
│  Compose UI / MainActivity                                            │
│    │ 只负责：开关、状态展示、权限申请、点通知进入                        │
│    ▼                                                                  │
│  Foreground Service（麦克风类型）                                     │
│    ├─ AudioRecord 16 kHz / mono / PCM16                               │
│    ├─ VoiceSegmenter（静音分段）或按时间切段                            │
│    ├─ 落盘 WAV/FLAC（应用私有目录）                                     │
│    ├─ 通知（锁屏唯一可见状态；暂停/继续/停止）                          │
│    └─ 上传协程（补传时进 dataSync 前台 + WakeLock/WifiLock）            │
│                                                                       │
│  另一条：TTS 播放器                                                    │
│    ├─ POST /api/tts → 音频字节                                        │
│    ├─ cacheDir 临时文件 → MediaPlayer.prepareAsync()                  │
│    └─ 失败 → 手机系统 TextToSpeech（可离线）                            │
│                                                                       │
└──────────────────────────────┬────────────────────────────────────────┘
                               │  HTTP（局域网/配对令牌）
                               ▼
┌──────────────────────── ECHO PC（默认 8970）─────────────────────────┐
│  /api/assistant/voice-command  转写 + 下发                            │
│  /api/commands/{id}            轮询结果                               │
│  /api/tts                      合成音频字节（不播）                    │
│  /api/meeting/*、/api/meetings/*  会议录音/转写                        │
└──────────────────────────────────────────────────────────────────────┘
```

两条容易混淆的链路要分开实现：

| 链路 | 服务 | 是否常驻 | 锁屏要求 |
|---|---|---|---|
| **会议录音**（手机当麦克风） | `MeetingMicService` | 录音期间常驻；录完可停 | 录音继续、分段继续落盘；上传也要活 |
| **常驻听写**（开车说一句） | `ListenService` | 用户开关期间一直常驻 | 继续听、继续发指令、继续念回复 |
| 补传（把手机录音发给 PC） | `MeetingMicService` 的 `ACTION_SYNC` 前台路径 | 有东西要传才进前台 | 拿 `PARTIAL_WAKE_LOCK + WifiLock` |

---

# 第一部分：锁屏继续录音

## 2. 核心原则：状态属于 Service，不属于页面

这是整件事最重要的一条。页面只做三件事：

1. 申请权限；
2. 用 `startForegroundService()` 启动/停止 Service；
3. 观察 Service 暴露的 `StateFlow` 并渲染。

录音、分段、写文件、上传、播放 TTS，全部在 Service 的协程/线程里。这样：

- 用户按 Home、锁屏、屏幕熄灭：Activity 可能 `onStop()`，但 Service 仍是前台，采集循环继续。
- 用户从最近任务划掉 App：默认 Service 不会因此停止（想要“划掉也停”才在 `onTaskRemoved` 处理；ECHO 选择继续听）。
- 页面旋转/重建：不会影响录音对象。

> 反例：在 `MainViewModel` 或 Compose `DisposableEffect` 里 new `AudioRecord`。页面一走就 `release`，锁屏必断。

## 3. Manifest：权限与前台服务类型

参考 `android/app/src/main/AndroidManifest.xml`：

```xml
<uses-permission android:name="android.permission.INTERNET"/>
<uses-permission android:name="android.permission.RECORD_AUDIO"/>

<!-- 前台服务本身 -->
<uses-permission android:name="android.permission.FOREGROUND_SERVICE"/>
<!-- Android 14：麦克风型前台服务必须单独声明这个权限 -->
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_MICROPHONE"/>
<!-- 如果同一个服务还负责上传/补传：dataSync 类型要这个 -->
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_DATA_SYNC"/>

<!-- Android 13+ 通知权限；不给也能启动前台服务，但通知栏看不到 -->
<uses-permission android:name="android.permission.POST_NOTIFICATIONS"/>

<!-- 补传时拿 PARTIAL_WAKE_LOCK / WifiLock 才需要 -->
<uses-permission android:name="android.permission.WAKE_LOCK"/>
<uses-permission android:name="android.permission.ACCESS_WIFI_STATE"/>

<!-- 引导用户把 App 加进“忽略电池优化”；只在用户点按钮时请求 -->
<uses-permission android:name="android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS"/>

<application ...>
    <service
        android:name=".service.ListenService"
        android:exported="false"
        android:foregroundServiceType="microphone"/>

    <!-- 录音 + 补传两条前台路径，所以类型是两个；不是让你同时干两件事 -->
    <service
        android:name=".service.MeetingMicService"
        android:exported="false"
        android:foregroundServiceType="microphone|dataSync"/>
</application>
```

要点：

- `exported=false`：只允许本 App 启动，防止外部乱拉麦克风。
- **Android 14 起类型必须与真实行为匹配**：
  - 录音 → `microphone`；
  - 上传/补传 → `dataSync`；
  - 边录边传 → `microphone|dataSync`，`startForeground` 时把两个类型都传上。
- 如果只做录音，不要为了“保活”乱加 `dataSync`；类型对不上会被系统拒绝或在商店审核被问。
- 若目标 SDK 35，Android 15 对 `dataSync` 有 6 小时运行上限；本 App 的补传是短任务，正常不会到上限。

## 4. 启动前台服务：`startForegroundService` 的 5 秒承诺

### 4.1 启动入口

```kotlin
fun start(context: Context) {
    val i = Intent(context, ListenService::class.java)
        .setAction(ACTION_START)
    ContextCompat.startForegroundService(context, i)
}
```

`startForegroundService()` 不是“起一个后台服务”，而是向系统承诺：

> 这个 Service 会在 **5 秒左右**内调用 `startForeground()`，否则按 ANR
> （`ForegroundServiceDidNotStartInTimeException`）处理。

所以正确的顺序是：

```
startForegroundService()
  → onStartCommand()
    → 检查 RECORD_AUDIO 权限
    → 创建通知渠道
    → startForeground(id, notification, type)   ← 必须很快
    → 打开 AudioRecord / 启动采集协程
```

### 4.2 `onStartCommand` 的标准形状

参考 `ListenService.kt`：

```kotlin
override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    if (intent == null) {
        // 系统回收后 START_STICKY 会带 null intent 把服务壳拉回来。
        // 麦克风只能由用户点开关打开：这里绝不自动开麦。
        state.reset(Phase.OFF, "服务被系统重启过，要用请再打开开关")
        stopSelf()
        return START_NOT_STICKY
    }
    when (intent.action) {
        ACTION_STOP -> { stopEverything(); return START_NOT_STICKY }
        ACTION_PAUSE -> { if (running) setUserPaused(true) else stopSelf(); return START_NOT_STICKY }
        ACTION_RESUME -> { if (running) setUserPaused(false) else stopSelf(); return START_NOT_STICKY }
        else -> {
            if (!ensureForeground()) return START_NOT_STICKY
            if (job == null) job = scope.launch { runLoop() }
        }
    }
    return START_STICKY
}
```

注意：

- `intent == null` 必须是“停下来，不自动开麦”，不能图省事直接 `startForeground` 再开麦。
- `START_STICKY` 只保证“系统愿意重建服务壳”，**不保证重建时用户还想录音**。
- 用户从最近任务划掉 App，默认不会触发 `onDestroy`；如果你希望划掉就停，要在 `onTaskRemoved()` 里主动停并撤通知。

### 4.3 `startForeground` 的写法

```kotlin
private fun ensureForeground(): Boolean {
    if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
        != PackageManager.PERMISSION_GRANTED) {
        // 没权限：不要 startForeground 后再去开麦
        state.fail("没有麦克风权限")
        stopSelf()
        return false
    }
    createChannel()   // 必须：O+ 没有渠道，通知不显示
    val n = buildNotification()
    return try {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID, n,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            )
        } else {
            startForeground(NOTIFICATION_ID, n)
        }
        true
    } catch (e: Exception) {
        // Android 14 对麦克风 FGS 查得严：权限/后台启动限制会在这里炸
        state.fail("开不了前台服务：${e.message}")
        stopSelf()
        false
    }
}
```

`MeetingMicService` 的 `startRecording()` 也是先 `startForegroundCompat()`，再启动采集协程；不要在采集开始后再补前台。

## 5. 通知：锁屏时用户唯一看得见的东西

前台服务**必须有**一条常驻通知（Android 8+ 的硬要求），它同时是锁屏时用户判断“它到底在干什么”的唯一依据。

### 5.1 渠道

```kotlin
val ch = NotificationChannel(
    CHANNEL_ID,
    "后台听写",
    NotificationManager.IMPORTANCE_LOW   // 常驻状态，不要响/不要横幅
).apply {
    description = "一直听着，说完一句就交给 ECHO"
    setShowBadge(false)
}
```

### 5.2 通知内容

```kotlin
NotificationCompat.Builder(this, CHANNEL_ID)
    .setSmallIcon(R.drawable.ic_echo_mic)
    .setContentTitle(title)              // e.g. “ECHO 正在听”
    .setContentText(body)                // e.g. “说完一句就会发出去”
    .setOngoing(true)                    // 不可滑掉
    .setSilent(true)
    .setShowWhen(false)
    .setContentIntent(openApp())         // 点通知回 App
    .addAction(0, if (paused) "继续" else "暂停", togglePause())
    .addAction(0, "停止听写", stopAction())
    .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
    .build()
```

要点：

- `setOngoing(true)`：前台通知不应被用户误滑掉；暂停/停止用 action。
- `setForegroundServiceBehavior(IMMEDIATE)`：Android 12+ 减少通知延迟。
- `PendingIntent` 用 `FLAG_IMMUTABLE`（Android 12+ 强制）。
- 通知文案必须随状态变：正在听 / 处理中 / 朗读中 / 失败原因 / 已暂停。
- 通知 action 要能直接调用 Service（`PendingIntent.getService`），不要依赖 Activity 是否活着。

### 5.3 通知权限

Android 13+ 需要 `POST_NOTIFICATIONS`。ECHO 的处理：

- 启动听写时申请；
- **用户拒绝也允许继续**，只是通知栏看不到（前台服务仍能跑，系统任务管理器仍可见）；
- 在设置页明确告诉用户“不给通知权限，锁屏看不到状态”。

不要因为通知权限被拒就拒绝提供录音功能；但要在 UI 上说清风险。

## 6. 音频采集参数与分段

参考 `ListenService` 与 `MeetingMicService`：

```kotlin
const val SAMPLE_RATE = 16000
const val CHANNEL = AudioFormat.CHANNEL_IN_MONO
const val ENCODING = AudioFormat.ENCODING_PCM_16BIT
const val FRAME_SAMPLES = SAMPLE_RATE / 10   // 100ms 一帧

val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL, ENCODING)
val ar = AudioRecord(
    MediaRecorder.AudioSource.VOICE_RECOGNITION,
    SAMPLE_RATE, CHANNEL, ENCODING, minBuf * 2
)
if (ar.state != AudioRecord.STATE_INITIALIZED) { ar.release(); fail(...) }
ar.startRecording()
```

约定：

- **会议录音/语音指令**：16 kHz、单声道、PCM16，服务端 ASR 就是这个格式。
- **采集源**：
  - 普通录音/听写：`VOICE_RECOGNITION`；
  - 需要“边播边听”（全双工）且设备支持 AEC：`VOICE_COMMUNICATION` + `AcousticEchoCanceler`；
  - 拿不到 AEC 就**降级为半双工**（念的时候暂停采音），并在状态里说明；不要假装全双工。
- `setPreferredDevice()` 是**偏好**，系统可以不听（蓝牙尤其会）；失败不要当致命错误，只把真实结果告诉用户。
- **缓冲不要装箱**：
  - 10 分钟 16k 单声道 = 960 万样本；
  - `ArrayList<Short>` 装箱后可能约 190 MB，Android 直接 OOM；
  - 用 `ShortArray`/自定义 `PcmBuffer`，10 分钟约 19 MB。
- 分段：
  - 按静音收尾（`VoiceSegmenter`）适合“说一句”；
  - 会议按固定时长（如 10 分钟）切段，边录边落盘；
  - 每段写 WAV/FLAC，文件名带递增序号；写盘必须在 IO 线程。
- **不要在 Activity 中读取麦克风**；`AudioRecord.read()` 放在 Service 的专用协程/线程。

### 6.1 边播边听（可选）

- 半双工：TTS 播放时 `paused = true`，不把音频喂给分段器；播放结束再 `paused = false`。
  - 代价：念回复的几秒里用户插话会丢；
  - 好处：不会把自己念的话再识别一遍（自激）。
- 全双工：仅当用户开启 **且** `AcousticEchoCanceler.isAvailable()`：
  - 采集源切 `VOICE_COMMUNICATION`；
  - `AcousticEchoCanceler.create(audioSessionId).enabled = true`；
  - 拿不到就如实降级，别静默。
- 不要为了全双工去改 `AudioManager.mode = MODE_IN_COMMUNICATION`：会把声音默认路由到听筒，车里等于听不见。

## 7. 锁屏存活：真正起作用的是什么

### 7.1 当前 ECHO 的做法（照这个做）

- 录音/听写全程在 **foreground service** 中；
- Service 类型 = `microphone`；
- 通知常驻；
- 采集循环是 Service 自己的协程；
- 没有依赖 Activity、悬浮窗、`WindowManager` 或“锁屏显示”。

**当前 ECHO 的采集服务本身没有持有 `PARTIAL_WAKE_LOCK`**。锁屏后仍能录，靠的是：

1. 前台服务让进程不被当成后台缓存进程回收；
2. 正在进行的 `AudioRecord` 让音频链路保持活跃；
3. 用户把 App 加入电池优化白名单（设置页有入口）。

> 必须诚实说明：这不是“所有 OEM 都 100% 保证”。小米/华为/OPPO 的省电策略、Doze、App Standby
> 仍可能冻结进程。前台服务是必要条件，不是万能药。

### 7.2 如果目标机型要求更强保证

可以在**录音期间**额外拿一个 `PARTIAL_WAKE_LOCK`，但必须遵守：

```kotlin
val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
val wl = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "echo:recording")
wl.setReferenceCounted(false)
wl.acquire(timeoutMs)          // 必须有超时，比如 10 分钟
// 定时续锁（release + acquire），或每段续一次
// finally / onDestroy 必须 release
```

注意：

- 长时间会议不要一次 `acquire()` 几个小时；用“分段超时 + 续锁”，并在 `onDestroy` 兜底释放。
- 不要为了“保活”常驻一个不释放的锁：用户手机整夜不睡，比录音断更糟。
- 如果只是普通录音，优先先测前台服务是否已经够；不要一上来就加锁。

### 7.3 电池优化白名单

Manifest：

```xml
<uses-permission android:name="android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS"/>
```

只在用户点按钮时发：

```kotlin
val intent = Intent(
    Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
    Uri.parse("package:$packageName")
).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
startActivity(intent)
```

- 不要在 `init` / `LaunchedEffect` 自动弹系统确认框，容易被当恶意软件。
- 设置页给出“去关闭电池优化”的入口，并说明“国产系统可能还需要允许自启动/后台运行”。

### 7.4 OEM 额外项

- 小米：设置 → 应用 → 权限 → 自启动；省电策略 → 无限制。
- 华为：应用启动管理 → 手动管理，允许自启动/关联启动/后台活动。
- OPPO/vivo：允许后台运行、关闭“智能省电”对该应用的限制。
- 这些没有统一 API；只能给用户入口 + 文档说明，不能代码里替用户改。

## 8. 上传/补传：WakeLock + WifiLock 的正确姿势

录音本身可以不拿锁，但**补传大文件**必须防“锁屏后进程被冻结”。ECHO 的做法：

### 8.1 两个前台类型

同一个 `MeetingMicService`：

- 录音：`FOREGROUND_SERVICE_TYPE_MICROPHONE`
- 补传：`FOREGROUND_SERVICE_TYPE_DATA_SYNC`
- 录音中补传：`MICROPHONE or DATA_SYNC`

`startForeground` 按路径切类型；不要用一个 `dataSync` 服务去偷偷开麦。

### 8.2 两个锁

```kotlin
// ① CPU 不睡：必须带超时
val wl = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "echo:sync-upload")
wl.setReferenceCounted(false)
wl.acquire(timeoutMs)

// ② WiFi 不降速：系统没有超时参数，自己加看门狗
val wifiLock = wm.createWifiLock(
    WifiManager.WIFI_MODE_FULL_HIGH_PERF, "echo:sync-wifi"
)
wifiLock.setReferenceCounted(false)
wifiLock.acquire()
scope.launch {
    delay(timeoutMs)
    releaseLocks()   // 到点无条件释放
}
```

### 8.3 超时怎么估

参考 `SyncForeground.kt`：

- 下限吞吐 `32 KB/s`（弱网/中继的保守值）；
- `timeout = bytes / 32KB/s * 1000 + 5min`；
- 夹在 `5min` 到 `30min` 之间；
- `30min` 是“忘记释放时的保险丝”，不是“允许传更久”。

### 8.4 生命周期顺序（唯一写法）

```kotlin
fun guard(...) {
    begin()                  // 1. 先 startForeground，再拿锁
    try {
        block()              // 2. 真正的上传，可能几分钟
        finish(endOf(result))// 3. 撤前台 + 放锁 + 发收尾通知
    } catch (t: Throwable) {
        finish(Cancelled(...))
        throw t
    } finally {
        releaseAll()         // 4. 双保险：无论哪条路都放干净
    }
}
```

`onDestroy()` 里**再释放一次**（幂等）。原因：服务被系统直接收掉时，不一定会走 `finally`；一个没释放的
`PARTIAL_WAKE_LOCK` 会让用户手机整夜不睡。

### 8.5 通知

- 补传通知：`ongoing=true`，标题“正在上传会议录音”，正文“已传 2/5 段（第 3 段 12.4 MB）”；
- 传完 `stopForeground(STOP_FOREGROUND_REMOVE)`，再发一条普通“传完了/没成”的收尾通知；
- 录音中的通知不能被补传通知替换掉：录音时补传要复用录音那条通知，结束时也不能撤它。

## 9. 进程被杀 / 手机重启后的恢复

ECHO 的约定：

- `START_STICKY` 只重建服务壳；`onStartCommand(null)` 时**不自动开麦**，把状态写成“服务被系统重启过，要用请再打开开关”，然后 `stopSelf()`。
- 会议录音的草稿（清单 + 已落盘音频）保留在私有目录；下次启动时：
  - 把“上次进程被杀、结束时间未知”的草稿按最后一段推断收尾；
  - 标记为“推断的结束时间”，让用户知道不是精确值；
  - 由补传逻辑继续上传。
- 进程被杀时，**内存里未封段的尾段补不回来**；这一点必须如实告诉用户，不能因为清单收尾了就说“整场都在”。
- 进程重启后，用户没重新点“开始录音”之前，不要自动开新会。

## 10. 锁屏验收清单（必须真机做）

### 10.1 会议录音

1. 开始录音，按电源键锁屏 10 分钟；
2. `adb shell dumpsys activity services <package>` 确认 Service 是 foreground；
3. 解锁后停止录音；
4. 检查：
   - 会议时长与真实时间一致；
   - 分段文件数量/大小持续增长；
   - 没有“0 字节”或只录到锁屏前几秒；
   - 上传在锁屏期间有进度，解锁后不必手点重试。

### 10.2 常驻听写

1. 打开听写，锁屏；
2. 对手机说一句指令（蓝牙耳机/车机麦克风更好）；
3. 确认：
   - 通知变成“处理中/朗读中”；
   - 回复能听见；
   - 说完后自动回到“正在听”；
   - 屏幕全程熄灭也不影响。

### 10.3 进程死亡

```bash
adb shell am force-stop <package>
# 等几秒，再打开 App
```

预期：

- 不会自动开麦；
- 已落盘录音还在，可继续补传；
- 界面明确显示“上次被系统停止”，不是假装还在录。

### 10.4 锁与电量

```bash
adb shell dumpsys power | findstr echo
adb shell dumpsys battery
```

- 补传结束后不应还有 `echo:sync-upload` / `echo:sync-wifi`；
- 锁屏 30 分钟待机耗电不应异常（有锁也不能整夜不睡）。

---

# 第二部分：调用 PC 版 TTS

## 11. 先分清三种“念”

| 方式 | 谁合成 | 网络 | 音色 | 用途 |
|---|---|---|---|---|
| **PC TTS**（本文重点） | ECHO PC | 必须能连 PC | 与电脑一致 | “ECHO 念” |
| **手机系统 TTS** | 手机厂商引擎 | 可能离线 | 与电脑不同 | 默认/回退 |
| **PC 本机播放**（`/api/control/tts/test`） | ECHO PC | 本机 | 与电脑一致 | 只在 PC 上测试，**不要给手机用** |

ECHO 的默认是**手机念**（`SpeakPrefs.ENGINE_SYSTEM`）；用户在设置里选“ECHO 念”才走 `/api/tts`。
新 App 也建议这样：默认不依赖网络，用户明确选择后再用 PC 音色。

## 12. PC 端接口契约（以现有实现为准）

### 12.1 请求

```http
POST http://<PC-LAN-IP>:8970/api/tts
Authorization: Bearer <配对后拿到的 token>
Content-Type: application/json

{"text": "你好，我是 ECHO。", "engine": ""}
```

字段：

- `text`：要念的文字，必填；空白 → `422`。
- `engine`：留空 = 用 PC 设置里的 `ttsEngine`。一般**不要传**；只有做联调/测试时才显式指定。
  - `auto`：优先 edge-tts，失败降级本机离线引擎；
  - `edge-tts`：只走在线，失败就 `502`；
  - Windows 离线引擎 `sapi` / macOS `say`：只走离线；
  - `off`：朗读被关闭 → `409`。
- **没有** `format` / `voice` / `rate` / `volume` 参数。格式由引擎决定；不要发明这些字段。

### 12.2 成功响应

```http
HTTP/1.1 200 OK
Content-Type: audio/mpeg        # 或 audio/wav
Content-Length: 13968
X-Echo-Tts-Engine: auto
Cache-Control: no-store
<二进制音频字节>
```

- `audio/mpeg`：edge-tts 在线；
- `audio/wav`：离线引擎；
- `X-Echo-Tts-Engine`：本次请求的 engine 值（例如 `auto`），**不是** auto 降级后的实际引擎；
  不要拿它判断音色；
- `Cache-Control: no-store`：不要跨文本复用缓存；每次文本变化重新请求；
- 响应是**完整音频**，不是流式；适合短句/简报，不适合整篇长文。

实测（2026-10-31，dev ECHO，`text="你好，我是 ECHO。"`，`engine=""`）：

```
HTTP 200, Content-Type: audio/mpeg, Content-Length: 13968,
X-Echo-Tts-Engine: auto, Cache-Control: no-store
耗时约 2.0 秒，音频头 FF F3 64 C4（MP3）
```

### 12.3 错误响应

| HTTP | 含义 | 客户端动作 |
|---|---|---|
| `401` | 令牌无效/缺失 | 重新配对/刷新 token；不要重试 |
| `422` | `text` 为空 | 不请求；本地校验 |
| `409` | `ttsEngine=off`（朗读被关闭） | 提示用户去 PC 设置打开；不要重试 |
| `502` | 合成失败（在线/离线引擎失败） | 显示原因；语音链路回退手机 TTS；最多重试 1 次 |
| 网络错误/超时 | 连不上 PC | 回退手机 TTS 或提示；不要无限重试 |

`422` / `409` 的响应体是 JSON `{"detail": "..."}`；`502` 也是 JSON。不要把错误体当音频播放。

### 12.4 隐私边界（必须告诉用户）

- `ttsEngine=auto` / `edge-tts` 时，**文本会发往微软在线 TTS**（`speech.platform.bing.com`）；
- 要完全不出网：PC 设置选离线引擎（Windows SAPI / macOS say），或手机直接选“手机念”；
- 不要把“ECHO 念”说成“纯本地”；它只是“音频在 PC 合成”，文本可能出网。

## 12.5 能力后端 TTS（2026-10-31 新增，手机 App 优先走这条）

> 这一节是后端升级后的**推荐路径**；上面的 `/api/tts` 是 ECHO PC 的兼容接口。
> 手机 App 直接调能力后端，edge-tts 只在后端升级一次。

```http
POST http://<能力后端>:8900/v1/tts
Authorization: Bearer <能力后端 client token>
Content-Type: application/json

{"text":"你好，我是 ECHO。", "voice":"zh-CN-XiaoxiaoNeural", "rate":"+0%"}
```

成功：

```http
200 OK
Content-Type: audio/mpeg
X-Echo-Tts-Engine: edge-tts
X-Echo-Tts-Voice: zh-CN-XiaoxiaoNeural
Cache-Control: no-store
```

错误：

| HTTP | code | 含义 |
|---|---|---|
| `401/403` | `unauthorized` / `forbidden` | token 无效，或 client 没有 `tts` scope |
| `422` | `tts_text_invalid` | 空文本/超长/voice/rate 格式错 |
| `429` | `rate_limited` | 该 client 1 秒内已经请求过；`Retry-After: 1` |
| `502` | `tts_failed` | edge-tts 上游失败 |
| `503` | `tts_unavailable` | 后端关闭 TTS 或没装 edge-tts |

关键约束：

- **不占 ASR 的 GPU / Admission / 每日音频额度**；TTS 是独立网络通道；
- **每个认证 client 1 QPS**，手机和 ECHO 使用不同 client/token，各自独立计数；
- 鉴权关闭时所有请求都是 `anonymous`，会共用 1 QPS —— 要让手机/ECHO 分开算，
  **必须开后端鉴权并分别发 client**；
- 客户端收到 `429` 按 `Retry-After` 退避，不要无限重试；`409` 不会出现（TTS 不占 ASR 通道）；
- `edge-tts` 版本要求 **>=7.3.1**：更早版本实测会被微软 `403`（`Sec-MS-GEC`/Chromium 版本过期）；
- 后端升级 edge-tts：改 `server/requirements.txt`，重建镜像/重启后端；手机和 ECHO 不用改。

ECHO PC 侧也通过 `EchoServerClient.tts()` 调同一个 `/v1/tts`（返回 `(mime, bytes)`）；
手机和 ECHO 是两个 client，TTS 请求互不计入对方的 1 QPS。

## 13. 手机端调用流程

### 13.1 语音指令的完整时序

```
[手机]
 录音（AudioRecord，16k mono PCM16）
   → 加 WAV 头
   → POST /api/assistant/voice-command（裸 audio/wav body）
   ← {ok, text, commandId}
   → 轮询 GET /api/commands/{commandId}
   ← {status, brief, speakText, reply}
   → 选择要念的文本（brief / speakText，不是整篇 reply）
   → SpeechText.normalize()
   → POST /api/tts
   ← audio bytes
   → cacheDir 临时文件
   → MediaPlayer.prepareAsync() / start()
   → 播完恢复听写
```

ECHO 的 `SpeakPlan` 会优先选 `brief`/`speakText`；不要直接把整篇回复丢给 TTS，又慢又吵。

### 13.2 Retrofit 定义

```kotlin
data class TtsRequest(
    val text: String,
    val engine: String = ""     // 一般留空
)

interface EchoApi {
    @POST("api/tts")
    suspend fun tts(@Body body: TtsRequest): Response<ResponseBody>
}
```

注意：

- 路径带 `api/` 前缀；`baseUrl` 是 `http://PC:8970/`（结尾要有 `/`），不是 `/api`；
- 用 `Response<ResponseBody>` 拿原始字节，不要用 Gson 把响应当 JSON；
- 带 Bearer：

```kotlin
.addInterceptor { chain ->
    val token = tokenProvider()
    val req = if (!token.isNullOrBlank()) {
        chain.request().newBuilder()
            .header("Authorization", "Bearer $token")
            .build()
    } else chain.request()
    chain.proceed(req)
}
```

- 超时：TTS 通常几秒；建议 connect 10s、read 60s。不要用 960s 的长任务客户端。
- `409/422/401` 不重试；`502`/网络错误最多重试 1 次。

### 13.3 播放器实现

参考 `EchoTtsPlayer.kt`，最小实现：

```kotlin
class EchoTtsPlayer(private val context: Context) {
    private var player: MediaPlayer? = null

    fun cache(bytes: ByteArray, contentType: String?): File {
        val ext = when {
            contentType?.contains("wav") == true -> "wav"
            contentType?.contains("mpeg") == true -> "mp3"
            else -> "bin"
        }
        return File(context.cacheDir, "echo-tts.$ext").apply {
            writeBytes(bytes)       // IO 线程
        }
    }

    fun play(file: File, onFinished: () -> Unit) {
        stop()
        val mp = MediaPlayer()
        player = mp
        mp.setOnPreparedListener { it.start() }
        mp.setOnCompletionListener { onFinished(); stop() }
        mp.setOnErrorListener { _, _, _ -> onFinished(); stop(); true }
        try {
            // 可选：setPreferredDevice() 只是偏好，失败不算播放失败
            mp.setDataSource(file.absolutePath)
            mp.prepareAsync()       // 不能用 prepare() 阻塞主线程
        } catch (e: Exception) {
            onFinished(); stop()
        }
    }

    fun stop() {
        player?.let { mp ->
            runCatching { if (mp.isPlaying) mp.stop() }
            runCatching { mp.reset() }
            runCatching { mp.release() }
        }
        player = null
    }
}
```

要点：

- 网络/写文件在 `Dispatchers.IO`；`MediaPlayer` 的创建/`prepareAsync`/`start` 在主线程；
- 一次只保留一个 `MediaPlayer`；新朗读先 `stop()` 旧的；
- 播放完成/出错都要释放并回调，避免 `_speaking` 永远为 true；
- 音频文件放 `cacheDir`，不是外部存储；下次可覆盖；
- `audio/mpeg` 和 `audio/wav` 都支持；不要只按 `.mp3` 写死；
- 如果 App 需要“锁屏时也能念”，播放器必须在 **Service** 里，不能只挂在 Activity/ViewModel 上。

### 13.4 回退到手机系统 TTS

ECHO 的规则：

- 用户在设置里选“手机念”（默认）：直接 `TextToSpeech`，不请求 PC；
- 选“ECHO 念”：
  - 请求 `/api/tts` 成功 → 播字节；
  - 请求失败/播放失败 → **回退手机 TTS**，并把“ECHO 念没成，这句用手机念”写进通知/界面；
  - 手机系统 TTS 也不可用 → 如实提示“回复在通知里”，不能假装念过。

新 App 建议：

- **前台可见的朗读**：PC TTS 失败就显示错误，并给“用手机念”按钮；
- **后台/锁屏语音链路**：必须自动回退，否则用户以为 App 死了；
- 回退要留原因，不要静默。

手机系统 TTS 初始化：

```kotlin
val tts = TextToSpeech(context) { status ->
    if (status == TextToSpeech.SUCCESS) {
        val r = tts.setLanguage(Locale.SIMPLIFIED_CHINESE)
        available = r != TextToSpeech.LANG_MISSING_DATA &&
                    r != TextToSpeech.LANG_NOT_SUPPORTED
    }
}
```

Android 11+ 包可见性：如果 App 需要枚举/使用系统 TTS 引擎，Manifest 里要有：

```xml
<queries>
    <intent>
        <action android:name="android.intent.action.TTS_SERVICE"/>
    </intent>
</queries>
```

否则 `TextToSpeech` 可能报“没有朗读引擎”，但系统其实装着引擎。

### 13.5 文本归一化

ECHO 在两条 TTS 路径前都会调用 `SpeechText.normalize()`：

- `8-19 度` → `8 到 19 度`（否则被念成“八百一十九”）；
- 只改送去合成的文本，界面/历史里的原文不动；
- 归一化必须幂等，避免“8 到 到 19”；
- PC 端 `synthesize()` 也会再归一化一次，客户端再做一次是双保险，不是重复 bug。

## 14. 锁屏下的 TTS 生命周期

如果 TTS 和常驻听写在同一个 Service：

```
LISTENING
  → 收到一句 → PROCESSING（paused=true，半双工）
  → 拿到回复 → SPEAKING
      → PC TTS 或系统 TTS
      → 播完 → paused=false
  → LISTENING
```

- 半双工：念的时候不收音，避免自激；
- 全双工：AEC 可用时才边念边听；
- 播放器在 Service 的 `onDestroy()` 里必须 `stop()`/`release()`；
- 新一句朗读开始前先停旧的，避免两路声音叠在一起；
- 音频焦点（AudioFocus）当前 ECHO 没有显式请求；新 App 如果要和音乐/导航共存，建议：
  - 请求 `AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK`；
  - 播完 `abandonAudioFocusRequest()`；
  - 不要改 `AudioManager.mode = MODE_IN_COMMUNICATION`（会把声音送听筒）。

## 15. 测试与验收

### 15.1 PC TTS 接口

```bash
# 成功：应看到 Content-Type + X-Echo-Tts-Engine
curl -sS -D - -o /tmp/echo-tts.bin \
  -X POST "http://192.168.1.10:8970/api/tts" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好，我是 ECHO。","engine":""}'

file /tmp/echo-tts.bin
```

预期：

- `200`，`Content-Type: audio/mpeg` 或 `audio/wav`；
- 文件非空，能被播放器打开；
- `engine=off` → `409`；
- 空 `text` → `422`；
- 错 token → `401`。

### 15.2 手机端

1. 设置选“ECHO 念”，锁屏说一句：
   - 能听到与 PC 一致的声音；
   - 播完回到“正在听”；
   - 通知显示“朗读中”再回“正在听”。
2. 关掉 PC 或断网，再说一句：
   - 不应崩溃/静默；
   - 语音链路回退手机 TTS，并显示/念出原因。
3. PC 设置 `ttsEngine=off`：
   - 请求返回 `409`，App 提示“朗读被关闭”，不重试。
4. 长文本：
   - 只念 `brief`/`speakText`，不要整篇回复；
   - 如果必须念长文，先截断/分段，并考虑请求超时与流量。
5. 锁屏 10 分钟连续听写：
   - TTS 播放不导致服务被杀；
   - 播放结束能继续听；
   - 没有自激（除非明确开启全双工且设备支持 AEC）。

### 15.3 失败留痕

至少记录：

- 请求 URL（不含 token）、HTTP 状态、Content-Type、字节数、耗时；
- 失败原因（网络/401/409/422/502/播放器错误）；
- 回退到了系统 TTS 还是彻底失败。

不要把 Bearer token 打进日志。

---

# 第三部分：给新 App 的落地顺序

## 16. 推荐实现顺序

1. **前台服务骨架**
   - Manifest 权限/服务类型；
   - `startForegroundService` + 5 秒内 `startForeground`；
   - 通知渠道 + 常驻通知 + 暂停/停止 action；
   - `onStartCommand(null)` 不自动开麦。
2. **采集与分段**
   - `AudioRecord` 16k/mono/PCM16；
   - 100ms 帧、静音分段或定时切段；
   - 写 WAV/FLAC 到私有目录；
   - 锁屏 10 分钟验收。
3. **持久化/恢复**
   - 每段落盘 + 清单；
   - 进程被杀后的“推断收尾”；
   - 重启不自动开麦。
4. **上传/补传**
   - `dataSync` 前台路径；
   - `PARTIAL_WAKE_LOCK + WifiLock`（带超时）；
   - `finally`/`onDestroy` 释放；
   - 录音中补传时复用录音通知。
5. **TTS**
   - `TtsRequest` + Retrofit；
   - `MediaPlayer` 播放器；
   - `SpeakPrefs` 默认手机念；
   - 失败回退系统 TTS + 原因。
6. **锁屏/后台验收**
   - 会议录音、常驻听写、补传、进程死亡、OEM 省电。
7. **权限与引导**
   - RECORD_AUDIO、POST_NOTIFICATIONS；
   - 电池优化白名单入口；
   - 国产 ROM 自启动/后台说明。

## 17. Definition of Done

- [ ] 锁屏 10 分钟，会议录音文件持续增长，时长正确。
- [ ] 锁屏期间上传有进度，解锁后不需要手点重试。
- [ ] 补传结束后所有 WakeLock/WifiLock 已释放。
- [ ] `startForegroundService` 后 5 秒内一定 `startForeground`，无 ANR。
- [ ] 进程被系统杀后不会自动开麦；已落盘音频可恢复/补传。
- [ ] `POST /api/tts` 成功时能播放 `audio/mpeg` 和 `audio/wav`。
- [ ] `409/422/401` 不重试；`502`/网络失败最多重试 1 次。
- [ ] ECHO 念失败时语音链路回退手机 TTS，且用户能知道原因。
- [ ] 日志不包含 token；错误可定位到 HTTP 状态/异常类型。
- [ ] 真机（至少一台国产 ROM）锁屏/后台测试通过。

## 18. 最容易踩的坑（速查）

1. **把录音放 Activity** → 锁屏/切后台必断。
2. **用 `startService` 起麦克风服务** → Android 8+ 限制/崩溃；必须 `startForegroundService`。
3. **`startForeground` 太晚** → `ForegroundServiceDidNotStartInTimeException`。
4. **Android 14 没声明 `FOREGROUND_SERVICE_MICROPHONE`** → `SecurityException`。
5. **`onStartCommand(null)` 自动开麦** → 用户没同意，且可能违反隐私预期。
6. **WakeLock 不释放/无超时** → 手机整夜不睡；必须 `finally` + 超时 + `onDestroy` 兜底。
7. **`WifiLock` 没有超时** → 自己加看门狗。
8. **TTS 只按 `.mp3` 处理** → 离线引擎返回 wav，播不了。
9. **用 `/api/control/tts/test` 给手机** → 声音在 PC 喇叭，手机没声音。
10. **把 `8900/8901` 当 API** → 那是能力后端；手机 API 是 8970。
11. **对 409/422 重试** → 永远失败；409 是“朗读关掉了”，去改设置。
12. **TTS 失败静默** → 用户以为 App 坏了；必须回退或明确提示。
13. **主线程 `MediaPlayer.prepare()`** → ANR；用 `prepareAsync()`。
14. **锁屏 TTS 播放器挂在 Activity/ViewModel** → 锁屏/退后台播放会被打断。
15. **把“ECHO 念”说成完全本地** → auto/edge 会把文本发到微软在线；要说明隐私边界。

---

## 19. 一页时序图（可直接贴给开发）

```
锁屏会议录音
────────────
UI 点击开始
  → startForegroundService(ACTION_START)
  → Service: 权限检查 → startForeground(MICROPHONE) → 通知
  → AudioRecord 循环 → 每 N 秒/每段落盘
  → 按 Home/锁屏（Activity onStop）
  → Service 继续（前台 + 音频链路）
  → 用户停止 → 封尾段 → 停止前台 → 释放资源
  → 有网时补传（dataSync 前台 + WakeLock/WifiLock，finally 释放）
```

```
锁屏语音 + PC TTS
─────────────────
Service 听到一句（或按键）
  → 结束分段 → WAV
  → POST /api/assistant/voice-command
  → 轮询 /api/commands/{id}
  → 选 brief/speakText → SpeechText.normalize()
  → POST /api/tts（Bearer，{text, engine:""}）
  → 200 audio/mpeg 或 audio/wav
  → cacheDir 临时文件 → MediaPlayer.prepareAsync()
  → 播放（半双工 paused=true；全双工需 AEC）
  → 播完 → 恢复听写
  → 失败：提示原因 + 回退系统 TTS
```

---

## 20. 相关文档

- `android/README.md`：现有 App 的阶段/测试/真机记录。
- `docs/手机触点-App设计.md`：`/api/tts` 的设计与状态码来源。
- `docs/AI手机-ECHO-App-设计.md`：手机端整体交互与接口表。
- `app/api.py::post_tts`、`app/audio/tts.py::synthesize`：PC 端实现。
- `android/.../ListenService.kt`、`MeetingMicService.kt`、`SyncForeground.kt`、`EchoTtsPlayer.kt`：本文所有结论的代码依据。
