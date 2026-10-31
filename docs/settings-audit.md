# 设置清单审计（`scripts/audit-settings.py` 生成）

对 `app/config.py:DEFAULTS` 的每一键，扫 `app/ web/ scripts/ mac/ dsh-failover/`：
谁定义 / 谁写 / **谁读**。判定：

* `OK` = app/ 的产品代码里真的读了；
* `OK-INDIRECT` = app/ 里通过循环变量/键清单间接读（需在 `ACK_INDIRECT` 里写明谁读）；
* `PANEL-READ` = 唯一消费方是面板自己（需在 `ACK_PANEL` 里写明用途）；
* `PANEL-ONLY` = 只有面板把它渲染成表单/写进去，没人读（可疑）；
* `DEAD` = 谁都不碰；`DEPRECATED` = 已弃用（不再展示、写入被拒收）。

`--check` 在出现 DEAD / PANEL-ONLY / **未确认**的间接读与面板消费时返回 1；
`tests/test_settings_wiring.py` 会跑这个检查，并额外验「每项都能读回」。

| 键 | 分组 | 二级小节 | 标记 | 判定 | 读它的地方（app/ 内） | 面板读 | 写它的地方 |
|---|---|---|---|---|---|---|---|
| `agentCodebuddyEnabled` | agent | - | hidden | **OK-INDIRECT** | - | - | - |
| `capabilityBackendPackage` | capability | - | hidden | **OK-INDIRECT** | - | - | - |
| `meetingStartHotkey` | meeting | - | - | **OK-INDIRECT** | - | - | - |
| `meetingStopHotkey` | meeting | - | - | **OK-INDIRECT** | - | - | - |
| `meetingWorkspaceTitle` | meeting | - | - | **OK-INDIRECT** | - | - | - |
| `panelHotkey` | panel | - | - | **OK-INDIRECT** | - | - | - |
| `routerBreakerCooldown` | router | - | - | **OK-INDIRECT** | - | - | - |
| `routerBreakerThreshold` | router | - | - | **OK-INDIRECT** | - | - | - |
| `routerConnectTimeout` | router | - | - | **OK-INDIRECT** | - | - | - |
| `routerFirstByteTimeout` | router | - | - | **OK-INDIRECT** | - | - | - |
| `routerProbeInterval` | router | - | - | **OK-INDIRECT** | - | - | - |
| `commandInputDeviceId` | voice | record | - | **OK-INDIRECT** | - | - | - |
| `commandOutputDeviceId` | voice | speech | - | **OK-INDIRECT** | - | - | - |
| `commandWorkspaceTitle` | voice | command | - | **OK-INDIRECT** | - | - | - |
| `fallbackHotkey` | voice | record | - | **OK-INDIRECT** | - | - | - |
| `inputDeviceId` | voice | record | - | **OK-INDIRECT** | - | - | - |
| `meetingInputDeviceId` | voice | record | - | **OK-INDIRECT** | - | - | - |
| `meetingOutputDeviceId` | voice | speech | - | **OK-INDIRECT** | - | - | - |
| `wakeHotkey` | voice | record | - | **OK-INDIRECT** | - | - | - |
| `dashboardShowRouter` | panel | - | - | **PANEL-READ** | - | web/app.js:5504 | - |
| `panelAutoRefresh` | panel | - | - | **PANEL-READ** | - | web/app.js:7925 | - |
| `agentBackend` | agent | - | hidden | **OK** | app/components.py:326<br>app/harness_proc.py:165<br>app/install_state.py:343<br>app/install_state.py:413 | - | - |
| `agentCustomPath` | agent | - | hidden | **OK** | app/agents/codebuddy.py:50 | - | - |
| `agentHarnessEnabled` | agent | - | hidden | **OK** | app/harness_proc.py:167 | - | - |
| `dshBaseUrl` | agent | - | hidden | **OK** | app/agents/dsh_agent.py:138 | - | - |
| `harnessCommand` | agent | - | hidden | **OK** | app/harness_proc.py:144 | web/app.js:2176 | - |
| `harnessHome` | agent | - | hidden | **OK** | app/harness_proc.py:80 | web/app.js:3945 | - |
| `harnessPort` | agent | - | hidden | **OK** | app/harness_proc.py:64 | - | - |
| `harnessToken` | agent | - | hidden,secret | **OK** | app/harness_proc.py:235 | - | - |
| `capabilityAsrProviderApiKey` | capability | - | hidden,secret | **OK** | app/capabilities/asr_provider.py:110 | - | - |
| `capabilityAsrProviderBaseUrl` | capability | - | hidden | **OK** | app/capabilities/asr_provider.py:109 | - | - |
| `capabilityAsrProviderModel` | capability | - | hidden | **OK** | app/capabilities/asr_provider.py:111 | - | - |
| `capabilityBackendDir` | capability | - | hidden | **OK** | app/paths.py:254 | - | - |
| `capabilityBackendListen` | capability | - | hidden | **OK** | app/backend_setup.py:304 | - | - |
| `capabilityBackendStopWithClient` | capability | - | hidden | **OK** | app/backend_admin.py:79 | - | - |
| `capabilityDiarizeBackend` | capability | - | hidden | **OK** | app/backend_watch.py:87 | web/app.js:5562<br>web/app.js:5637 | - |
| `capabilityEchoServerStaticToken` | capability | - | hidden,secret | **OK** | app/capabilities/echo_server.py:226 | - | - |
| `capabilityEchoServerToken` | capability | - | hidden,secret | **OK** | app/capabilities/echo_server.py:223 | - | - |
| `capabilityEchoServerUrl` | capability | - | hidden | **OK** | app/backend_watch.py:140<br>app/capabilities/echo_server.py:207 | - | - |
| `capabilityEmbedBackend` | capability | - | hidden | **OK** | app/backend_watch.py:88 | web/app.js:5562 | - |
| `capabilityLocalPairPath` | capability | - | hidden | **OK** | app/backend_setup.py:468<br>app/capabilities/pairing.py:654 | - | app/backend_setup.py:480 |
| `capabilityMeetingAsrBackend` | capability | - | hidden | **OK** | app/backend_watch.py:86 | web/app.js:3749<br>web/app.js:3890<br>web/app.js:5568 | - |
| `capabilityPrivacy` | capability | - | hidden | **OK** | app/backend_admin.py:195<br>app/capability_admin.py:497<br>app/capabilities/echo_server.py:800 | web/app.js:4034 | - |
| `meetingAutoCompressAudio` | meeting | - | - | **OK** | app/meeting.py:2399 | - | - |
| `meetingAutoSummarize` | meeting | - | - | **OK** | app/meeting.py:986<br>app/meeting.py:1877<br>app/meeting.py:3219<br>app/meeting.py:5513 | - | - |
| `meetingKeepRawAudio` | meeting | - | - | **OK** | app/meeting.py:395<br>app/meeting.py:460<br>app/meeting.py:2408<br>app/meeting.py:5843 | - | - |
| `meetingSegmentMinutes` | meeting | - | - | **OK** | app/meeting.py:985<br>app/meeting.py:1876<br>app/meeting.py:1916<br>app/meeting.py:3217 | - | - |
| `meetingWorkspace` | meeting | - | - | **OK** | app/paths.py:279 | - | - |
| `device` | model | - | - | **OK** | app/api.py:3640<br>app/meeting.py:984<br>app/meeting.py:1875<br>app/meeting.py:3215 | - | - |
| `modelCleanupDays` | model | - | - | **OK** | app/model_cleanup.py:29 | - | - |
| `sttModel` | model | - | - | **OK** | app/meeting.py:1031<br>app/meeting.py:1319<br>app/meeting.py:1895<br>app/meeting.py:5676 | - | - |
| `voiceprintAutoEnroll` | model | - | - | **OK** | app/voiceprint.py:70 | web/app.js:5676 | - |
| `voiceprintMargin` | model | - | - | **OK** | app/voiceprint.py:80 | web/app.js:5678 | - |
| `voiceprintThreshold` | model | - | - | **OK** | app/voiceprint.py:76 | web/app.js:5677 | - |
| `apiAuthEnabled` | panel | - | - | **OK** | app/api.py:125<br>app/api.py:3578<br>app/config.py:1451<br>app/config.py:1453 | web/app.js:5867 | - |
| `panelAutoStart` | panel | - | - | **OK** | app/runtime.py:258<br>mac/mac_runtime.py:118 | - | - |
| `panelOpenMode` | panel | - | - | **OK** | app/runtime.py:256<br>app/runtime.py:337<br>app/runtime.py:391<br>mac/mac_runtime.py:110 | - | mac/run_mac.py:69 |
| `panelStartCollapsed` | panel | - | - | **OK** | app/runtime.py:274<br>mac/mac_runtime.py:121 | - | - |
| `relayEnabled` | panel | - | - | **OK** | app/api.py:3579<br>app/relay.py:112 | - | - |
| `serverBindMode` | panel | - | - | **OK** | app/api.py:3572<br>app/config.py:1439<br>app/config.py:1443<br>app/config.py:1448 | - | - |
| `serverLanHost` | panel | - | - | **OK** | app/api.py:3573<br>app/netguard.py:190<br>app/netguard.py:273 | - | - |
| `serverPort` | panel | - | - | **OK** | app/api.py:3554<br>app/harness_proc.py:565<br>app/main.py:232<br>app/relay.py:324 | web/app.js:3788 | - |
| `meetingsDir` | paths | - | - | **OK** | app/paths.py:182<br>app/paths.py:278<br>app/paths.py:376 | - | - |
| `modelsDir` | paths | - | - | **OK** | app/paths.py:237<br>app/paths.py:353<br>app/paths.py:377 | - | - |
| `providerAsr` | provider | - | - | **OK** | app/meeting.py:3190<br>app/providers/__init__.py:204 | - | - |
| `providerAsrApiKey` | provider | - | secret | **OK** | app/providers/openai.py:49 | - | - |
| `providerAsrBaseUrl` | provider | - | - | **OK** | app/providers/openai.py:46 | - | - |
| `providerAsrModel` | provider | - | - | **OK** | app/providers/openai.py:52 | - | - |
| `dailyReviewBroadcastChars` | review | - | - | **OK** | app/daily_review.py:167 | - | - |
| `dailyReviewEnabled` | review | - | - | **OK** | app/daily_review.py:69 | - | - |
| `dailyReviewEnsureSessionAccess` | review | - | - | **OK** | app/daily_review.py:427 | - | - |
| `dailyReviewMaxRecordSec` | review | - | - | **OK** | app/daily_review.py:143 | - | - |
| `dailyReviewPrePrompt` | review | - | hidden | **OK** | app/daily_review.py:1174 | - | - |
| `dailyReviewPrompt` | review | - | - | **OK** | app/daily_review.py:324 | - | - |
| `dailyReviewPromptNext` | review | - | hidden | **OK** | app/daily_review.py:328 | - | - |
| `dailyReviewReplyTimeoutSec` | review | - | - | **OK** | app/daily_review.py:159 | - | - |
| `dailyReviewSilenceMs` | review | - | - | **OK** | app/daily_review.py:151 | - | - |
| `dailyReviewVaultRoot` | review | - | - | **OK** | app/daily_review.py:74 | - | - |
| `dailyReviewWorkspace` | review | - | - | **OK** | app/paths.py:226 | - | - |
| `dailyReviewWorkspaceTitle` | review | - | - | **OK** | app/daily_review.py:372 | - | - |
| `routerAutoRegister` | router | - | - | **OK** | app/boot.py:523<br>app/settings_effects.py:210 | - | - |
| `routerDisplayName` | router | - | - | **OK** | app/router_admin.py:508 | - | - |
| `allowVirtualInputDevice` | voice | record | hidden | **OK** | app/audio/recorder.py:272 | - | - |
| `beepOnDone` | voice | beep | - | **OK** | app/assistant.py:602<br>app/assistant.py:864 | - | - |
| `beepOnSend` | voice | beep | - | **OK** | app/assistant.py:1141 | - | - |
| `beepOnStart` | voice | beep | - | **OK** | app/assistant.py:579<br>app/assistant.py:832 | - | - |
| `commandIdleRotateHours` | voice | command | - | **OK** | app/agents/dsh_agent.py:607 | - | - |
| `commandTargetSession` | voice | command | - | **OK** | app/assistant.py:983 | web/app.js:1636 | - |
| `commandTargetWorkspace` | voice | command | - | **OK** | app/assistant.py:982 | web/app.js:1635 | - |
| `commandWorkspace` | voice | command | - | **OK** | app/paths.py:203 | - | - |
| `consumeMediaKey` | voice | record | - | **OK** | app/platform/win32/hotkey.py:148 | - | - |
| `maxBriefChars` | voice | speech | - | **OK** | app/assistant.py:353<br>app/assistant.py:778 | - | - |
| `maxRecordMs` | voice | record | - | **OK** | app/assistant.py:589 | - | - |
| `minimalReply` | voice | speech | - | **OK** | app/assistant.py:161 | - | - |
| `minimalReplyChars` | voice | speech | - | **OK** | app/assistant.py:167<br>app/assistant.py:1133 | - | - |
| `minimalReplyHint` | voice | speech | - | **OK** | app/assistant.py:163 | - | - |
| `noSpeechAbortMs` | voice | record | - | **OK** | app/assistant.py:592<br>app/assistant.py:856 | - | - |
| `notifyOnSend` | voice | beep | - | **OK** | app/assistant.py:450<br>app/assistant.py:614<br>app/assistant.py:624<br>app/assistant.py:630 | - | - |
| `outputDeviceIds` | voice | speech | - | **OK** | app/audio/output.py:195<br>app/audio/output.py:225 | - | - |
| `sendEnvContext` | voice | command | - | **OK** | app/assistant.py:142 | - | - |
| `silenceHangoverMs` | voice | record | - | **OK** | app/assistant.py:591 | - | - |
| `silenceThreshold` | voice | record | - | **OK** | app/assistant.py:590<br>app/assistant.py:612<br>app/assistant.py:854 | - | - |
| `sttLanguage` | voice | record | - | **OK** | app/assistant.py:540<br>app/meeting.py:2963<br>app/meeting.py:2984<br>app/meeting.py:3216 | - | - |
| `triggerKeys` | voice | record | - | **OK** | app/runtime.py:401<br>mac/mac_runtime.py:185 | - | - |
| `ttsEngine` | voice | speech | - | **OK** | app/api.py:3455<br>app/boot.py:568<br>app/config.py:1246<br>app/audio/tts.py:477 | web/app.js:2299<br>web/app.js:2443<br>web/app.js:2546 | - |
| `userLocation` | voice | command | - | **OK** | app/assistant.py:147 | - | - |
| `voiceBrief` | voice | speech | - | **OK** | app/assistant.py:1169 | - | - |
| `voiceConfirm` | voice | speech | - | **OK** | app/assistant.py:668 | - | - |
| `worklogEnabled` | worklog | - | - | **OK** | app/worklog.py:59 | - | - |
| `worklogEnsureSessionAccess` | worklog | - | - | **OK** | app/daily_review.py:436<br>app/worklog.py:103 | - | - |
| `worklogPrompt` | worklog | - | - | **OK** | app/worklog.py:227 | - | - |
| `worklogVaultRoot` | worklog | - | - | **OK** | app/daily_review.py:77<br>app/install_state.py:210<br>app/worklog.py:64 | - | - |
| `dshNodePath` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `dshPackageDir` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `dshStartCommand` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `meetingDiarize` | model | - | deprecated | **DEPRECATED** | - | - | - |
| `meetingSttModel` | model | - | deprecated | **DEPRECATED** | mac/run_mac.py:40 | - | - |
| `voiceprintEnabled` | model | - | deprecated | **DEPRECATED** | - | - | - |
| `providerTts` | provider | - | deprecated,hidden | **DEPRECATED** | - | - | - |
| `dailyReviewSttBackend` | review | - | deprecated | **DEPRECATED** | - | - | - |
| `worklogMode` | worklog | - | deprecated | **DEPRECATED** | - | - | - |
