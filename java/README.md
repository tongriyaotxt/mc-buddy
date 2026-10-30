# java/ —— 模组侧补丁（GPL-3.0）

这些不是完整的模组源码，是**我们对 UnionClef（AltoClef + Tungsten 的整合 fork）所做的补丁文件**，
按“同样的相对路径覆盖到上游源码树”使用。

上游：UnionClef 0.95.63（AltoClef，MIT；UnionClef 整合层，GPL-3.0 —— 以它自己的 `LICENSE` 为准）。

## 文件

| 文件 | 做什么 | 没有它会怎样 |
|---|---|---|
| `util/agent/HeadlessBreak.java` | 判断“现在有挖/放的活要干” | —— |
| `mixins/ClientTickMixin.java` | 只对 `doItemUse()` 里**那一个** `isCursorLocked()` 调用点做 `@Redirect` | 没人点进去的客户端，原版每 tick 取消破坏 ⇒ **按了没反应** |
| `Py4jEntryPoint.java` | `placeBlockAtRaw`：把“没放上”分成 `cooldown` / `body_blocked` / `permanent` | 全部混成“冷却”，外面会重试八次还在原地 |
| `mcp/McpServer.java` | 89 个 MCP 工具与它们的描述 | 指挥层没有手 |
| `chains/MobDefenseChain.java` | 逃跑报价前先问“真有东西可逃吗”；并保留 PVP 决斗那一支 | 低血 + 挨饿 + 场上没怪 ⇒ 对着空气逃跑，**75 分永久压住吃饭**，人站着不动几小时 |
| `tasks/movement/CustomBaritoneGoalTask.java` | `FINISHED` 日志降频（同目标同格子只打一次 / 2 秒一次） | 一条重复日志能写满 4 小时（实测 21 万行 / 21.4 万行） |

## 怎么用

```bash
# 1) 拿上游源码
git clone <unionclef 0.95.63 源码> && cd unionclef
# 2) 把本目录的文件按相对路径覆盖进去
cp -r /path/to/mc-buddy/java/* src/main/java/
# 3) 编译（Windows：JDK 21；Linux 容器见上游 AGENTS.md）
./gradlew :1.21.11:build
# 4) ⚠️ 编译前删掉 IDE 的 bin/，否则成品里可能是旧类
rm -rf versions/*/bin
```

## 两个必须知道的坑

1. **`isCursorLocked` 的那个 Redirect 必须放 altoclef，不能放 tungsten** —— 打包脚本会把
   tungsten 的 jar 换成上游的，放在 tungsten 里等于没改。
2. **“改完要验证改动真的进了 jar”**，不要相信“编译成功”：把 class 从 jar 里抽出来查常量
   （本项目就是这么确认 `mobdef: 拒绝对着空气逃跑` 那版真的进去了）。

## 许可

本目录下的补丁是 UnionClef 的衍生作品，**GPL-3.0**；随附源码即为 GPL 合规的“提供对应源码”。
本项目其余部分（`companion/` `launcher/` `tools/` `docs/`）是 Apache-2.0。
