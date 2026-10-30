# mc-buddy

**一个会自己决定"该干什么"的 Minecraft 陪玩 AI。**

> for my brother xiaorun — 写给我兄弟 Xiaorun：他一个人玩的时候，服务器里至少有个活人。

它不是一个"脚本机器人"。它的脑子里没有神经网络权重，也没有漂亮的对话模型幻觉：

- **手**在一个 Minecraft 模组里（Fabric + AltoClef/Tungsten），通过 MCP 暴露成 89 个工具；
- **脑子**在 Python 里（`companion/`），拿状态、排计划、下命令、看结果；
- **嘴**在同一个 Python 进程里（限流 6 秒、去重 90 秒），说话走语音和聊天两条路；
- **验收**不靠"看起来像"，靠一局一局的读数（黑匣子回放 + 32 项自检闸）。

目标很朴素也很硬：**让它像个人一样陪玩——跟着你、帮你挡刀、替你干活、别烦人、别站着不动。**

---

## 现在能干什么（都带读数，不是"应该能"）

| 能力 | 状态 | 读数是什么 |
|---|---|---|
| 跟随 / 让路 / 点头摇头 / 应答 / 护主 | ✅ | 自检闸 ③④⑪ 全绿；护主抢占与"应答不抢占"各有单测 |
| 说话（打字 + 系统 TTS 朗读） | ✅ | 限流 6s / 去重 90s / 单条截断 |
| 听人说话（本地 SenseVoice 识别） | ✅ | 兜底链 sapi→sherpa→cosyvoice→火山；静音过滤两道 |
| 别把自己说的话当指令 | ✅ | 静音期 0 泄漏；**拆掉闸的对照组必须失败**（有牙的测试） |
| 多人陪玩（同一个服 3 个） | ✅ | 3 在线 / 3 心跳 / **只有说话权者执行** / 语音只跑一份 |
| 目标分解 + 自动前置 + 变通阶梯 | ✅ | 闸 ⑮ 五项、⑯ 七项、⑫（没牛→否决皮甲→退铁→退木剑→不硬打） |
| 挖 / 放（"回 ok 但世界没变"已修） | ✅ | 挖一格草：进度 0.3→0.9→1 秒内变 air；放置三种失败各自可辨 |
| 逃跑不再"对着空气逃" | ✅ | 场上没东西可逃就不报价：45 秒内 `fleeLive(0 danger)` **0 行**（修前 23 行）；有真怪时仍逃（`(2 danger(s))`） |
| 看不见的客户端也能干活 | ✅ | 窗口在屏外 1px，准星仍随瞄准变化（渲染帧在跑） |
| 长跑稳定 | ✅ | 120 分钟：存活率 **1.0**、内存斜率 **0.12 MB/分**、`error`/`incident`/`death` 全 0 |
| 打包成 exe（没装 Python 也能跑） | ✅ | `Buddy.exe` 74.8 MB，`--selftest` OK，`--run-py` 通道可用 |
| 自检闸 | ✅ | **32 项全绿**（`tools/companion_gate.py`，约 45 秒；客户端没起时活体项记 SKIP，不算失败） |

**半通的 5 项（能用，但没第二次现场复验）**：护主清怪腿、启动器、控制台 v2、近战护主、作业期卡死检测。
完整能力表在 [`companion/capabilities.yaml`](companion/capabilities.yaml)（39 项：通 34 / 半通 5）。

---

## 它为什么长这样（三句话）

1. **手必须活在游戏里。** 用外部协议（Mineflayer 那类）模拟玩家，永远差一口气：挖方块的手感、
   碰撞箱、限速闸、界面焦点，全是客户端内部的语义。所以我们把"手"做进模组。
2. **脑子必须在外面。** 计划、语音、多人协调、失败归因，改起来要秒级迭代——放进 Java 里就等于
   每次改都要重启游戏。所以脑子是 Python，通过 MCP 调那 89 个工具。
3. **"能跑"和"跑对"必须分开测。** 这个项目踩过的最贵的坑，全是"接口回 ok、世界没变"
   （放置假成功、挖掘被原版取消、死亡界面关不掉、日志说自己在逃但场上没怪）。
   所以每个动作的判据都落在**世界状态**上，而不是返回值上。

---

## 快速开始

前置：Windows 10/11、Python 3.11+、JDK 21、一个 1.21.11 的 Fabric 客户端（或用我们的一键准备）。

```powershell
# 0) 依赖
python -m pip install -r requirements.txt

# 1) 起服务端（本地自建服；公服会被封号，见 docs/SECURITY.md）
python -X utf8 tools\server_warden.py ensure --port 25566 --wait-sec 300 --settle-sec 75

# 2) 起"身体"：一个看不见但能干的客户端，进服（⚠️ 一定要带 --server，默认端口不是 25566）
python -X utf8 tools\buddy_body.py restart --index 1 --offscreen --server 127.0.0.1:25566
python -X utf8 tools\buddy_body.py status  --index 1 --json

# 3) 起"脑子"：指挥层（语音/组队按需）
python -X utf8 -m companion.commander --port 25351 --player Xiaorun --seconds 3600 --voice

# 4) 开工前必跑：32 项自检（约 45 秒）
python -X utf8 tools\companion_gate.py
```

### 对着它说话

```
跟着我 / 过来 / 别跟了 / 挖这块 / 清怪 / 给我木头 / 要点吃的 / 你在哪 / 你能干什么 / 天黑了 / 回来
```

指令走规则解析（快、可预测），说不清的才交给模型（DeepSeek）——模型只做**建议**，
不直接驱动身体；这一步是刻意设计的：模型会幻觉，手不会。

---

## 目录

```
companion/     脑子：计划 / 语音 / 队伍 / 反射弧 / 任务层（Python）
launcher/      控制台：实例管理 / 放 jar / 启动参数 / 诊断包 / Qt UI
tools/         运行期工具：自检闸、起身体、长跑、发布、模型下载
config/        配置模板（真配置永不入仓）
java/          模组侧补丁（GPL-3.0，drop-in 到 UnionClef 0.95.63 的源码树）
docs/          快速开始 / 架构 / 安全 / FAQ
```

---

## 已知边界（写在这里，比写进 issue 有用）

- **公服不要用。** 多数服把自动操作当宏，会封号。只用单机、自建服、朋友服。
- **真人验收还没做。** 30 分钟真人陪玩 ×3、20 句语音实测，只有人能做。
- **骑乘 / 钓鱼 / 交易 / 附魔** 还没逐个验。
- Windows only（启动器依赖 Win32 的桌面与会话 API）。

---

## 许可

- 本项目自己的代码（`companion/` `launcher/` `tools/` `docs/`）：**Apache-2.0**，见 [LICENSE](LICENSE)。
- `java/` 下的补丁是 [UnionClef](https://github.com/)（AltoClef + Tungsten）的衍生作品，
  上游是 **GPL-3.0**，所以那部分补丁同样按 **GPL-3.0** 提供，见 [NOTICE](NOTICE)。
- 第三方清单与出处：[THIRD-PARTY.md](THIRD-PARTY.md)。

---

## 致谢

AltoClef / Baritone / Tungsten 的作者们把"怎么让方块人真的走路"这件事做到了极致；
我们只是在这副骨架上，给它装了一个会问"你需要我做什么"的脑子。
