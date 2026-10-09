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
| `providerLlm` | provider | - | - | **OK-INDIRECT** | - | - | - |
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
| `capabilityDiarizeBackend` | capability | - | hidden | **PANEL-READ** | - | web/app.js:3760<br>web/app.js:4990<br>web/app.js:5070 | - |
| `capabilityEmbedBackend` | capability | - | hidden | **PANEL-READ** | - | web/app.js:4990 | - |
| `capabilityMeetingAsrBackend` | capability | - | hidden | **PANEL-READ** | - | web/app.js:3474<br>web/app.js:3628<br>web/app.js:4996 | - |
| `dashboardShowRouter` | panel | - | - | **PANEL-READ** | - | web/app.js:4900 | - |
| `panelAutoRefresh` | panel | - | - | **PANEL-READ** | - | web/app.js:7037 | - |
| `agentBackend` | agent | - | hidden | **OK** | app/components.py:403<br>app/harness_proc.py:165<br>app/install_state.py:212<br>app/install_state.py:324 | - | - |
| `agentCustomPath` | agent | - | hidden | **OK** | app/agents/codebuddy.py:50 | - | - |
| `agentHarnessEnabled` | agent | - | hidden | **OK** | app/harness_proc.py:167 | - | - |
| `dshBaseUrl` | agent | - | hidden | **OK** | app/agents/dsh_agent.py:138 | - | - |
| `harnessCommand` | agent | - | hidden | **OK** | app/harness_proc.py:144 | web/app.js:1821 | - |
| `harnessHome` | agent | - | hidden | **OK** | app/harness_proc.py:80 | web/app.js:3670 | - |
| `harnessPort` | agent | - | hidden | **OK** | app/harness_proc.py:64 | - | - |
| `harnessToken` | agent | - | hidden,secret | **OK** | app/harness_proc.py:235 | - | - |
| `capabilityAsrProviderApiKey` | capability | - | hidden,secret | **OK** | app/capabilities/asr_provider.py:110 | - | - |
| `capabilityAsrProviderBaseUrl` | capability | - | hidden | **OK** | app/capabilities/asr_provider.py:109 | - | - |
| `capabilityAsrProviderModel` | capability | - | hidden | **OK** | app/capabilities/asr_provider.py:111 | - | - |
| `capabilityBackendDir` | capability | - | hidden | **OK** | app/paths.py:254 | - | - |
| `capabilityBackendListen` | capability | - | hidden | **OK** | app/backend_setup.py:304 | - | - |
| `capabilityBackendStopWithClient` | capability | - | hidden | **OK** | app/backend_admin.py:79 | - | - |
| `capabilityEchoServerStaticToken` | capability | - | hidden,secret | **OK** | app/capabilities/echo_server.py:117 | - | - |
| `capabilityEchoServerToken` | capability | - | hidden,secret | **OK** | app/capabilities/echo_server.py:114 | - | - |
| `capabilityEchoServerUrl` | capability | - | hidden | **OK** | app/capabilities/echo_server.py:98 | - | - |
| `capabilityLocalPairPath` | capability | - | hidden | **OK** | app/backend_setup.py:468<br>app/capabilities/pairing.py:427 | - | app/backend_setup.py:480 |
| `capabilityPrivacy` | capability | - | hidden | **OK** | app/backend_admin.py:195<br>app/capability_admin.py:482 | web/app.js:3745 | - |
| `meetingAutoCompressAudio` | meeting | - | - | **OK** | app/meeting.py:910 | - | - |
| `meetingAutoSummarize` | meeting | - | - | **OK** | app/meeting.py:651<br>app/meeting.py:1682<br>app/meeting.py:3481 | - | - |
| `meetingKeepRawAudio` | meeting | - | - | **OK** | app/meeting.py:269<br>app/meeting.py:334<br>app/meeting.py:919<br>app/meeting.py:3799 | - | - |
| `meetingSegmentMinutes` | meeting | - | - | **OK** | app/meeting.py:650<br>app/meeting.py:686<br>app/meeting.py:1680<br>app/meeting.py:3480 | - | - |
| `meetingWorkspace` | meeting | - | - | **OK** | app/paths.py:279 | - | - |
| `device` | model | - | - | **OK** | app/api.py:483<br>app/api.py:1174<br>app/api.py:2419<br>app/assistant.py:436 | web/app.js:4988<br>web/app.js:5057 | - |
| `modelCleanupDays` | model | - | - | **OK** | app/model_cleanup.py:73 | - | - |
| `sttModel` | model | - | - | **OK** | app/assistant.py:426<br>app/meeting.py:665<br>app/meeting.py:3635<br>app/capabilities/local.py:170 | web/app.js:2166<br>web/app.js:2180<br>web/app.js:4981 | - |
| `voiceprintAutoEnroll` | model | - | - | **OK** | app/voiceprint.py:70 | web/app.js:5113 | - |
| `voiceprintMargin` | model | - | - | **OK** | app/voiceprint.py:80 | web/app.js:5115 | - |
| `voiceprintThreshold` | model | - | - | **OK** | app/voiceprint.py:76 | web/app.js:5114 | - |
| `wakeEngine` | model | - | - | **OK** | app/model_usage.py:183<br>app/audio/wake.py:43<br>app/audio/wake.py:287 | web/app.js:4989<br>web/app.js:5056 | - |
| `apiAuthEnabled` | panel | - | - | **OK** | app/api.py:103<br>app/api.py:2364<br>app/config.py:1433<br>app/config.py:1435 | web/app.js:5311 | - |
| `panelAutoStart` | panel | - | - | **OK** | app/runtime.py:107<br>mac/mac_runtime.py:118 | - | - |
| `panelOpenMode` | panel | - | - | **OK** | app/runtime.py:105<br>app/runtime.py:176<br>app/runtime.py:230<br>mac/mac_runtime.py:110 | - | mac/run_mac.py:69 |
| `panelStartCollapsed` | panel | - | - | **OK** | app/runtime.py:113<br>mac/mac_runtime.py:121 | - | - |
| `serverBindMode` | panel | - | - | **OK** | app/api.py:2358<br>app/config.py:1421<br>app/config.py:1425<br>app/config.py:1430 | - | - |
| `serverLanHost` | panel | - | - | **OK** | app/api.py:2359<br>app/netguard.py:190<br>app/netguard.py:273 | - | - |
| `serverPort` | panel | - | - | **OK** | app/api.py:2351<br>app/harness_proc.py:486<br>app/main.py:232<br>app/runtime.py:66 | web/app.js:3513 | - |
| `meetingsDir` | paths | - | - | **OK** | app/paths.py:182<br>app/paths.py:278<br>app/paths.py:376 | - | - |
| `modelsDir` | paths | - | - | **OK** | app/paths.py:237<br>app/paths.py:353<br>app/paths.py:377 | - | - |
| `providerAsr` | provider | - | - | **OK** | app/meeting.py:1653<br>app/providers/__init__.py:203 | - | - |
| `providerAsrApiKey` | provider | - | secret | **OK** | app/providers/openai.py:121 | - | - |
| `providerAsrBaseUrl` | provider | - | - | **OK** | app/providers/openai.py:118 | - | - |
| `providerAsrModel` | provider | - | - | **OK** | app/providers/openai.py:124 | - | - |
| `providerLlmApiKey` | provider | - | secret | **OK** | app/providers/openai.py:77 | - | - |
| `providerLlmBaseUrl` | provider | - | - | **OK** | app/providers/openai.py:74 | - | - |
| `providerLlmModel` | provider | - | - | **OK** | app/providers/openai.py:80 | - | - |
| `dailyReviewBroadcastChars` | review | - | - | **OK** | app/daily_review.py:121 | - | - |
| `dailyReviewEnabled` | review | - | - | **OK** | app/daily_review.py:68 | - | - |
| `dailyReviewEnsureSessionAccess` | review | - | - | **OK** | app/daily_review.py:331 | - | - |
| `dailyReviewMaxRecordSec` | review | - | - | **OK** | app/daily_review.py:97 | - | - |
| `dailyReviewPrompt` | review | - | - | **OK** | app/daily_review.py:243 | - | - |
| `dailyReviewReplyTimeoutSec` | review | - | - | **OK** | app/daily_review.py:113 | - | - |
| `dailyReviewSilenceMs` | review | - | - | **OK** | app/daily_review.py:105 | - | - |
| `dailyReviewSttBackend` | review | - | - | **OK** | app/assistant.py:377 | - | - |
| `dailyReviewVaultRoot` | review | - | - | **OK** | app/daily_review.py:73 | - | - |
| `dailyReviewWorkspace` | review | - | - | **OK** | app/paths.py:226 | - | - |
| `dailyReviewWorkspaceTitle` | review | - | - | **OK** | app/daily_review.py:287 | - | - |
| `routerAutoRegister` | router | - | - | **OK** | app/boot.py:440<br>app/settings_effects.py:187 | - | - |
| `routerDisplayName` | router | - | - | **OK** | app/router_admin.py:508 | - | - |
| `allowVirtualInputDevice` | voice | record | hidden | **OK** | app/audio/recorder.py:272 | - | - |
| `beepOnDone` | voice | beep | - | **OK** | app/assistant.py:498<br>app/assistant.py:722 | - | - |
| `beepOnSend` | voice | beep | - | **OK** | app/assistant.py:928 | - | - |
| `beepOnStart` | voice | beep | - | **OK** | app/assistant.py:475<br>app/assistant.py:696 | - | - |
| `commandIdleRotateHours` | voice | command | - | **OK** | app/agents/dsh_agent.py:597 | - | - |
| `commandTargetSession` | voice | command | - | **OK** | app/assistant.py:808 | web/app.js:1282 | - |
| `commandTargetWorkspace` | voice | command | - | **OK** | app/assistant.py:807 | web/app.js:1281 | - |
| `commandWorkspace` | voice | command | - | **OK** | app/paths.py:203 | - | - |
| `consumeMediaKey` | voice | record | - | **OK** | app/platform/win32/hotkey.py:148 | - | - |
| `maxBriefChars` | voice | speech | - | **OK** | app/assistant.py:661<br>app/assistant.py:937 | - | - |
| `maxRecordMs` | voice | record | - | **OK** | app/assistant.py:485 | - | - |
| `minimalReply` | voice | speech | - | **OK** | app/assistant.py:169 | - | - |
| `minimalReplyChars` | voice | speech | - | **OK** | app/assistant.py:175<br>app/assistant.py:920 | - | - |
| `minimalReplyHint` | voice | speech | - | **OK** | app/assistant.py:171 | - | - |
| `noSpeechAbortMs` | voice | record | - | **OK** | app/assistant.py:488<br>app/assistant.py:714 | - | - |
| `notifyOnSend` | voice | beep | - | **OK** | app/assistant.py:510<br>app/assistant.py:522<br>app/assistant.py:526<br>app/assistant.py:886 | - | - |
| `outputDeviceIds` | voice | speech | - | **OK** | app/audio/output.py:195<br>app/audio/output.py:225 | - | - |
| `sendEnvContext` | voice | command | - | **OK** | app/assistant.py:150 | - | - |
| `silenceHangoverMs` | voice | record | - | **OK** | app/assistant.py:487 | - | - |
| `silenceThreshold` | voice | record | - | **OK** | app/assistant.py:486<br>app/assistant.py:508<br>app/assistant.py:712 | - | - |
| `sttLanguage` | voice | record | - | **OK** | app/assistant.py:406<br>app/assistant.py:419<br>app/assistant.py:435<br>app/meeting.py:1426 | - | - |
| `triggerKeys` | voice | record | - | **OK** | app/runtime.py:240<br>mac/mac_runtime.py:185 | - | - |
| `ttsEngine` | voice | speech | - | **OK** | app/boot.py:518<br>app/config.py:1237<br>app/providers/__init__.py:144 | web/app.js:1944<br>web/app.js:2088<br>web/app.js:2227 | - |
| `userLocation` | voice | command | - | **OK** | app/assistant.py:155 | - | - |
| `voiceBrief` | voice | speech | - | **OK** | app/assistant.py:942 | - | - |
| `voiceConfirm` | voice | speech | - | **OK** | app/assistant.py:560 | - | - |
| `wakeAliases` | wake | - | - | **OK** | app/audio/wake.py:313 | - | - |
| `wakeConfirmN` | wake | - | - | **OK** | app/audio/wake.py:374<br>app/audio/wake.py:386 | - | - |
| `wakeConfirmX` | wake | - | - | **OK** | app/audio/wake.py:373<br>app/audio/wake.py:385 | - | - |
| `wakeCooldownSec` | wake | - | - | **OK** | app/audio/wake.py:372 | - | - |
| `wakeEnabled` | wake | - | - | **OK** | app/boot.py:536<br>app/install_state.py:211<br>app/model_usage.py:185<br>app/runtime.py:294 | - | - |
| `wakeKeywords` | wake | - | - | **OK** | app/assistant.py:113<br>app/audio/wake.py:270<br>app/audio/wake.py:311 | - | - |
| `wakePaused` | wake | - | - | **OK** | app/audio/wake.py:378<br>app/audio/wake.py:384<br>app/audio/wake.py:409 | - | - |
| `wakeSilenceFloor` | wake | - | - | **OK** | app/audio/wake.py:375 | - | - |
| `wakeThreshold` | wake | - | - | **OK** | app/audio/wake.py:340 | - | - |
| `worklogEnabled` | worklog | - | - | **OK** | app/worklog.py:59 | - | - |
| `worklogEnsureSessionAccess` | worklog | - | - | **OK** | app/daily_review.py:340<br>app/worklog.py:103 | - | - |
| `worklogPrompt` | worklog | - | - | **OK** | app/worklog.py:227 | - | - |
| `worklogVaultRoot` | worklog | - | - | **OK** | app/daily_review.py:76<br>app/worklog.py:64 | - | - |
| `dshNodePath` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `dshPackageDir` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `dshStartCommand` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `meetingDiarize` | model | - | deprecated | **DEPRECATED** | - | - | - |
| `meetingSttModel` | model | - | deprecated | **DEPRECATED** | mac/run_mac.py:40 | - | - |
| `voiceprintEnabled` | model | - | deprecated | **DEPRECATED** | - | - | - |
| `providerTts` | provider | - | deprecated,hidden | **DEPRECATED** | - | - | - |
| `worklogMode` | worklog | - | deprecated | **DEPRECATED** | - | - | - |
