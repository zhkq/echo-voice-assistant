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
| `meetingWorkspaceTitle` | meeting | - | - | **OK-INDIRECT** | - | - | - |
| `panelHotkey` | panel | - | - | **OK-INDIRECT** | - | - | - |
| `providerLlm` | provider | - | hidden | **OK-INDIRECT** | - | - | - |
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
| `capabilityDiarizeBackend` | capability | - | hidden | **PANEL-READ** | - | web/app.js:4071 | - |
| `capabilityEmbedBackend` | capability | - | hidden | **PANEL-READ** | - | web/app.js:4071 | - |
| `capabilityMeetingAsrBackend` | capability | - | hidden | **PANEL-READ** | - | web/app.js:4077 | - |
| `panelAutoRefresh` | panel | - | - | **PANEL-READ** | - | web/app.js:6087 | - |
| `agentBackend` | agent | - | hidden | **OK** | app/components.py:387<br>app/harness_proc.py:163<br>app/install_state.py:208<br>app/agents/__init__.py:128 | - | - |
| `agentCustomPath` | agent | - | hidden | **OK** | app/agents/codebuddy.py:50 | - | - |
| `agentHarnessEnabled` | agent | - | hidden | **OK** | app/harness_proc.py:165 | - | - |
| `dshBaseUrl` | agent | - | hidden | **OK** | app/agents/dsh_agent.py:138 | - | - |
| `harnessCommand` | agent | - | hidden | **OK** | app/harness_proc.py:142 | web/app.js:1732 | - |
| `harnessHome` | agent | - | hidden | **OK** | app/harness_proc.py:78 | web/app.js:3515 | - |
| `harnessPort` | agent | - | hidden | **OK** | app/harness_proc.py:62 | - | - |
| `harnessToken` | agent | - | hidden,secret | **OK** | app/harness_proc.py:188 | - | - |
| `capabilityAsrProviderApiKey` | capability | - | hidden,secret | **OK** | app/capabilities/asr_provider.py:110 | - | - |
| `capabilityAsrProviderBaseUrl` | capability | - | hidden | **OK** | app/capabilities/asr_provider.py:109 | - | - |
| `capabilityAsrProviderModel` | capability | - | hidden | **OK** | app/capabilities/asr_provider.py:111 | - | - |
| `capabilityBackendDir` | capability | - | hidden | **OK** | app/paths.py:229 | - | - |
| `capabilityBackendStopWithClient` | capability | - | hidden | **OK** | app/backend_admin.py:79 | - | - |
| `capabilityEchoServerStaticToken` | capability | - | hidden,secret | **OK** | app/capabilities/echo_server.py:117 | - | - |
| `capabilityEchoServerToken` | capability | - | hidden,secret | **OK** | app/capabilities/echo_server.py:114 | - | - |
| `capabilityEchoServerUrl` | capability | - | hidden | **OK** | app/capabilities/echo_server.py:98 | - | - |
| `capabilityLocalPairPath` | capability | - | hidden | **OK** | app/backend_setup.py:447<br>app/capabilities/pairing.py:427 | - | app/backend_setup.py:459 |
| `capabilityPrivacy` | capability | - | hidden | **OK** | app/backend_admin.py:195<br>app/capability_admin.py:482 | - | - |
| `meetingAutoCompressAudio` | meeting | - | - | **OK** | app/meeting.py:910 | - | - |
| `meetingAutoSummarize` | meeting | - | - | **OK** | app/meeting.py:651<br>app/meeting.py:1682<br>app/meeting.py:3428 | - | - |
| `meetingKeepRawAudio` | meeting | - | - | **OK** | app/meeting.py:269<br>app/meeting.py:334<br>app/meeting.py:919<br>app/meeting.py:3746 | - | - |
| `meetingSegmentMinutes` | meeting | - | - | **OK** | app/meeting.py:650<br>app/meeting.py:686<br>app/meeting.py:1680<br>app/meeting.py:3427 | - | - |
| `meetingWorkspace` | meeting | - | - | **OK** | app/paths.py:254 | - | - |
| `device` | model | - | - | **OK** | app/api.py:387<br>app/api.py:929<br>app/assistant.py:362<br>app/boot.py:502 | web/app.js:4069<br>web/app.js:4138 | - |
| `modelCleanupDays` | model | - | - | **OK** | app/model_cleanup.py:73 | - | - |
| `sttModel` | model | - | - | **OK** | app/assistant.py:353<br>app/meeting.py:665<br>app/meeting.py:3582<br>app/capabilities/local.py:170 | web/app.js:2130<br>web/app.js:4065<br>web/app.js:4136 | - |
| `voiceprintAutoEnroll` | model | - | - | **OK** | app/voiceprint.py:70 | web/app.js:4188 | - |
| `voiceprintMargin` | model | - | - | **OK** | app/voiceprint.py:80 | web/app.js:4190 | - |
| `voiceprintThreshold` | model | - | - | **OK** | app/voiceprint.py:76 | web/app.js:4189 | - |
| `wakeEngine` | model | - | - | **OK** | app/model_usage.py:183<br>app/audio/wake.py:43<br>app/audio/wake.py:203 | web/app.js:4070<br>web/app.js:4137 | - |
| `apiAuthEnabled` | panel | - | - | **OK** | app/api.py:53 | web/app.js:4386 | - |
| `panelAutoStart` | panel | - | - | **OK** | app/runtime.py:107<br>mac/mac_runtime.py:118 | - | - |
| `panelOpenMode` | panel | - | - | **OK** | app/runtime.py:105<br>app/runtime.py:176<br>app/runtime.py:208<br>mac/mac_runtime.py:110 | - | mac/run_mac.py:69 |
| `panelStartCollapsed` | panel | - | - | **OK** | app/runtime.py:113<br>mac/mac_runtime.py:121 | - | - |
| `serverPort` | panel | - | - | **OK** | app/main.py:232<br>app/runtime.py:66<br>app/runtime.py:174<br>mac/mac_runtime.py:40 | - | - |
| `meetingsDir` | paths | - | - | **OK** | app/paths.py:180<br>app/paths.py:253<br>app/paths.py:351 | - | - |
| `modelsDir` | paths | - | - | **OK** | app/paths.py:212<br>app/paths.py:328<br>app/paths.py:352 | - | - |
| `providerAsr` | provider | - | hidden | **OK** | app/meeting.py:1653<br>app/providers/__init__.py:203 | - | - |
| `providerAsrApiKey` | provider | - | hidden,secret | **OK** | app/providers/openai.py:121 | - | - |
| `providerAsrBaseUrl` | provider | - | hidden | **OK** | app/providers/openai.py:118 | - | - |
| `providerAsrModel` | provider | - | hidden | **OK** | app/providers/openai.py:124 | - | - |
| `providerLlmApiKey` | provider | - | hidden,secret | **OK** | app/providers/openai.py:77 | - | - |
| `providerLlmBaseUrl` | provider | - | hidden | **OK** | app/providers/openai.py:74 | - | - |
| `providerLlmModel` | provider | - | hidden | **OK** | app/providers/openai.py:80 | - | - |
| `routerAutoRegister` | router | - | - | **OK** | app/boot.py:440<br>app/settings_effects.py:136 | - | - |
| `routerDisplayName` | router | - | - | **OK** | app/router_admin.py:508 | - | - |
| `allowVirtualInputDevice` | voice | record | hidden | **OK** | app/audio/recorder.py:272 | - | - |
| `beepOnDone` | voice | beep | - | **OK** | app/assistant.py:424 | - | - |
| `beepOnSend` | voice | beep | - | **OK** | app/assistant.py:626 | - | - |
| `beepOnStart` | voice | beep | - | **OK** | app/assistant.py:401 | - | - |
| `commandIdleRotateHours` | voice | command | - | **OK** | app/agents/dsh_agent.py:597 | - | - |
| `commandTargetSession` | voice | command | - | **OK** | app/assistant.py:506 | web/app.js:1212 | - |
| `commandTargetWorkspace` | voice | command | - | **OK** | app/assistant.py:505 | web/app.js:1211 | - |
| `commandWorkspace` | voice | command | - | **OK** | app/paths.py:201 | - | - |
| `consumeMediaKey` | voice | record | - | **OK** | app/platform/win32/hotkey.py:148 | - | - |
| `maxBriefChars` | voice | speech | - | **OK** | app/assistant.py:635 | - | - |
| `maxRecordMs` | voice | record | - | **OK** | app/assistant.py:411 | - | - |
| `minimalReply` | voice | speech | - | **OK** | app/assistant.py:169 | - | - |
| `minimalReplyChars` | voice | speech | - | **OK** | app/assistant.py:175<br>app/assistant.py:618 | - | - |
| `minimalReplyHint` | voice | speech | - | **OK** | app/assistant.py:171 | - | - |
| `noSpeechAbortMs` | voice | record | - | **OK** | app/assistant.py:414 | - | - |
| `notifyOnSend` | voice | beep | - | **OK** | app/assistant.py:436<br>app/assistant.py:448<br>app/assistant.py:452<br>app/assistant.py:584 | - | - |
| `outputDeviceIds` | voice | speech | - | **OK** | app/audio/output.py:195<br>app/audio/output.py:225 | - | - |
| `sendEnvContext` | voice | command | - | **OK** | app/assistant.py:150 | - | - |
| `silenceHangoverMs` | voice | record | - | **OK** | app/assistant.py:413 | - | - |
| `silenceThreshold` | voice | record | - | **OK** | app/assistant.py:412<br>app/assistant.py:434 | - | - |
| `sttLanguage` | voice | record | - | **OK** | app/assistant.py:345<br>app/assistant.py:361<br>app/meeting.py:1426<br>app/meeting.py:1447 | - | - |
| `triggerKeys` | voice | record | - | **OK** | app/runtime.py:216<br>mac/mac_runtime.py:185 | - | - |
| `ttsEngine` | voice | speech | - | **OK** | app/boot.py:518<br>app/config.py:1045<br>app/providers/__init__.py:144 | web/app.js:1840<br>web/app.js:2046<br>web/app.js:2088 | - |
| `userLocation` | voice | command | - | **OK** | app/assistant.py:155 | - | - |
| `voiceBrief` | voice | speech | - | **OK** | app/assistant.py:640 | - | - |
| `voiceConfirm` | voice | speech | - | **OK** | app/assistant.py:472 | - | - |
| `wakeAliases` | wake | - | - | **OK** | app/audio/wake.py:229 | - | - |
| `wakeConfirmN` | wake | - | - | **OK** | app/audio/wake.py:290<br>app/audio/wake.py:302 | - | - |
| `wakeConfirmX` | wake | - | - | **OK** | app/audio/wake.py:289<br>app/audio/wake.py:301 | - | - |
| `wakeCooldownSec` | wake | - | - | **OK** | app/audio/wake.py:288 | - | - |
| `wakeEnabled` | wake | - | - | **OK** | app/boot.py:536<br>app/install_state.py:207<br>app/model_usage.py:185<br>app/runtime.py:270 | - | - |
| `wakeKeywords` | wake | - | - | **OK** | app/assistant.py:113<br>app/audio/wake.py:186<br>app/audio/wake.py:227 | - | - |
| `wakePaused` | wake | - | - | **OK** | app/audio/wake.py:294<br>app/audio/wake.py:300<br>app/audio/wake.py:314 | - | - |
| `wakeSilenceFloor` | wake | - | - | **OK** | app/audio/wake.py:291 | - | - |
| `wakeThreshold` | wake | - | - | **OK** | app/audio/wake.py:256 | - | - |
| `worklogEnabled` | worklog | - | - | **OK** | app/worklog.py:59 | - | - |
| `worklogEnsureSessionAccess` | worklog | - | - | **OK** | app/worklog.py:103 | - | - |
| `worklogPrompt` | worklog | - | - | **OK** | app/worklog.py:227 | - | - |
| `worklogVaultRoot` | worklog | - | - | **OK** | app/worklog.py:64 | - | - |
| `dshNodePath` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `dshPackageDir` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `dshStartCommand` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `meetingDiarize` | model | - | deprecated | **DEPRECATED** | - | - | - |
| `meetingSttModel` | model | - | deprecated | **DEPRECATED** | mac/run_mac.py:40 | - | - |
| `voiceprintEnabled` | model | - | deprecated | **DEPRECATED** | - | - | - |
| `providerTts` | provider | - | deprecated,hidden | **DEPRECATED** | - | - | - |
| `worklogMode` | worklog | - | deprecated | **DEPRECATED** | - | - | - |
