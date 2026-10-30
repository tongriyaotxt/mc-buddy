# companion —— 陪玩指挥层

> 2026-10-26 operator：「保留 altoclef 的所有能力的前提下，在上面构造一个陪玩的指挥层，以后可能还会接入 ai」。
>
> 📌 **施工图**：`docs/PLAN-OMNI-COMPANION.md`（里程碑 M0~M5、判据、估算）
> 📌 **进度口径**：`companion/capabilities.yaml`（唯一权威，别处不许另立）
> 📌 **转向理由**：`docs/PIVOT-OMNI-COMPANION.md`

```
companion/
├── body.py        # 身体通道：MCP 客户端 + 双重编码解码（解到不是字符串为止）+ say()
├── registry.py    # 能力注册表：76 工具（运行时自动发现）+ 40 条 @命令 + 内部动作
├── actions.py     # 动作层：看世界→动作→再看世界（站位/挖矿/界面守护/读chat/任务链哨兵）
├── guards.py      # 礼仪/安全闸：NoChat / Cooldown / Safety / Etiquette
├── planner.py     # 决策：Intent + RulePlanner / AIPlanner / HybridPlanner（AI 插这里）
├── agent.py       # 会自己用工具的 agent（**DeepSeek 原生 tool calls + 流式**；无端点时降级规则）
├── team.py        # 多陪玩协作：黑板 + 任务认领 + 编队站位 + 说话权（详见 docs/DESIGN-MULTI-BUDDY.md）
├── nlu.py         # 人话 → 意图（指令化与自然语言同一条路）
├── chat_in.py     # 听：getRecentChat → 三道过滤 → NLU → 请求总线
├── speech.py      # 说：限流 / 去重 / 截断 / 语气分层（能说话之后，难的是别烦人）
├── voice_io.py    # 声音：SapiTTS + **SapiSTT（Windows zh-CN，零下载）** / HttpTTS / WhisperSTT
├── memory.py      # **人物记忆**（称呼/特征/承诺/历史；明确拒绝场景记忆）
├── bus.py         # 请求总线：人说的话 / 面板请求 都从这里进
├── body_ctl.py    # 身体进程控制：restart / ensure
├── commander.py   # 主循环：观察 → 界面守护 → 听 → 决策 → 过闸 → 单作业 → 记账 → 自愈
└── capabilities.yaml  # 能力表现状（唯一进度口径；验收台 tools/caps_test.py 出证据）
```

配套工具（项目根 `tools/`）：

| 脚本 | 干什么 |
|---|---|
| `watch_body.py run/report/doctor` | 身体守护 + 自检报告 + 五项体检（**出问题先看它**）|
| `caps_test.py` | 15 项能力验收台，逐项核验世界状态 |
| `fake_player.js` | 假人（mineflayer 真客户端）：能走/能跟/能捡东西/能在聊天里下命令 |
| `buddy_body.py` | 身体进程守护：status/restart/ensure |
| `screen_guard.py` | 关掉挡路的 GUI（GUI 开着 = 准星死 = 什么都不能做）|
| `_tile_windows.py` | 把多个 MC 窗口平铺（**遮挡同样会让准星冻结**，2026-10-27 实测）|
| `_fastlane_test.py` | 快车道 / 取消 / 掉线退避 三条机制的现场判据 |
| `_team_test.py` | 多陪玩四条规则（说话权/认领/编队/心跳）的纯逻辑判据 |
| `_move_probe.py` / `_focus_probe.py` / `_win_map.py` | 身体冻住时的分层排查（走路腿 / 焦点 / 窗口↔进程↔用户名）|
| `launcher/buddy_app.py` → `dist/Buddy.exe` | 脱离 MC 的控制台：多开数量 / 参与度 / 组队 / DeepSeek / 状态表 / 快捷指令 |

### 2026-10-27 白天（详见 `docs/RUN-2026-10-27.md`）

1. **交互饥饿**修掉：作业跑着时 `say/ask/stop` 走**快车道**（0.22 秒应答，原来 30 秒）。
2. **作业可取消**：取消旗穿进 `goto/mine/wait_goal`，加 240 秒硬预算看门狗（1.45 秒收尾）。
3. **掉线自愈**：`no_body` 退避 + 8/60 秒 `ensure()`，恢复写一行 `body_back`。
4. **"身体冻住"的第四、五真因**：准星不再推进 + 站在实体身上；新增 `aim_alive()`（0.6 秒判死）
   与 `unstick()`（挪一格），健康阶梯变成 retry → @goto → 界面守护 → **挪一格** → 重启。
5. **DeepSeek agent**：原生 tool_calls 循环，长活走指挥层作业线程（不破坏单瞄准者），带预算与 token 记账。
6. **多陪玩协作**（`team.py`）：说话权 / 认领 / 编队 / 心跳；请求带 `to`，杜绝 N 份重复执行。
7. **控制台 v2** + 重新打包 `dist/Buddy.exe`。

### 2026-10-26 夜的大变动（operator 九条要求，详见 `docs/NIGHT-RUN-2026-10-26.md`）

