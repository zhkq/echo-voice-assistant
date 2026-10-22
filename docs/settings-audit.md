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
| `dashboardShowRouter` | panel | - | - | **PANEL-READ** | - | web/app.js:5466 | - |
| `panelAutoRefresh` | panel | - | - | **PANEL-READ** | - | web/app.js:7982 | - |
| `agentBackend` | agent | - | hidden | **OK** | app/components.py:403<br>app/harness_proc.py:165<br>app/install_state.py:390<br>app/install_state.py:502 | - | - |
| `agentCustomPath` | agent | - | hidden | **OK** | app/agents/codebuddy.py:50 | - | - |
| `agentHarnessEnabled` | agent | - | hidden | **OK** | app/harness_proc.py:167 | - | - |
| `dshBaseUrl` | agent | - | hidden | **OK** | app/agents/dsh_agent.py:138 | - | - |
| `harnessCommand` | agent | - | hidden | **OK** | app/harness_proc.py:144 | web/app.js:2150 | - |
| `harnessHome` | agent | - | hidden | **OK** | app/harness_proc.py:80 | web/app.js:3970 | - |
| `harnessPort` | agent | - | hidden | **OK** | app/harness_proc.py:64 | - | - |
| `harnessToken` | agent | - | hidden,secret | **OK** | app/harness_proc.py:235 | - | - |
| `capabilityAsrProviderApiKey` | capability | - | hidden,secret | **OK** | app/capabilities/asr_provider.py:110 | - | - |
| `capabilityAsrProviderBaseUrl` | capability | - | hidden | **OK** | app/capabilities/asr_provider.py:109 | - | - |
| `capabilityAsrProviderModel` | capability | - | hidden | **OK** | app/capabilities/asr_provider.py:111 | - | - |
| `capabilityBackendDir` | capability | - | hidden | **OK** | app/paths.py:254 | - | - |
| `capabilityBackendListen` | capability | - | hidden | **OK** | app/backend_setup.py:304 | - | - |
| `capabilityBackendStopWithClient` | capability | - | hidden | **OK** | app/backend_admin.py:79 | - | - |
| `capabilityDiarizeBackend` | capability | - | hidden | **OK** | app/backend_watch.py:87 | web/app.js:5556<br>web/app.js:5636 | - |
| `capabilityEchoServerStaticToken` | capability | - | hidden,secret | **OK** | app/capabilities/echo_server.py:225 | - | - |
| `capabilityEchoServerToken` | capability | - | hidden,secret | **OK** | app/capabilities/echo_server.py:222 | - | - |
| `capabilityEchoServerUrl` | capability | - | hidden | **OK** | app/backend_watch.py:140<br>app/capabilities/echo_server.py:206 | - | - |
| `capabilityEmbedBackend` | capability | - | hidden | **OK** | app/backend_watch.py:88 | web/app.js:5556 | - |
| `capabilityLocalPairPath` | capability | - | hidden | **OK** | app/backend_setup.py:468<br>app/capabilities/pairing.py:654 | - | app/backend_setup.py:480 |
| `capabilityMeetingAsrBackend` | capability | - | hidden | **OK** | app/backend_watch.py:86 | web/app.js:3758<br>web/app.js:3915<br>web/app.js:5562 | - |
| `capabilityPrivacy` | capability | - | hidden | **OK** | app/backend_admin.py:195<br>app/capability_admin.py:498 | web/app.js:4059 | - |
| `meetingAutoCompressAudio` | meeting | - | - | **OK** | app/meeting.py:2341 | - | - |
| `meetingAutoSummarize` | meeting | - | - | **OK** | app/meeting.py:943<br>app/meeting.py:1819<br>app/meeting.py:3159<br>app/meeting.py:5453 | - | - |
| `meetingKeepRawAudio` | meeting | - | - | **OK** | app/meeting.py:352<br>app/meeting.py:417<br>app/meeting.py:2350<br>app/meeting.py:5783 | - | - |
| `meetingSegmentMinutes` | meeting | - | - | **OK** | app/meeting.py:942<br>app/meeting.py:1818<br>app/meeting.py:1858<br>app/meeting.py:3157 | - | - |
| `meetingWorkspace` | meeting | - | - | **OK** | app/paths.py:279 | - | - |
| `device` | model | - | - | **OK** | app/api.py:675<br>app/api.py:1695<br>app/api.py:3573<br>app/assistant.py:529 | web/app.js:5554<br>web/app.js:5623 | - |
| `modelCleanupDays` | model | - | - | **OK** | app/model_cleanup.py:73 | - | - |
| `sttModel` | model | - | - | **OK** | app/api.py:996<br>app/assistant.py:519<br>app/meeting.py:981<br>app/meeting.py:1269 | web/app.js:2495<br>web/app.js:2509<br>web/app.js:5547 | - |
| `voiceprintAutoEnroll` | model | - | - | **OK** | app/voiceprint.py:70 | web/app.js:5679 | - |
| `voiceprintMargin` | model | - | - | **OK** | app/voiceprint.py:80 | web/app.js:5681 | - |
| `voiceprintThreshold` | model | - | - | **OK** | app/voiceprint.py:76 | web/app.js:5680 | - |
| `wakeEngine` | model | - | - | **OK** | app/model_usage.py:183<br>app/audio/wake.py:43<br>app/audio/wake.py:287 | web/app.js:5555<br>web/app.js:5622 | - |
| `apiAuthEnabled` | panel | - | - | **OK** | app/api.py:126<br>app/api.py:3511<br>app/config.py:1451<br>app/config.py:1453 | web/app.js:5877 | - |
| `panelAutoStart` | panel | - | - | **OK** | app/runtime.py:183<br>mac/mac_runtime.py:118 | - | - |
| `panelOpenMode` | panel | - | - | **OK** | app/runtime.py:181<br>app/runtime.py:262<br>app/runtime.py:316<br>mac/mac_runtime.py:110 | - | mac/run_mac.py:69 |
| `panelStartCollapsed` | panel | - | - | **OK** | app/runtime.py:199<br>mac/mac_runtime.py:121 | - | - |
| `relayEnabled` | panel | - | - | **OK** | app/api.py:3512<br>app/relay.py:112 | - | - |
| `serverBindMode` | panel | - | - | **OK** | app/api.py:3505<br>app/config.py:1439<br>app/config.py:1443<br>app/config.py:1448 | - | - |
| `serverLanHost` | panel | - | - | **OK** | app/api.py:3506<br>app/netguard.py:190<br>app/netguard.py:273 | - | - |
| `serverPort` | panel | - | - | **OK** | app/api.py:3487<br>app/harness_proc.py:565<br>app/main.py:232<br>app/relay.py:324 | web/app.js:3797 | - |
| `meetingsDir` | paths | - | - | **OK** | app/paths.py:182<br>app/paths.py:278<br>app/paths.py:376 | - | - |
| `modelsDir` | paths | - | - | **OK** | app/paths.py:237<br>app/paths.py:353<br>app/paths.py:377 | - | - |
| `providerAsr` | provider | - | - | **OK** | app/meeting.py:3130<br>app/providers/__init__.py:204 | - | - |
| `providerAsrApiKey` | provider | - | secret | **OK** | app/providers/openai.py:49 | - | - |
| `providerAsrBaseUrl` | provider | - | - | **OK** | app/providers/openai.py:46 | - | - |
| `providerAsrModel` | provider | - | - | **OK** | app/providers/openai.py:52 | - | - |
| `dailyReviewBroadcastChars` | review | - | - | **OK** | app/daily_review.py:167 | - | - |
| `dailyReviewEnabled` | review | - | - | **OK** | app/daily_review.py:69 | - | - |
| `dailyReviewEnsureSessionAccess` | review | - | - | **OK** | app/daily_review.py:427 | - | - |
| `dailyReviewMaxRecordSec` | review | - | - | **OK** | app/daily_review.py:143 | - | - |
| `dailyReviewPrePrompt` | review | - | hidden | **OK** | app/daily_review.py:1066 | - | - |
| `dailyReviewPrompt` | review | - | - | **OK** | app/daily_review.py:324 | - | - |
| `dailyReviewPromptNext` | review | - | hidden | **OK** | app/daily_review.py:328 | - | - |
| `dailyReviewReplyTimeoutSec` | review | - | - | **OK** | app/daily_review.py:159 | - | - |
| `dailyReviewSilenceMs` | review | - | - | **OK** | app/daily_review.py:151 | - | - |
| `dailyReviewSttBackend` | review | - | - | **OK** | app/assistant.py:470 | - | - |
| `dailyReviewVaultRoot` | review | - | - | **OK** | app/daily_review.py:74 | - | - |
| `dailyReviewWorkspace` | review | - | - | **OK** | app/paths.py:226 | - | - |
| `dailyReviewWorkspaceTitle` | review | - | - | **OK** | app/daily_review.py:372 | - | - |
| `routerAutoRegister` | router | - | - | **OK** | app/boot.py:525<br>app/settings_effects.py:249 | - | - |
| `routerDisplayName` | router | - | - | **OK** | app/router_admin.py:508 | - | - |
| `allowVirtualInputDevice` | voice | record | hidden | **OK** | app/audio/recorder.py:272 | - | - |
| `beepOnDone` | voice | beep | - | **OK** | app/assistant.py:591<br>app/assistant.py:834 | - | - |
| `beepOnSend` | voice | beep | - | **OK** | app/assistant.py:1107 | - | - |
| `beepOnStart` | voice | beep | - | **OK** | app/assistant.py:568<br>app/assistant.py:808 | - | - |
| `commandIdleRotateHours` | voice | command | - | **OK** | app/agents/dsh_agent.py:607 | - | - |
| `commandTargetSession` | voice | command | - | **OK** | app/assistant.py:949 | web/app.js:1587 | - |
| `commandTargetWorkspace` | voice | command | - | **OK** | app/assistant.py:948 | web/app.js:1586 | - |
| `commandWorkspace` | voice | command | - | **OK** | app/paths.py:203 | - | - |
| `consumeMediaKey` | voice | record | - | **OK** | app/platform/win32/hotkey.py:148 | - | - |
| `maxBriefChars` | voice | speech | - | **OK** | app/assistant.py:361<br>app/assistant.py:754 | - | - |
| `maxRecordMs` | voice | record | - | **OK** | app/assistant.py:578 | - | - |
| `minimalReply` | voice | speech | - | **OK** | app/assistant.py:169 | - | - |
| `minimalReplyChars` | voice | speech | - | **OK** | app/assistant.py:175<br>app/assistant.py:1099 | - | - |
| `minimalReplyHint` | voice | speech | - | **OK** | app/assistant.py:171 | - | - |
| `noSpeechAbortMs` | voice | record | - | **OK** | app/assistant.py:581<br>app/assistant.py:826 | - | - |
| `notifyOnSend` | voice | beep | - | **OK** | app/assistant.py:603<br>app/assistant.py:615<br>app/assistant.py:619<br>app/assistant.py:1065 | - | - |
| `outputDeviceIds` | voice | speech | - | **OK** | app/audio/output.py:195<br>app/audio/output.py:225 | - | - |
| `sendEnvContext` | voice | command | - | **OK** | app/assistant.py:150 | - | - |
| `silenceHangoverMs` | voice | record | - | **OK** | app/assistant.py:580 | - | - |
| `silenceThreshold` | voice | record | - | **OK** | app/assistant.py:579<br>app/assistant.py:601<br>app/assistant.py:824 | - | - |
| `sttLanguage` | voice | record | - | **OK** | app/api.py:997<br>app/assistant.py:499<br>app/assistant.py:512<br>app/assistant.py:528 | - | - |
| `triggerKeys` | voice | record | - | **OK** | app/runtime.py:326<br>mac/mac_runtime.py:185 | - | - |
| `ttsEngine` | voice | speech | - | **OK** | app/api.py:3388<br>app/boot.py:602<br>app/config.py:1255<br>app/audio/tts.py:477 | web/app.js:2273<br>web/app.js:2417<br>web/app.js:2552 | - |
| `userLocation` | voice | command | - | **OK** | app/assistant.py:155 | - | - |
| `voiceBrief` | voice | speech | - | **OK** | app/assistant.py:1135 | - | - |
| `voiceConfirm` | voice | speech | - | **OK** | app/assistant.py:653 | - | - |
| `wakeAliases` | wake | - | - | **OK** | app/audio/wake.py:313 | - | - |
| `wakeConfirmN` | wake | - | - | **OK** | app/audio/wake.py:374<br>app/audio/wake.py:386 | - | - |
| `wakeConfirmX` | wake | - | - | **OK** | app/audio/wake.py:373<br>app/audio/wake.py:385 | - | - |
| `wakeCooldownSec` | wake | - | - | **OK** | app/audio/wake.py:372 | - | - |
| `wakeEnabled` | wake | - | - | **OK** | app/boot.py:616<br>app/install_state.py:389<br>app/model_usage.py:185<br>app/runtime.py:380 | - | - |
| `wakeKeywords` | wake | - | - | **OK** | app/assistant.py:113<br>app/audio/wake.py:270<br>app/audio/wake.py:311 | - | - |
| `wakePaused` | wake | - | - | **OK** | app/audio/wake.py:378<br>app/audio/wake.py:384<br>app/audio/wake.py:409 | - | - |
| `wakeSilenceFloor` | wake | - | - | **OK** | app/audio/wake.py:375 | - | - |
| `wakeThreshold` | wake | - | - | **OK** | app/audio/wake.py:340 | - | - |
| `worklogEnabled` | worklog | - | - | **OK** | app/worklog.py:59 | - | - |
| `worklogEnsureSessionAccess` | worklog | - | - | **OK** | app/daily_review.py:436<br>app/worklog.py:103 | - | - |
| `worklogPrompt` | worklog | - | - | **OK** | app/worklog.py:227 | - | - |
| `worklogVaultRoot` | worklog | - | - | **OK** | app/daily_review.py:77<br>app/install_state.py:226<br>app/worklog.py:64 | - | - |
| `dshNodePath` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `dshPackageDir` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `dshStartCommand` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `meetingDiarize` | model | - | deprecated | **DEPRECATED** | - | - | - |
| `meetingSttModel` | model | - | deprecated | **DEPRECATED** | mac/run_mac.py:40 | - | - |
| `voiceprintEnabled` | model | - | deprecated | **DEPRECATED** | - | - | - |
| `providerTts` | provider | - | deprecated,hidden | **DEPRECATED** | - | - | - |
| `worklogMode` | worklog | - | deprecated | **DEPRECATED** | - | - | - |
