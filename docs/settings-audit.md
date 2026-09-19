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
| `panelHotkey` | panel | - | - | **OK-INDIRECT** | - | - | - |
| `routerBreakerCooldown` | router | - | - | **OK-INDIRECT** | - | - | - |
| `routerBreakerThreshold` | router | - | - | **OK-INDIRECT** | - | - | - |
| `routerConnectTimeout` | router | - | - | **OK-INDIRECT** | - | - | - |
| `routerFirstByteTimeout` | router | - | - | **OK-INDIRECT** | - | - | - |
| `routerProbeInterval` | router | - | - | **OK-INDIRECT** | - | - | - |
| `fallbackHotkey` | voice | record | - | **OK-INDIRECT** | - | - | - |
| `wakeHotkey` | voice | record | - | **OK-INDIRECT** | - | - | - |
| `panelAutoRefresh` | panel | - | - | **PANEL-READ** | - | web/app.js:2392 | - |
| `agentBackend` | agent | - | hidden | **OK** | app/agents/__init__.py:127 | - | - |
| `agentCustomPath` | agent | - | hidden | **OK** | app/agents/codebuddy.py:50 | web/app.js:962 | - |
| `meetingAutoSummarize` | meeting | - | - | **OK** | app/meeting.py:157<br>app/meeting.py:461 | - | - |
| `meetingKeepRawAudio` | meeting | - | - | **OK** | app/meeting.py:1881 | - | - |
| `meetingSegmentMinutes` | meeting | - | - | **OK** | app/meeting.py:156<br>app/meeting.py:172<br>app/meeting.py:459 | - | - |
| `meetingWorkspace` | meeting | - | - | **OK** | app/meeting.py:849 | - | - |
| `device` | model | - | - | **OK** | app/api.py:158<br>app/api.py:554<br>app/assistant.py:306<br>app/boot.py:296 | web/app.js:1796<br>web/app.js:1831 | - |
| `meetingDiarize` | model | - | - | **OK** | app/meeting.py:158<br>app/meeting.py:463 | web/app.js:1832 | - |
| `meetingSttModel` | model | - | - | **OK** | app/meeting.py:154<br>app/meeting.py:351<br>app/meeting.py:456<br>mac/run_mac.py:36 | web/app.js:1234<br>web/app.js:1792<br>web/app.js:1829 | - |
| `sttModel` | model | - | - | **OK** | app/assistant.py:297<br>app/meeting.py:167<br>app/meeting.py:456<br>app/meeting.py:523 | web/app.js:1233<br>web/app.js:1792<br>web/app.js:1828 | - |
| `voiceprintAutoEnroll` | model | - | - | **OK** | app/voiceprint.py:49 | web/app.js:1869 | - |
| `voiceprintEnabled` | model | - | - | **OK** | app/voiceprint.py:44 | web/app.js:1852<br>web/app.js:1868 | - |
| `voiceprintMargin` | model | - | - | **OK** | app/voiceprint.py:59 | web/app.js:1871 | - |
| `voiceprintThreshold` | model | - | - | **OK** | app/voiceprint.py:55 | web/app.js:1870 | - |
| `wakeEngine` | model | - | - | **OK** | app/audio/wake.py:43<br>app/audio/wake.py:203 | web/app.js:1797<br>web/app.js:1830 | - |
| `apiAuthEnabled` | panel | - | - | **OK** | app/api.py:50 | web/app.js:2028 | - |
| `dshBaseUrl` | panel | - | - | **OK** | app/agents/dsh_agent.py:125 | - | - |
| `panelAutoStart` | panel | - | - | **OK** | app/runtime.py:106<br>mac/mac_runtime.py:67 | - | - |
| `panelOpenMode` | panel | - | - | **OK** | app/runtime.py:104<br>app/runtime.py:175<br>app/runtime.py:207<br>mac/mac_runtime.py:59 | - | mac/run_mac.py:65 |
| `panelStartCollapsed` | panel | - | - | **OK** | app/runtime.py:112<br>mac/mac_runtime.py:70 | - | - |
| `serverPort` | panel | - | - | **OK** | app/main.py:190<br>app/runtime.py:65<br>app/runtime.py:173<br>mac/mac_runtime.py:47 | - | - |
| `meetingsDir` | paths | - | - | **OK** | app/paths.py:142<br>app/paths.py:182 | - | - |
| `modelsDir` | paths | - | - | **OK** | app/paths.py:149<br>app/paths.py:163<br>app/paths.py:183 | - | - |
| `providerAsr` | provider | - | hidden | **OK** | app/meeting.py:424<br>app/providers/__init__.py:203 | - | - |
| `providerAsrApiKey` | provider | - | hidden,secret | **OK** | app/providers/openai.py:121 | - | - |
| `providerAsrBaseUrl` | provider | - | hidden | **OK** | app/providers/openai.py:118 | - | - |
| `providerAsrModel` | provider | - | hidden | **OK** | app/providers/openai.py:124 | - | - |
| `providerLlm` | provider | - | hidden | **OK** | app/meeting.py:1539 | - | - |
| `providerLlmApiKey` | provider | - | hidden,secret | **OK** | app/providers/openai.py:77 | - | - |
| `providerLlmBaseUrl` | provider | - | hidden | **OK** | app/providers/openai.py:74 | - | - |
| `providerLlmModel` | provider | - | hidden | **OK** | app/providers/openai.py:80 | - | - |
| `routerAutoRegister` | router | - | - | **OK** | app/boot.py:252 | - | - |
| `routerDisplayName` | router | - | - | **OK** | app/router_admin.py:458 | - | - |
| `beepOnDone` | voice | beep | - | **OK** | app/assistant.py:333 | - | - |
| `beepOnSend` | voice | beep | - | **OK** | app/assistant.py:504 | - | - |
| `beepOnStart` | voice | beep | - | **OK** | app/assistant.py:317 | - | - |
| `commandIdleRotateHours` | voice | command | - | **OK** | app/agents/dsh_agent.py:514 | - | - |
| `commandTargetSession` | voice | command | - | **OK** | app/assistant.py:401 | web/app.js:767 | - |
| `commandTargetWorkspace` | voice | command | - | **OK** | app/assistant.py:400 | web/app.js:766 | - |
| `commandWorkspace` | voice | command | - | **OK** | app/agents/dsh_agent.py:505<br>app/agents/dsh_agent.py:551 | - | - |
| `consumeMediaKey` | voice | record | - | **OK** | app/platform/win32/hotkey.py:148 | - | - |
| `inputDeviceId` | voice | record | - | **OK** | app/api.py:1084<br>app/assistant.py:328<br>app/assistant.py:337<br>app/meeting.py:173 | - | - |
| `maxBriefChars` | voice | speech | - | **OK** | app/assistant.py:513 | - | - |
| `maxRecordMs` | voice | record | - | **OK** | app/assistant.py:324 | - | - |
| `minimalReply` | voice | speech | - | **OK** | app/assistant.py:121 | - | - |
| `minimalReplyChars` | voice | speech | - | **OK** | app/assistant.py:127<br>app/assistant.py:496 | - | - |
| `minimalReplyHint` | voice | speech | - | **OK** | app/assistant.py:123 | - | - |
| `noSpeechAbortMs` | voice | record | - | **OK** | app/assistant.py:327 | - | - |
| `notifyOnSend` | voice | beep | - | **OK** | app/assistant.py:340<br>app/assistant.py:462<br>app/assistant.py:506 | - | - |
| `sendEnvContext` | voice | command | - | **OK** | app/assistant.py:102 | - | - |
| `silenceHangoverMs` | voice | record | - | **OK** | app/assistant.py:326 | - | - |
| `silenceThreshold` | voice | record | - | **OK** | app/assistant.py:325<br>app/assistant.py:338 | - | - |
| `sttLanguage` | voice | record | - | **OK** | app/assistant.py:289<br>app/assistant.py:305<br>app/meeting.py:390<br>app/meeting.py:458 | - | - |
| `triggerKeys` | voice | record | - | **OK** | app/runtime.py:215<br>mac/mac_runtime.py:134 | - | - |
| `ttsEngine` | voice | speech | - | **OK** | app/boot.py:343<br>app/config.py:542<br>app/providers/__init__.py:144 | web/app.js:1084<br>web/app.js:1183<br>web/app.js:1199 | - |
| `userLocation` | voice | command | - | **OK** | app/assistant.py:107 | - | - |
| `voiceBrief` | voice | speech | - | **OK** | app/assistant.py:518 | - | - |
| `voiceConfirm` | voice | speech | - | **OK** | app/assistant.py:367 | - | - |
| `wakeAliases` | wake | - | - | **OK** | app/audio/wake.py:229 | - | - |
| `wakeConfirmN` | wake | - | - | **OK** | app/audio/wake.py:289<br>app/audio/wake.py:301 | - | - |
| `wakeConfirmX` | wake | - | - | **OK** | app/audio/wake.py:288<br>app/audio/wake.py:300 | - | - |
| `wakeCooldownSec` | wake | - | - | **OK** | app/audio/wake.py:287 | - | - |
| `wakeEnabled` | wake | - | - | **OK** | app/api.py:273<br>app/boot.py:361<br>app/runtime.py:269<br>mac/mac_runtime.py:192 | - | - |
| `wakeKeywords` | wake | - | - | **OK** | app/assistant.py:65<br>app/audio/wake.py:186<br>app/audio/wake.py:227 | - | - |
| `wakePaused` | wake | - | - | **OK** | app/audio/wake.py:293<br>app/audio/wake.py:299 | - | - |
| `wakeSilenceFloor` | wake | - | - | **OK** | app/audio/wake.py:290 | - | - |
| `wakeThreshold` | wake | - | - | **OK** | app/audio/wake.py:256 | - | - |
| `worklogEnabled` | worklog | - | - | **OK** | app/worklog.py:59 | - | - |
| `worklogEnsureSessionAccess` | worklog | - | - | **OK** | app/worklog.py:103 | - | - |
| `worklogPrompt` | worklog | - | - | **OK** | app/worklog.py:204 | - | - |
| `worklogVaultRoot` | worklog | - | - | **OK** | app/worklog.py:64 | - | - |
| `dshNodePath` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `dshPackageDir` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `dshStartCommand` | dsh | - | deprecated | **DEPRECATED** | - | - | - |
| `providerTts` | provider | - | deprecated,hidden | **DEPRECATED** | - | - | - |
| `worklogMode` | worklog | - | deprecated | **DEPRECATED** | - | - | - |
