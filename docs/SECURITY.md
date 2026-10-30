# SECURITY

## 1. 这个仓库里不会有你的密钥（我们自己的也没有）

公开这一版之前做过一次**逐文件脱敏**，脚本是 `tools/_publish_mc_buddy.py`（保留在仓库里，
可以自己跑一遍 `--check` 核对）。它做两件事：

1. **白名单复制**：只有明确列出的文件会被公开，`replays/` `worlds/` `runs/` 这类运行产物、
   以及 400+ 个研究脚本，一个都不进仓。
2. **复制时脱敏**：本机绝对路径、客户端目录、模型目录、用户名、以及任何 `sk-…` 形状的密钥，
   在写进公开树之前就替换成占位符；然后对产物再做一遍**泄露扫描**，命中即报错（`exit 1`）。

**这一版实际清掉的东西**（都在源工作树里，公开仓里已不存在）：

| 类别 | 原样 | 公开仓里 |
|---|---|---|
| DeepSeek API key | `config/ai.json` / `.env` 里的一个真 key | 只有 `config/ai.json.example`（`sk-REPLACE_ME`） |
| 本机绝对路径 | `D:\<repo>`、`%MC_HOME%
| 用户名 | `C:\Users\<真名>` | `C:\Users\<you>` |
| 启动器里写死的目录 | `launcher/core.py` 的 `Path(r"D:\…")` | 改为 `BUDDY_MC_HOME` / `BUDDY_CLIENTS` / `BUDDY_MODELS` 或 `launcher/paths.json` |
| 运行产物 | `replays/` `runs/` `worlds/` `_refs/` `_copies/` | 不入仓 |

> 如果你拿到过这一版**之前**的任何构建（jar / exe / 压缩包），里面的 `config/ai.json`
> 可能带着那把 DeepSeek key —— 换一把，比信任它便宜。

## 2. 它自己有权限吗？（默认很小）

- **服务端 RCON 密码**在配置里，默认 `buddy`，只连 `127.0.0.1`。这不是安全性，是本地便利值：
  换服请改 `server.properties` 和 `--rcon-password`。
- **诊断包**（崩溃留证）默认**只写本地** `runs/crash/`；`config/support.json` 的 `endpoint`
  留空就是不上传。要上传必须显式开 `enabled: true` 并填地址。
- **语音**默认本地识别（SenseVoice int8）；云端 TTS/ASR 需要你自己填 key，开启时会在 UI 里告知
  音频会离开本机。
- **AI 层**默认离线可用：没有 key 时降级成规则，不会偷偷联网。

## 3. 不要用在公服

多数公共服务器把自动操作算作宏/脚本，会封号。这个项目**只**面向单机、自建服、朋友服。
启动器首启弹窗写的就是这一条。

## 4. 报告问题

发现密钥泄露、路径泄露、或者任何"它做了你没让它做的事"，开 issue 时**不要贴日志原文**
（日志里带路径、带服务器地址）。贴脱敏后的片段。
