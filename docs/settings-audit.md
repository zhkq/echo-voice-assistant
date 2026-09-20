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
| `providerLlm` | provider | - | hidden | **OK-INDIRECT** | - | - | - |
| `routerBreakerCooldown` | router | - | - | **OK-INDIRECT** | - | - | - |
| `routerBreakerThreshold` | router | - | - | **OK-INDIRECT** | - | - | - |
| `routerConnectTimeout` | router | - | - | **OK-INDIRECT** | - | - | - |
| `routerFirstByteTimeout` | router | - | - | **OK-INDIRECT** | - | - | - |
| `routerProbeInterval` | router | - | - | **OK-INDIRECT** | - | - | - |
| `fallbackHotkey` | voice | record | - | **OK-INDIRECT** | - | - | - |
| `wakeHotkey` | voice | record | - | **OK-INDIRECT** | - | - | - |
| `panelAutoRefresh` | panel | - | - | **PANEL-READ** | - | web/app.js:3367 | - |
| `agentBackend` | agent | - | hidden | **OK** | app/harness_proc.py:95<br>app/agents/__init__.py:128 | - | - |
| `agentCustomPath` | agent | - | hidden | **OK** | app/agents/codebuddy.py:50 | - | - |
| `agentHarnessEnabled` | agent | - | hidden | **OK** | app/harness_proc.py:97 | - | - |
| `dshBaseUrl` | agent | - | hidden | **OK** | app/agents/dsh_agent.py:137 | - | - |
| `harnessCommand` | agent | - | hidden | **OK** | app/harness_proc.py:83 | - | - |
| `harnessHome` | agent | - | hidden | **OK** | app/harness_proc.py:75 | - | - |
| `harnessPort` | agent | - | hidden | **OK** | app/harness_proc.py:60 | - | - |
| `harnessToken` | agent | - | hidden,secret | **OK** | app/harness_proc.py:120 | - | - |
| `meetingAutoSummarize` | meeting | - | - | **OK** | app/meeting.py:160<br>app/meeting.py:488 | - | - |
| `meetingKeepRawAudio` | meeting | - | - | **OK** | app/meeting.py:1925 | - | - |
| `meetingSegmentMinutes` | meeting | - | - | **OK** | app/meeting.py:159<br>app/meeting.py:175<br>app/meeting.py:486 | - | - |
| `meetingWorkspace` | meeting | - | - | **OK** | app/meeting.py:876 | - | - |
| `device` | model | - | - | **OK** | app/api.py:158<br>app/api.py:610<br>app/assistant.py:305<br>app/boot.py:332 | web/app.js:2147<br>web/app.js:2199 | - |
| `meetingDiarize` | model | - | - | **OK** | app/meeting.py:161<br>app/meeting.py:490 | web/app.js:2200 | - |
| `meetingSttModel` | model | - | - | **OK** | app/meeting.py:157<br>app/meeting.py:378<br>app/meeting.py:483<br>mac/run_mac.py:40 | web/app.js:1509<br>web/app.js:2143<br>web/app.js:2197 | - |
| `sttModel` | model | - | - | **OK** | app/assistant.py:296<br>app/meeting.py:170<br>app/meeting.py:483<br>app/meeting.py:550 | web/app.js:1508<br>web/app.js:2143<br>web/app.js:2196 | - |
| `voiceprintAutoEnroll` | model | - | - | **OK** | app/voiceprint.py:49 | web/app.js:2237 | - |
| `voiceprintEnabled` | model | - | - | **OK** | app/voiceprint.py:44 | web/app.js:2220<br>web/app.js:2236 | - |
| `voiceprintMargin` | model | - | - | **OK** | app/voiceprint.py:59 | web/app.js:2239 | - |
| `voiceprintThreshold` | model | - | - | **OK** | app/voiceprint.py:55 | web/app.js:2238 | - |
| `wakeEngine` | model | - | - | **OK** | app/audio/wake.py:43<br>app/audio/wake.py:203 | web/app.js:2148<br>web/app.js:2198 | - |
| `apiAuthEnabled` | panel | - | - | **OK** | app/api.py:50 | web/app.js:2397 | - |
| `panelAutoStart` | panel | - | - | **OK** | app/runtime.py:107<br>mac/mac_runtime.py:118 | - | - |
| `panelOpenMode` | panel | - | - | **OK** | app/runtime.py:105<br>app/runtime.py:176<br>app/runtime.py:208<br>mac/mac_runtime.py:110 | - | mac/run_mac.py:69 |
| `panelStartCollapsed` | panel | - | - | **OK** | app/runtime.py:113<br>mac/mac_runtime.py:121 | - | - |
| `serverPort` | panel | - | - | **OK** | app/main.py:190<br>app/runtime.py:66<br>app/runtime.py:174<br>mac/mac_runtime.py:40 | - | - |
| `meetingsDir` | paths | - | - | **OK** | app/paths.py:142<br>app/paths.py:182 | - | - |
| `modelsDir` | paths | - | - | **OK** | app/paths.py:149<br>app/paths.py:163<br>app/paths.py:183 | - | - |
| `providerAsr` | provider | - | hidden | **OK** | app/meeting.py:451<br>app/providers/__init__.py:203 | - | - |
| `providerAsrApiKey` | provider | - | hidden,secret | **OK** | app/providers/openai.py:121 | - | - |
| `providerAsrBaseUrl` | provider | - | hidden | **OK** | app/providers/openai.py:118 | - | - |
| `providerAsrModel` | provider | - | hidden | **OK** | app/providers/openai.py:124 | - | - |
| `providerLlmApiKey` | provider | - | hidden,secret | **OK** | app/providers/openai.py:77 | - | - |
| `providerLlmBaseUrl` | provider | - | hidden | **OK** | app/providers/openai.py:74 | - | - |
| `providerLlmModel` | provider | - | hidden | **OK** | app/providers/openai.py:80 | - | - |
| `routerAutoRegister` | router | - | - | **OK** | app/boot.py:288 | - | - |
| `routerDisplayName` | router | - | - | **OK** | app/router_admin.py:458 | - | - |
| `beepOnDone` | voice | beep | - | **OK** | app/assistant.py:332 | - | - |
| `beepOnSend` | voice | beep | - | **OK** | app/assistant.py:503 | - | - |
| `beepOnStart` | voice | beep | - | **OK** | app/assistant.py:316 | - | - |
| `commandIdleRotateHours` | voice | command | - | **OK** | app/agents/dsh_agent.py:528 | - | - |
| `commandTargetSession` | voice | command | - | **OK** | app/assistant.py:400 | web/app.js:900 | - |
| `commandTargetWorkspace` | voice | command | - | **OK** | app/assistant.py:399 | web/app.js:899 | - |
| `commandWorkspace` | voice | command | - | **OK** | app/agents/dsh_agent.py:519<br>app/agents/dsh_agent.py:568 | - | - |
| `consumeMediaKey` | voice | record | - | **OK** | app/platform/win32/hotkey.py:148 | - | - |
| `inputDeviceId` | voice | record | - | **OK** | app/api.py:1242<br>app/assistant.py:327<br>app/assistant.py:336<br>app/meeting.py:176 | - | - |
| `maxBriefChars` | voice | speech | - | **OK** | app/assistant.py:512 | - | - |
| `maxRecordMs` | voice | record | - | **OK** | app/assistant.py:323 | - | - |
| `minimalReply` | voice | speech | - | **OK** | app/assistant.py:121 | - | - |
| `minimalReplyChars` | voice | speech | - | **OK** | app/assistant.py:127<br>app/assistant.py:495 | - | - |
| `minimalReplyHint` | voice | speech | - | **OK** | app/assistant.py:123 | - | - |
| `noSpeechAbortMs` | voice | record | - | **OK** | app/assistant.py:326 | - | - |
| `notifyOnSend` | voice | beep | - | **OK** | app/assistant.py:339<br>app/assistant.py:461<br>app/assistant.py:505 | - | - |
| `sendEnvContext` | voice | command | - | **OK** | app/assistant.py:102 | - | - |
| `silenceHangoverMs` | voice | record | - | **OK** | app/assistant.py:325 | - | - |
| `silenceThreshold` | voice | record | - | **OK** | app/assistant.py:324<br>app/assistant.py:337 | - | - |
| `sttLanguage` | voice | record | - | **OK** | app/assistant.py:288<br>app/assistant.py:304<br>app/meeting.py:417<br>app/meeting.py:485 | - | - |
| `triggerKeys` | voice | record | - | **OK** | app/runtime.py:216<br>mac/mac_runtime.py:185 | - | - |
| `ttsEngine` | voice | speech | - | **OK** | app/boot.py:379<br>app/config.py:575<br>app/providers/__init__.py:144 | web/app.js:1276<br>web/app.js:1430<br>web/app.js:1472 | - |
| `userLocation` | voice | command | - | **OK** | app/assistant.py:107 | - | - |
| `voiceBrief` | voice | speech | - | **OK** | app/assistant.py:517 | - | - |
| `voiceConfirm` | voice | speech | - | **OK** | app/assistant.py:366 | - | - |
| `wakeAliases` | wake | - | - | **OK** | app/audio/wake.py:229 | - | - |
| `wakeConfirmN` | wake | - | - | **OK** | app/audio/wake.py:289<br>app/audio/wake.py:301 | - | - |
| `wakeConfirmX` | wake | - | - | **OK** | app/audio/wake.py:288<br>app/audio/wake.py:300 | - | - |
| `wakeCooldownSec` | wake | - | - | **OK** | app/audio/wake.py:287 | - | - |
| `wakeEnabled` | wake | - | - | **OK** | app/api.py:273<br>app/boot.py:397<br>app/runtime.py:270<br>mac/mac_runtime.py:243 | - | - |
| `wakeKeywords` | wake | - | - | **OK** | app/assistant.py:65<br>app/audio/wake.py:186<br>app/audio/wake.py:227 | - | - |
| `wakePaused` | wake | - | - | **OK** | app/audio/wake.py:293<br>app/audio/wake.py:299<br>app/audio/wake.py:313 | - | - |
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
