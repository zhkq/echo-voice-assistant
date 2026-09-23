# 发版记录

每次发 GitHub Release，把**线上正文原样存一份**到这里（文件名 `v<版本>.md`）。好处：

- 版本之间能 diff —— 「这版到底改了什么」不用去翻 tag 和提交日志；
- Release 正文被改坏/误删时，仓库里还有底；
- 和 `app/__init__.py:__version__`、git tag 一起，构成一条可核对的发版链。

**存的是线上那份，不是重打一遍**（避免"文档说 A、线上写 B"）：

```powershell
gh api repos/zhkq/echo-voice-assistant/releases/tags/v2.0.0 --jq .body > docs/releases/v2.0.0.md
```

> 注意：Windows 上跑这条前先 `[Console]::OutputEncoding=[Text.Encoding]::UTF8`，
> 否则中文正文会被按本地码页解码成乱码（05 那边踩过同类的坑）。

| 版本 | 发布日期 | 正文 | Release |
|---|---|---|---|
| v2.0.0 | 2026-09-23 | [v2.0.0.md](v2.0.0.md) | [链接](https://github.com/zhkq/echo-voice-assistant/releases/tag/v2.0.0) |
| v2.0.1 | 2026-09-23 | [v2.0.1.md](v2.0.1.md) | [链接](https://github.com/zhkq/echo-voice-assistant/releases/tag/v2.0.1) |
| v2.0.2 | 2026-09-23 | [v2.0.2.md](v2.0.2.md) | [链接](https://github.com/zhkq/echo-voice-assistant/releases/tag/v2.0.2) |
| v1.0.0 | 2026-09-18 | （未存档 —— 该惯例从 v2.0.0 起） | [链接](https://github.com/zhkq/echo-voice-assistant/releases/tag/v1.0.0) |

发版流程见 [REFACTOR-PLAN §13](../REFACTOR-PLAN.md)；出包见仓库根的
`scripts/build_kit.py`（`python scripts/build_kit.py`）。