1. **零聊天输出取消了** —— 陪玩可以说话，但必须走 `speech.py`（限流/去重/语气）。
2. **人怎么指挥它**：游戏聊天框打字（指令化 `b 挖木头` 或自然语言 `帮我砍三根木头`）、
   EXE 面板、以后语音 —— 三条路都汇到 `bus.py`。
3. **人物记忆**（不是场景记忆）：记称呼/特征/承诺，**拒绝**存坐标与方块。
4. **补了一次 jar**：66 → 76 个工具（`getEntities` / `scanBlocks` / `closeScreen` /
   `respawnPlayer` / `taskChain` / `destroyBlockAt` …）。
5. **一键传送**：命令瞬移（需 OP）与走路两条腿。

### 2026-10-26 晚的关键发现（改了整条设计）

**GUI 开着 = 身体是死的。** `MinecraftClient.currentScreen != null` 时 tungsten 的
`WindMouseRotation.applyRenderStep` 每帧直接 return（第 137 行），于是准星永不转、
`crosshairTarget` 永不等于目标、`attackKey` 永不按下 —— chat 里就是
`Mining aborted: ticks=302 … progress=-0.10`。实测：关掉界面后同一批目标 `mineBlock` **2/2 成功**。

⇒ 从此指挥层**每个 tick 先过界面守护**（`actions.ensure_no_screen`：发 ESC 关掉，
并已把 `options.txt` 的 `pauseOnLostFocus` 改成 false 防复发）。

## 这个仓现在的整体逻辑

```
 ┌──────────────────────────────────────────────────────────────┐
 │  ① 身体（不归我们管，原样保留）                                │
 │     真 Minecraft 客户端 + UnionClef(AltoClef) + tungsten       │
 │     · 66 个 MCP 工具（有 schema，运行时可发现）                 │
 │     · 40 条 @命令（@get/@hero/@mineblock/@selfcare/@alive…）    │
 └───────────────▲──────────────────────────────┬───────────────┘
                 │ 动作（工具/命令）              │ 观察（getGameState/playerDetail/pathStatus）
 ┌───────────────┴──────────────────────────────▼───────────────┐
 │  ② 指挥层 companion/  ← 本次新建，就是「陪玩的性格」           │
 │     registry  → 能力全量在册，一个不漏                          │
 │     planner   → 观察变成"想干什么"（规则 / AI / 混合）          │
 │     guards    → 想干的事先过礼仪与安全闸                        │
 │     commander → 一 tick 只放一件事过去 + 全程记账               │
 └───────────────▲───────────────────────────────────────────────┘
                 │ trace.jsonl（obs/intent/verdict/act）
 ┌───────────────┴───────────────────────────────────────────────┐
 │  ③ 复盘与训练（阶段 3 才接）                                    │
 │     账本 → 语料 → 微调那个 planner → 换进 HybridPlanner         │
 └───────────────────────────────────────────────────────────────┘
```

### 关键设计（为什么这样切）

| 决定 | 理由 |
|---|---|
| **能力不裁剪** | `Registry.catalog()` 把 66 工具 + 40 命令原样列出；`invoke()` 接受任意工具名或 `@命令`。指挥层只是"选"，不是"禁用"。 |
| **护栏在意图与执行之间** | 将来换成 AI 决策，护栏照样生效——否则换个 planner 就全漏。 |
| **planner 是 Protocol** | `RulePlanner`（现在）/`AIPlanner`（接模型）/`HybridPlanner`（AI 优先、沉默回落规则）。指挥循环一行不改。 |
| **铁律 5 焊死在 Body** | `ChatMessage` 在 `Body.call()` 里直接抛错，任何上层（含 AI）都发不出人话。 |
| **全程记账** | `runs/commander/<ts>/trace.jsonl`：每 tick 的 obs / intent / 护栏判决 / 执行结果。既复盘，也是将来训练 AI 的语料。 |
| **身体自愈** | 位移为 0 也算卡（`gotoXYZ` 会静默放弃），阶梯：重试 → `@goto` → 标记 `body_unhealthy`。 |

### 和旧栈的关系

| 旧 | 现在 |
|---|---|
| `policy/` + `bot/`（mineflayer 无头 bot） | **退为回归/A-B 对照**，不再承载陪玩 |
| `policy/partner/{observe,intent,divide}.js` | 它的"观察→意图→分寸"思想被搬到 `companion/planner.py`（Python 侧，跑在真客户端上） |
| `chains/`、`scenarios/*_real_v1.yaml`（8 站） | 退为压测/回归，不再是进度口径 |
| `tools/companion_clef.py`（v0.1）、`tools/companion_v2.py`（v2） | 被 `companion/` 取代；保留作对照 |

### 怎么跑

```powershell
# 看全量能力清单（证明"一个都没少"）
python -X utf8 -m companion.commander --settings "<...>\altoclef_settings.json" --catalog

# 纯规则陪玩
python -X utf8 -m companion.commander --settings "<...>\altoclef_settings.json" --seconds 60 --hz 3

# 接 AI（Ollama/OpenAI 兼容；AI 不出声就自动回落规则）
python -X utf8 -m companion.commander --settings "<...>\altoclef_settings.json" \
    --planner hybrid --ai-endpoint http://127.0.0.1:11434/v1/chat/completions --ai-model qwen2.5:7b
```
