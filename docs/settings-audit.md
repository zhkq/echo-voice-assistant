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
| `dashboardShowRouter` | panel | - | - | **PANEL-READ** | - | web/app.js:5582 | - |
| `panelAutoRefresh` | panel | - | - | **PANEL-READ** | - | web/app.js:8001 | - |
| `agentBackend` | agent | - | hidden | **OK** | app/components.py:326<br>app/harness_proc.py:276<br>app/install_state.py:351<br>app/install_state.py:421 | - | - |
| `agentCustomPath` | agent | - | hidden | **OK** | app/agents/codebuddy.py:50 | - | - |
| `dshAutoStart` | agent | - | hidden | **OK** | app/boot.py:332<br>app/settings_effects.py:195<br>app/agents/harness_agent.py:224 | - | - |
| `dshBaseUrl` | agent | - | hidden | **OK** | app/harness_proc.py:82<br>app/agents/dsh_agent.py:138 | - | - |
| `dshHome` | agent | - | hidden | **OK** | app/paths.py:181 | - | - |
| `harnessCommand` | agent | - | hidden | **OK** | app/harness_proc.py:252 | web/app.js:2200 | - |
| `harnessPort` | agent | - | hidden | **OK** | app/harness_proc.py:64 | - | - |
| `accountServerUrl` | capability | - | hidden | **OK** | app/account_admin.py:110 | - | - |
| `capabilityAsrProviderApiKey` | capability | - | hidden,secret | **OK** | app/capabilities/asr_provider.py:110 | - | - |
| `capabilityAsrProviderBaseUrl` | capability | - | hidden | **OK** | app/capabilities/asr_provider.py:109 | - | - |
| `capabilityAsrProviderModel` | capability | - | hidden | **OK** | app/capabilities/asr_provider.py:111 | - | - |
| `capabilityBackendDir` | capability | - | hidden | **OK** | app/paths.py:305 | - | - |
| `capabilityBackendListen` | capability | - | hidden | **OK** | app/backend_setup.py:304 | - | - |
| `capabilityBackendStopWithClient` | capability | - | hidden | **OK** | app/backend_admin.py:79 | - | - |
| `capabilityDiarizeBackend` | capability | - | hidden | **OK** | app/backend_watch.py:87 | web/app.js:5640<br>web/app.js:5715 | - |
| `capabilityEchoServerStaticToken` | capability | - | hidden,secret | **OK** | app/capabilities/echo_server.py:226 | - | - |
| `capabilityEchoServerToken` | capability | - | hidden,secret | **OK** | app/capabilities/echo_server.py:223 | - | - |
| `capabilityEchoServerUrl` | capability | - | hidden | **OK** | app/backend_watch.py:140<br>app/capabilities/echo_server.py:207 | - | - |
| `capabilityEmbedBackend` | capability | - | hidden | **OK** | app/backend_watch.py:88 | web/app.js:5640 | - |
| `capabilityLocalPairPath` | capability | - | hidden | **OK** | app/backend_setup.py:468<br>app/capabilities/pairing.py:747 | - | app/backend_setup.py:480 |
| `capabilityMeetingAsrBackend` | capability | - | hidden | **OK** | app/backend_watch.py:86 | web/app.js:3772<br>web/app.js:3913<br>web/app.js:5646 | - |
| `capabilityPrivacy` | capability | - | hidden | **OK** | app/backend_admin.py:195<br>app/capability_admin.py:497<br>app/capabilities/echo_server.py:859 | web/app.js:4057 | - |
| `meetingAutoCompressAudio` | meeting | - | - | **OK** | app/meeting.py:2399 | - | - |
| `meetingAutoSummarize` | meeting | - | - | **OK** | app/meeting.py:986<br>app/meeting.py:1877<br>app/meeting.py:3256<br>app/meeting.py:5573 | - | - |
| `meetingKeepRawAudio` | meeting | - | - | **OK** | app/meeting.py:395<br>app/meeting.py:460<br>app/meeting.py:2408<br>app/meeting.py:5903 | - | - |
| `meetingSegmentMinutes` | meeting | - | - | **OK** | app/meeting.py:985<br>app/meeting.py:1876<br>app/meeting.py:1916<br>app/meeting.py:3254 | - | - |
| `meetingWorkspace` | meeting | - | - | **OK** | app/paths.py:330 | - | - |
| `device` | model | - | - | **OK** | app/api.py:3957<br>app/api.py:4032<br>app/e2e.py:399<br>app/meeting.py:984 | - | - |
| `modelCleanupDays` | model | - | - | **OK** | app/model_cleanup.py:29 | - | - |
| `sttModel` | model | - | - | **OK** | app/meeting.py:1031<br>app/meeting.py:1319<br>app/meeting.py:1895<br>app/meeting.py:5736 | - | - |
| `voiceprintAutoEnroll` | model | - | - | **OK** | app/voiceprint.py:70 | web/app.js:5754 | - |
| `voiceprintMargin` | model | - | - | **OK** | app/voiceprint.py:80 | web/app.js:5756 | - |
| `voiceprintThreshold` | model | - | - | **OK** | app/voiceprint.py:76 | web/app.js:5755 | - |
| `apiAuthEnabled` | panel | - | - | **OK** | app/api.py:127<br>app/api.py:3806<br>app/config.py:1483<br>app/config.py:1485 | web/app.js:5945 | - |
| `panelAutoStart` | panel | - | - | **OK** | app/runtime.py:287<br>mac/mac_runtime.py:118 | - | - |
| `panelOpenMode` | panel | - | - | **OK** | app/runtime.py:285<br>app/runtime.py:366<br>app/runtime.py:420<br>mac/mac_runtime.py:110 | - | mac/run_mac.py:69 |
| `panelStartCollapsed` | panel | - | - | **OK** | app/runtime.py:303<br>mac/mac_runtime.py:121 | - | - |
| `relayEnabled` | panel | - | - | **OK** | app/api.py:3807<br>app/relay.py:113 | - | - |
| `serverBindMode` | panel | - | - | **OK** | app/api.py:3799<br>app/config.py:1471<br>app/config.py:1475<br>app/config.py:1480 | - | - |
| `serverLanHost` | panel | - | - | **OK** | app/api.py:3800<br>app/netguard.py:190<br>app/netguard.py:273 | - | - |
| `serverPort` | panel | - | - | **OK** | app/api.py:3781<br>app/harness_proc.py:676<br>app/main.py:232<br>app/relay.py:325 | web/app.js:3811 | - |
| `meetingsDir` | paths | - | - | **OK** | app/paths.py:233<br>app/paths.py:329<br>app/paths.py:427 | - | - |
| `modelsDir` | paths | - | - | **OK** | app/paths.py:288<br>app/paths.py:404<br>app/paths.py:428 | - | - |
| `providerAsr` | provider | - | - | **OK** | app/meeting.py:3227<br>app/providers/__init__.py:204 | - | - |
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
| `dailyReviewWorkspace` | review | - | - | **OK** | app/paths.py:277 | - | - |
| `dailyReviewWorkspaceTitle` | review | - | - | **OK** | app/daily_review.py:372 | - | - |
| `routerAutoRegister` | router | - | - | **OK** | app/boot.py:563<br>app/settings_effects.py:226 | - | - |
| `routerDisplayName` | router | - | - | **OK** | app/router_admin.py:508 | - | - |
| `allowVirtualInputDevice` | voice | record | hidden | **OK** | app/audio/recorder.py:273 | - | - |
| `beepOnDone` | voice | beep | - | **OK** | app/assistant.py:602<br>app/assistant.py:864 | - | - |
| `beepOnSend` | voice | beep | - | **OK** | app/assistant.py:1141 | - | - |
| `beepOnStart` | voice | beep | - | **OK** | app/assistant.py:579<br>app/assistant.py:832 | - | - |
| `commandIdleRotateHours` | voice | command | - | **OK** | app/agents/dsh_agent.py:607 | - | - |
| `commandTargetSession` | voice | command | - | **OK** | app/assistant.py:983 | web/app.js:1660 | - |
| `commandTargetWorkspace` | voice | command | - | **OK** | app/assistant.py:982 | web/app.js:1659 | - |
| `commandWorkspace` | voice | command | - | **OK** | app/paths.py:254 | - | - |
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
| `sttLanguage` | voice | record | - | **OK** | app/assistant.py:540<br>app/meeting.py:2963<br>app/meeting.py:2984<br>app/meeting.py:3253 | - | - |
| `triggerKeys` | voice | record | - | **OK** | app/runtime.py:430<br>mac/mac_runtime.py:185 | - | - |
| `ttsEngine` | voice | speech | - | **OK** | app/api.py:3682<br>app/boot.py:608<br>app/config.py:1272<br>app/providers/__init__.py:144 | web/app.js:2323<br>web/app.js:2468<br>web/app.js:2571 | - |
| `userLocation` | voice | command | - | **OK** | app/assistant.py:147 | - | - |
| `voiceBrief` | voice | speech | - | **OK** | app/assistant.py:1169 | - | - |
| `voiceConfirm` | voice | speech | - | **OK** | app/assistant.py:668 | - | - |
| `worklogEnabled` | worklog | - | - | **OK** | app/worklog.py:59 | - | - |
| `worklogEnsureSessionAccess` | worklog | - | - | **OK** | app/daily_review.py:436<br>app/worklog.py:103 | - | - |
| `worklogPrompt` | worklog | - | - | **OK** | app/worklog.py:227 | - | - |
| `worklogVaultRoot` | worklog | - | - | **OK** | app/daily_review.py:77<br>app/install_state.py:210<br>app/worklog.py:64 | - | - |
| `agentHarnessEnabled` | agent | - | deprecated,hidden | **DEPRECATED** | - | - | - |
| `harnessHome` | agent | - | deprecated,hidden | **DEPRECATED** | - | web/app.js:3968 | - |
| `harnessToken` | agent | - | deprecated,hidden | **DEPRECATED** | app/harness_proc.py:346 | - | - |
| `dshNodePath` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `dshPackageDir` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `dshStartCommand` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `meetingDiarize` | model | - | deprecated | **DEPRECATED** | - | - | - |
| `meetingSttModel` | model | - | deprecated | **DEPRECATED** | mac/run_mac.py:40 | - | - |
| `voiceprintEnabled` | model | - | deprecated | **DEPRECATED** | - | - | - |
| `providerTts` | provider | - | deprecated,hidden | **DEPRECATED** | - | - | - |
| `dailyReviewSttBackend` | review | - | deprecated | **DEPRECATED** | - | - | - |
| `worklogMode` | worklog | - | deprecated | **DEPRECATED** | - | - | - |
