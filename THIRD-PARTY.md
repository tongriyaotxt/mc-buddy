# THIRD-PARTY —— 第三方组件与许可（2026-10-29）

> 本文件是 `NOTICE` 的机器可读补充：**每一项都写清楚"我们怎么用它、许可是什么、要不要随包分发"**。
> ⚠️ 标 **[待核]** 的必须在公开发布前由人确认（我不替你定法律）。

## 1. 运行底座（客户端侧）

| 组件 | 用途 | 许可 | 我们怎么分发 |
|---|---|---|---|
| AltoClef | 上游自动任务引擎 | **MIT**（Copyright 2020 Adris Jautakas） | 通过 UnionClef 底座间接包含 |
| UnionClef（`_refs/unionclef-0.95.63`） | 我们改的 fork：MCP 服务端 + hook 层 + 85 工具 | **GPL-3.0**（`LICENSE` 文件）；但 `fabric.mod.json` 写 **MIT** ⇒ **[待核]** 二者矛盾 | 按 GPL-3.0 合规：**补丁源码与构建脚本随包公开** |
| tungsten | 寻路/执行器（`META-INF/jars/tungsten-BETA-1.0.0.jar`） | 以原仓库为准 **[待核]** | 随 UnionClef 的混血 jar 提供 |
| Baritone | 寻路思路 / 部分代码 | 以原仓库为准 **[待核]** | 间接 |
| Fabric Loader / Fabric API | 模组加载 | Apache-2.0 | 用户自行安装 |

## 2. 语音

| 组件 | 用途 | 许可 | 备注 |
|---|---|---|---|
| sherpa-onnx 2.0.1 | 本地 ASR 运行时（自带 onnxruntime.dll） | **Apache-2.0** | wheel 28.6MB，随包 |
| SenseVoice-small int8（`csukuangfj/...-2024-07-17`） | 中文识别模型 228MB | **[待核]**（以模型仓库 LICENSE 为准） | 外置下载，不进 exe |
| sounddevice / PortAudio | 录音 | **MIT** | 随包 |
| CosyVoice2（可选） | 本地 TTS | **Apache-2.0** | 可选，需用户自装 |
| 火山/豆包实时语音（可选） | 云端 TTS/ASR | 商业条款 | 用户自备 key；开启时明确告知音频上传 |

## 3. 服务端 / 游戏

| 组件 | 许可 | 我们怎么处理 |
|---|---|---|
| Paper / PaperMC | **GPL-3.0** | **不分发**，用户自行下载 |
| Minecraft 客户端/服务端资源与商标 | Mojang/Microsoft EULA | **不分发任何原版资源**；用户必须自备正版账号与客户端 |
| Java 运行时 | 各自许可 | 用户自行安装 |

## 4. 我们的分发模型（一句话）

```
我们的 Python/Qt/EXE     → Apache-2.0（本仓库 LICENSE）
改过的 mod（jar + 补丁） → GPL-3.0 合规：补丁源码与构建脚本公开（见 docs/COMPLIANCE.md §2）
模型 / Paper / MC 资源   → 一律不分发，用户自行获取
```

## 5. 待办（发布前必须勾掉）

- [ ] UnionClef 的 GPL-3.0 vs MIT 矛盾定案（决定 jar 分发形态）；
- [ ] SenseVoice 模型许可复核（能不能随安装器自动下载/再分发）；
- [ ] tungsten / Baritone 的 LICENSE 复核；
- [ ] 把本文件与 `NOTICE`、`LICENSE`、`docs/COMPLIANCE.md` 一起打进安装包。
