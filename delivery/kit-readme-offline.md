# ECHO 离线最小包（给同事）

> 这一包**不用联网、也不用 AI 助手**：双击 `装我.cmd` 就装完了。

## 三句话

1. 把整个文件夹复制到目标机器（U 盘 / 内网共享都行）。
2. **双击 `装我.cmd`** —— 它会自己装好运行时、依赖、模型，最后打印**面板地址**。
3. 打开那个地址就是 ECHO 面板；说一句话试试，或开始一场会议录音。

## 包里有什么

| 文件 | 说明 |
|---|---|
| `装我.cmd` | **双击这个**。一条命令装完 |
| `install-offline.ps1` | 上面那个 cmd 真正调的东西（命令行用：`-Root D:\ECHO -Profile main`） |
| `ECHO\` | ECHO 主程序（已解开，约 10 MB）—— 里面的 `ECHO\scripts\install-all.ps1` 是快路入口 |
| `echo-install\` | 安装技能：**想交给 AI 助手装也行**（见下），与 `装我.cmd` 效果一样 |
| `bundle\wheels\` | 离线 Python 依赖（pip 用 `--no-index --find-links` 从这里装，**不联网**） |
| `bundle\models\sherpa-onnx-streaming\` | 流式转写模型（189 MB，随包） |
| `bundle\runtime\` | 兜底的 CPython 3.11 嵌入包 + get-pip.py（目标机没有 Python / uv 时用它） |
| `BUILD-INFO.txt` / `SHA256SUMS.txt` | 包的身份与主程序各文件的校验值 |

## 两条路（装出来的东西一样）

**快路（推荐，离线）**：双击 `装我.cmd`，或手动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-offline.ps1 -Root D:\ECHO
```

* 默认装到 `D:\ECHO`（没有 D 盘就 `C:\ECHO`）；装 `minimal` 档：**sherpa 流式转写**（语音指令 + 会议转写都能用）。
* 想要唤醒词/更多档位：`-Profile main`（唤醒词模型的权重不在本包里，那一步需要网络）。
* 装完**不需要联网、不需要 agent**。

**原路（把技能交给 AI 助手）**：把整个文件夹交给你的助手，说「按 echo-install 这个技能给我装 ECHO」。
技能仍然独立可用，只是**离线包不需要它**——它问你的那几件事，`装我.cmd` 全用默认值替你定了。

## 什么时候还需要联网

* 想要**会议纪要 / 语音指令的智能体**（DSH 标准版）：本包**不含** DSH ——
  `@deepseek-ai/dsh` 是私有 npm 包，许可上不能随包分发。需要时按 `echo-install` 技能的
  第 5 节联网装（或直接用已装的 DSH 桌面版）。
* 想要**唤醒词 / whisper 档 / SenseVoice / 说话人分离**：这些模型不随包，
  在面板 →「能力」里按需下载（走 ModelScope / hf-mirror 镜像）。

## 遇到问题

* **双击没反应 / 一闪而过**：在文件夹里按住 `Shift` 右键 →「在此处打开 PowerShell 窗口」，然后跑
  `powershell -NoProfile -ExecutionPolicy Bypass -File .\install-offline.ps1 -Root D:\ECHO`，
  报错就能看见了。日志在 `<安装目录>\data\logs\install-all.log`。
* 报 **`DLL load failed … 找不到指定的模块`**：缺 **Microsoft Visual C++ 2015-2022 运行库（x64）**，
  装 `https://aka.ms/vs/17/release/vc_redist.x64.exe` 后重跑（会弹 UAC）。
* 报 **`WebView2 初始化失败`** / **`.NET` 提示**：那是右缘浮动条要的，与 ECHO 本体无关，
  用浏览器打开面板即可（装完打印的那个地址）。
* 安装目录**必须是纯英文路径**（中文路径会让部分转写引擎读不出模型）。
