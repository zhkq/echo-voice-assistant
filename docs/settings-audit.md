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
| `capabilityDiarizeBackend` | capability | - | hidden | **OK-INDIRECT** | - | - | - |
| `capabilityEmbedBackend` | capability | - | hidden | **OK-INDIRECT** | - | - | - |
| `capabilityMeetingAsrBackend` | capability | - | hidden | **OK-INDIRECT** | - | - | - |
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
| `panelAutoRefresh` | panel | - | - | **PANEL-READ** | - | web/app.js:3692 | - |
| `agentBackend` | agent | - | hidden | **OK** | app/components.py:364<br>app/harness_proc.py:142<br>app/install_state.py:208<br>app/agents/__init__.py:128 | - | - |
| `agentCustomPath` | agent | - | hidden | **OK** | app/agents/codebuddy.py:50 | - | - |
| `agentHarnessEnabled` | agent | - | hidden | **OK** | app/harness_proc.py:144 | - | - |
| `dshBaseUrl` | agent | - | hidden | **OK** | app/agents/dsh_agent.py:138 | - | - |
| `harnessCommand` | agent | - | hidden | **OK** | app/harness_proc.py:121 | - | - |
| `harnessHome` | agent | - | hidden | **OK** | app/harness_proc.py:77 | - | - |
| `harnessPort` | agent | - | hidden | **OK** | app/harness_proc.py:62 | - | - |
| `harnessToken` | agent | - | hidden,secret | **OK** | app/harness_proc.py:167 | - | - |
| `capabilityEchoServerStaticToken` | capability | - | hidden,secret | **OK** | app/capabilities/echo_server.py:96 | - | - |
| `capabilityEchoServerToken` | capability | - | hidden,secret | **OK** | app/capabilities/echo_server.py:93 | - | - |
| `capabilityEchoServerUrl` | capability | - | hidden | **OK** | app/meeting.py:433<br>app/capabilities/echo_server.py:79 | - | - |
| `capabilityPrivacy` | capability | - | hidden | **OK** | app/capability_admin.py:123 | - | - |
| `meetingAutoSummarize` | meeting | - | - | **OK** | app/meeting.py:164<br>app/meeting.py:561 | - | - |
| `meetingKeepRawAudio` | meeting | - | - | **OK** | app/meeting.py:2075 | - | - |
| `meetingSegmentMinutes` | meeting | - | - | **OK** | app/meeting.py:163<br>app/meeting.py:195<br>app/meeting.py:559 | - | - |
| `meetingWorkspace` | meeting | - | - | **OK** | app/meeting.py:1024 | - | - |
| `device` | model | - | - | **OK** | app/api.py:239<br>app/api.py:700<br>app/assistant.py:362<br>app/boot.py:501 | web/app.js:2464<br>web/app.js:2516 | - |
| `meetingDiarize` | model | - | - | **OK** | app/meeting.py:165<br>app/meeting.py:563 | web/app.js:2517 | - |
| `meetingSttModel` | model | - | - | **OK** | app/meeting.py:161<br>app/meeting.py:373<br>app/meeting.py:556<br>app/capabilities/local.py:180 | web/app.js:1673<br>web/app.js:2460<br>web/app.js:2514 | - |
| `sttModel` | model | - | - | **OK** | app/assistant.py:353<br>app/meeting.py:174<br>app/meeting.py:556<br>app/meeting.py:623 | web/app.js:1672<br>web/app.js:2460<br>web/app.js:2513 | - |
| `voiceprintAutoEnroll` | model | - | - | **OK** | app/voiceprint.py:49 | web/app.js:2554 | - |
| `voiceprintEnabled` | model | - | - | **OK** | app/voiceprint.py:44 | web/app.js:2537<br>web/app.js:2553 | - |
| `voiceprintMargin` | model | - | - | **OK** | app/voiceprint.py:59 | web/app.js:2556 | - |
| `voiceprintThreshold` | model | - | - | **OK** | app/voiceprint.py:55 | web/app.js:2555 | - |
| `wakeEngine` | model | - | - | **OK** | app/audio/wake.py:43<br>app/audio/wake.py:203 | web/app.js:2465<br>web/app.js:2515 | - |
| `apiAuthEnabled` | panel | - | - | **OK** | app/api.py:50 | web/app.js:2716 | - |
| `panelAutoStart` | panel | - | - | **OK** | app/runtime.py:107<br>mac/mac_runtime.py:118 | - | - |
| `panelOpenMode` | panel | - | - | **OK** | app/runtime.py:105<br>app/runtime.py:176<br>app/runtime.py:208<br>mac/mac_runtime.py:110 | - | mac/run_mac.py:69 |
| `panelStartCollapsed` | panel | - | - | **OK** | app/runtime.py:113<br>mac/mac_runtime.py:121 | - | - |
| `serverPort` | panel | - | - | **OK** | app/main.py:213<br>app/runtime.py:66<br>app/runtime.py:174<br>mac/mac_runtime.py:40 | - | - |
| `meetingsDir` | paths | - | - | **OK** | app/paths.py:142<br>app/paths.py:182 | - | - |
| `modelsDir` | paths | - | - | **OK** | app/paths.py:149<br>app/paths.py:163<br>app/paths.py:183 | - | - |
| `providerAsr` | provider | - | hidden | **OK** | app/meeting.py:533<br>app/providers/__init__.py:203 | - | - |
| `providerAsrApiKey` | provider | - | hidden,secret | **OK** | app/providers/openai.py:121 | - | - |
| `providerAsrBaseUrl` | provider | - | hidden | **OK** | app/providers/openai.py:118 | - | - |
| `providerAsrModel` | provider | - | hidden | **OK** | app/providers/openai.py:124 | - | - |
| `providerLlmApiKey` | provider | - | hidden,secret | **OK** | app/providers/openai.py:77 | - | - |
| `providerLlmBaseUrl` | provider | - | hidden | **OK** | app/providers/openai.py:74 | - | - |
| `providerLlmModel` | provider | - | hidden | **OK** | app/providers/openai.py:80 | - | - |
| `routerAutoRegister` | router | - | - | **OK** | app/boot.py:439<br>app/settings_effects.py:136 | - | - |
| `routerDisplayName` | router | - | - | **OK** | app/router_admin.py:508 | - | - |
| `allowVirtualInputDevice` | voice | record | hidden | **OK** | app/audio/recorder.py:272 | - | - |
| `beepOnDone` | voice | beep | - | **OK** | app/assistant.py:424 | - | - |
| `beepOnSend` | voice | beep | - | **OK** | app/assistant.py:609 | - | - |
| `beepOnStart` | voice | beep | - | **OK** | app/assistant.py:401 | - | - |
| `commandIdleRotateHours` | voice | command | - | **OK** | app/agents/dsh_agent.py:593 | - | - |
| `commandTargetSession` | voice | command | - | **OK** | app/assistant.py:506 | web/app.js:1038 | - |
| `commandTargetWorkspace` | voice | command | - | **OK** | app/assistant.py:505 | web/app.js:1037 | - |
| `commandWorkspace` | voice | command | - | **OK** | app/agents/dsh_agent.py:584<br>app/agents/dsh_agent.py:633 | - | - |
| `consumeMediaKey` | voice | record | - | **OK** | app/platform/win32/hotkey.py:148 | - | - |
| `maxBriefChars` | voice | speech | - | **OK** | app/assistant.py:618 | - | - |
| `maxRecordMs` | voice | record | - | **OK** | app/assistant.py:411 | - | - |
| `minimalReply` | voice | speech | - | **OK** | app/assistant.py:169 | - | - |
| `minimalReplyChars` | voice | speech | - | **OK** | app/assistant.py:175<br>app/assistant.py:601 | - | - |
| `minimalReplyHint` | voice | speech | - | **OK** | app/assistant.py:171 | - | - |
| `noSpeechAbortMs` | voice | record | - | **OK** | app/assistant.py:414 | - | - |
| `notifyOnSend` | voice | beep | - | **OK** | app/assistant.py:436<br>app/assistant.py:448<br>app/assistant.py:452<br>app/assistant.py:567 | - | - |
| `outputDeviceIds` | voice | speech | - | **OK** | app/audio/output.py:195<br>app/audio/output.py:225 | - | - |
| `sendEnvContext` | voice | command | - | **OK** | app/assistant.py:150 | - | - |
| `silenceHangoverMs` | voice | record | - | **OK** | app/assistant.py:413 | - | - |
| `silenceThreshold` | voice | record | - | **OK** | app/assistant.py:412<br>app/assistant.py:434 | - | - |
| `sttLanguage` | voice | record | - | **OK** | app/assistant.py:345<br>app/assistant.py:361<br>app/meeting.py:462<br>app/meeting.py:480 | - | - |
| `triggerKeys` | voice | record | - | **OK** | app/runtime.py:216<br>mac/mac_runtime.py:185 | - | - |
| `ttsEngine` | voice | speech | - | **OK** | app/boot.py:548<br>app/config.py:827<br>app/providers/__init__.py:144 | web/app.js:1424<br>web/app.js:1594<br>web/app.js:1636 | - |
| `userLocation` | voice | command | - | **OK** | app/assistant.py:155 | - | - |
| `voiceBrief` | voice | speech | - | **OK** | app/assistant.py:623 | - | - |
| `voiceConfirm` | voice | speech | - | **OK** | app/assistant.py:472 | - | - |
| `wakeAliases` | wake | - | - | **OK** | app/audio/wake.py:229 | - | - |
| `wakeConfirmN` | wake | - | - | **OK** | app/audio/wake.py:290<br>app/audio/wake.py:302 | - | - |
| `wakeConfirmX` | wake | - | - | **OK** | app/audio/wake.py:289<br>app/audio/wake.py:301 | - | - |
| `wakeCooldownSec` | wake | - | - | **OK** | app/audio/wake.py:288 | - | - |
| `wakeEnabled` | wake | - | - | **OK** | app/boot.py:566<br>app/install_state.py:207<br>app/runtime.py:270<br>app/settings_effects.py:80 | - | - |
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
| `providerTts` | provider | - | deprecated,hidden | **DEPRECATED** | - | - | - |
| `worklogMode` | worklog | - | deprecated | **DEPRECATED** | - | - | - |
