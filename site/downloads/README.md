# 下载文件目录

部署时把安装包放在这个目录（静态站根下的 `/downloads/`），文件名必须与下载页一致：

<!-- downloads-table:start -->
| 平台 | 文件 | 大小（字节） | SHA-256 |
|---|---|---|---|
| Windows | `ECHO-Windows-0.9.0-beta.zip` | 74,895,328 | `CC6B5AE5850EAB786C1C98248DC76BAEBFB3E5BD4FB67B84218B5EF2B71FFAE8` |
| macOS | `ECHO-macOS-0.9.0-beta.zip` | 8,784,030 | `76BCD9690E9AAF1AB8FBB5147C62A4DB512605A725D1C4D95EB1BC1F11DD2B51` |
| Android | `ECHO-Android-0.9.0-beta-debug.apk` | 22,346,187 | `5ADD81EBE1B86CD5D46968E36AFBB25C5FC2B61A38133C9BA7975202B7CE0C15` |
<!-- downloads-table:end -->

Windows/macOS 文件是 GitHub Release `v3.0.1` 交付包的改名副本；Android 是交付包内同一份
debug APK。**大文件不要提交进 git**；部署脚本/人工从 Release 取回后放到这里即可。
升级版本时用 `python scripts/update-download-page.py --write` 同步页面与这张表；
部署前后用 `powershell -ExecutionPolicy Bypass -File scripts\verify-downloads.ps1 -Dir <这里> -Full`
（或 `-BaseUrl https://echoserver.zhkq.online:8443/downloads`）核对实际文件。
