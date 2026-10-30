# FAQ —— 十条最常见的"它坏了"

> 对应 `docs/HANDOFF-AUTONOMOUS.md` §5 W4-2 的验收口径：**任选 3 条，按文档能在 5 分钟内定位**。
> 全部命令在仓库根执行：`<repo>`（PowerShell，或 `python -X utf8 ...`）。
> 每条固定三段：**判据**（怎么确认是这个病）／**命令**（可直接复制）／**修复**（分步）。
> 路径、端口、默认值均取自实际文件：`launcher/core.py`、`tools/server_warden.py`、`tools/buddy_body.py`、
> `companion/commander.py`、`companion/voice_io.py`、`companion/voice_engine.py`、`companion/agent.py`、
> `config/voice.json`、`config/support.json`、`%MC_SERVERS%\paper-1.21.11\server.properties`。
>
> **动手前先跑这一句**（21 项，约 30 秒，`HANDOFF-AUTONOMOUS.md` 开头就要求）：
>
> ```powershell
> python -X utf8 tools\companion_gate.py
> ```
>
> 它绿了说明指挥层本身没坏，问题在环境/客户端侧；它挂了直接照 ❌ 那一行去修。

---

## 1. 进不去世界／一直卡在"正在加入服务器"

**判据**

- `tools\buddy_body.py status` 里 `mcp: true` 但 `in_game: false` —— MCP 活着，人还在菜单。
- 两个已知真因（原文都在 `tools/buddy_body.py:ensure_ingame()` 的注释里）：
  1. `ConnectToServer` 只是**置状态**，真正去连的是 AltoClef TaskRunner 里的 `gameMenuTaskChain`；
     TaskRunner 被 `@stop` disable 之后这条链根本不 tick ⇒ **"回 ok、什么也不发生"**（`@unpause` 解不了，它只解 pause）；
  2. 客户端被 kill 过 ⇒ 服务端留着**幽灵会话**，同名连接静默失败。
- 服务端侧：`tools\server_warden.py status` 里 25566 那行不是就绪状态。

**命令**

```powershell
python -X utf8 tools\server_warden.py status
python -X utf8 tools\buddy_body.py status --index 1 --server 127.0.0.1:25566
python -X utf8 tools\server_warden.py ensure --port 25566 --wait-sec 300 --settle-sec 75
```

**修复**

1. 先让服务端就绪：上面第三条（退出码 0 = 就绪；它内部会探活 → 起服 → 等 RCON `list`）。
2. 再让客户端真的进服：

   ```powershell
   python -X utf8 tools\buddy_body.py ensure --index 1 --server 127.0.0.1:25566
   ```

   它内部顺序是 `takeControl{on:false}`（= `TaskRunner.enable()`）→ `ConnectToServer` → 每 8 秒重试，直到 `inGame` 回 ok。
3. 还是不进行：`python -X utf8 tools\buddy_body.py restart --index 1 --server 127.0.0.1:25566`
   （杀进程 → 用 `--no-connect` 重拉 → 等 MCP → 进服）。**不要**用 quickPlay 直连进服，
   那会让 mod 半初始化（`HANDOFF-AUTONOMOUS.md` §0 铁律 1：SlotHandler NPE / MCP 不起）。
4. 幽灵会话：`ensure_ingame` 会自己尝试 `kick Buddy`；不生效就在服务端控制台手动 `kick Buddy`。
5. 端口别搞错：`tools\buddy_body.py` 的默认 `SERVER` 是 `127.0.0.1:25565`（环境变量 `BUDDY_SERVER`），
   而最终部署形态在 **25566** —— 一定显式带 `--server 127.0.0.1:25566`。

---

## 2. 它不吭声（打字有反应，但听不见它说话）

**判据**

- trace 里根本没有 `tts` 事件。
- 有 `voice_ready`，但 `tts` 字段是 `none`（`companion/voice_io.py` 里 `NullTTS.name = "none"`）。
- **最常见的一条**：指挥层没带 `--voice`。`companion/commander.py` 是
  `VoiceIO.auto(enabled=voice, ...)`，不给 `--voice` 整个语音层就是空实现（TTS/STT 都是 null）；
  `--voice` 的 help 原文：「开声音（TTS 朗读 / STT 预留）」。
- 有 `tts` 事件但没声音：输出设备是虚拟声卡（ToDesk 之类），播报会明显变慢甚至听不到
  （`docs/USER-QUICKSTART.md` §6 已列此坑）。

**命令**

```powershell
Select-String -Path runs\commander\*\trace.jsonl -Pattern '"ev": "voice_ready"|"ev": "voice_engine"|"ev": "tts"' | Select-Object -Last 10
Get-Content config\voice.json
Get-CimInstance Win32_SoundDevice | Select-Object Name,Status
```

**修复**

1. 起指挥层时带 `--voice`（其余参数照 `HANDOFF-AUTONOMOUS.md` §0 那行入口）：

   ```powershell
   python -X utf8 -m companion.commander --port 25351 --settings "%BUDDY_CLIENT%" --player Xiaorun --seconds 600 --hz 6 --server 127.0.0.1:25566 --voice
   ```

2. 看 `voice_ready.tts`：
   - `sapi` = 走 Windows SAPI（Huihui），正常；
   - `none` = 没接上 —— 同一行前后找 `voice_engine_missing` / `voice_engine_failed` 的 `err`。
3. 换输出设备成真扬声器/耳机（不要虚拟声卡）。SAPI 每句有约 **0.9 秒进程开销**
   （`docs/VOICE-STACK-2026-10-29.md` §3），这是引擎特性不是故障。
4. 调音：`config\voice.json` 的 `tts_rate`（默认 5）、`tts_volume`（默认 100）—— 改完**重启指挥层**生效。
5. 想要更好听/能插话（可选，不影响"能不能出声"，SAPI 永远兜底）：
   - `cosyvoice_dir` 指向 CosyVoice2 目录（`voice_engine.pick_tts()` 检测到目录存在才会选它）；
   - `volc_token` 填火山实时语音（支持插话）。
   - 两者都不配时 `pick_tts` 的 `why` 就是「默认零依赖 SAPI（中文 Huihui）；要更好听配 cosyvoice/volc」。

---

## 3. 它不回应语音（说话没反应，但打字/启动器输入框正常）

**判据**

- trace 里没有 `stt` 事件，只有 `stt_muted` —— 那是"它正在说话，麦克风被防自听关掉"
  （`companion/voice_io.py:VoiceIO.listen`：正在播报时最多阻塞 2 秒、记一条 `stt_muted`），不是故障。
- `voice_ready.stt` 不是 `sherpa`；或 `voice_engine{kind:"stt", engine:"sapi", why:"sherpa 模型目录不存在 → 兜底 SAPI"}`。
- 时序：说完要等 **0.6 秒静音**才出字（`silence_chunks: 6`）；纯静音会被 `_looks_like_speech()` 拦掉
  （防 SenseVoice 幻觉 `'그.'`，**别删这两道过滤**）。
- `input_device` 为空 = 系统默认设备；本机有 ToDesk 回环等 13 个输入设备（`docs/VOICE-STACK-2026-10-29.md`），选错会"听见自己"。

**命令**

```powershell
Get-Content config\voice.json
python -X utf8 tools\_voice_probe.py --seconds 3
python -X utf8 tools\_voice_probe.py --device ToDesk --seconds 3
Select-String -Path runs\commander\*\trace.jsonl -Pattern '"ev": "stt"|"ev": "stt_muted"|"ev": "voice_ready"' | Select-Object -Last 10
```

**修复**

1. 确认带了 `--voice`（见第 2 条）——不带的话 `listen()` 直接返回 `None`。
2. 配置对得上：`config\voice.json` 里 `stt` 应为 `sherpa`、`sherpa_model_dir` 应为 `%MODELS%
   该目录下必须有 `model.int8.onnx` 与 `tokens.txt`（本机实测 228.2MB + 0.3MB，加载 3.03 秒）。
3. 模型不在：`python -X utf8 tools\get_voice_model.py --guide`，照它打印的位置放好后重试；
   `launcher\core.py:model_status()` 用同一判据（找 `*.onnx` 且名字含 `sense`/`model.int8`）能判有没有。
4. 麦克风验不过：用 `--device <设备名片段>` 指定；`vad_threshold`（默认 0.012）太低会吃环境噪声、
   太高会听不见 —— 每次 0.005 调。
5. 嫌慢：`silence_chunks` 6 → 3（0.3 秒判定，**有截断风险**，`docs/VOICE-STACK-2026-10-29.md`「断句」一行已写明）。
6. 真想"边说边出字"：现在是 VAD 断句（非流式），要走流式得换 streaming zipformer 模型（W1-3，尚未落地）。

---

## 4. jar 不生效（客户端起来了，但没有任何陪玩工具）

**判据**

- MCP 25351 不通，或通了但工具数 **< 80**（闸 ㉑ `check_live()` 的断言就是 `len(names) >= 80`）。
- `launcher/core.py:_mods_dirs()` 写死了**两个目录都要放**：
  `<.minecraft>/versions/<版本>/mods` 和 `<.minecraft>/mods`；只放一个 = 版本隔离时看不见。
- `core.list_instances()` 里 `buddy_jar` 为空 = 那个版本压根没装。
- 客户端日志报 `WinError 32` = 换 jar 时旧 jar 还被运行中的客户端 mmap 住（`tools/install_jar_now.py` 为此存在）。

**命令**

```powershell
python -X utf8 tools\companion_gate.py
python -X utf8 -c "import sys,json; sys.path.insert(0,'launcher'); import core; [print(json.dumps(x,ensure_ascii=False)) for x in core.find_game_dirs()]"
python -X utf8 -c "import sys,json; sys.path.insert(0,'launcher'); import core; g=core.find_game_dirs()[0]['dir']; [print(json.dumps(x,ensure_ascii=False)) for x in core.list_instances(g)]"
python -X utf8 tools\_verify_jar.py "_refs\unionclef-0.95.63\versions\1.21.11\build\libs\unionclef-1.21.11-0.95.63-buddy9.jar"
```

**修复**

1. **先关客户端**（不然 WinError 32），再装：启动器「实例」页 →「安装 / 更新陪玩 jar」。
   等价内核调用是 `core.install_mod(game_dir, version)` —— 它同时写两个 mods 目录，并把旧 jar 备份成 `.bak`。
2. 装完 `list_instances` 必须能看到 `buddy_jar`；看不到说明游戏目录/版本选错了（第一条命令打印的就是候选清单）。
3. 起来后验两层：
   ```powershell
   python -X utf8 tools\buddy_body.py status --index 1 --server 127.0.0.1:25566
   python -X utf8 tools\companion_gate.py --require-live
   ```
   前者 `mcp: true`，后者让 ㉑ 必须绿（`--require-live` 时 25351 不在就判失败）。
4. 客户端**必须重启**才认新 jar（TaskRunner/MCP 在客户端初始化时建）。这也是"jar 换了没反应"的最高频原因。
5. 刚更新完才坏的：启动器「诊断」页「回滚陪玩 jar」= `core.rollback_jar(game_dir, version)`
   （取最新 `.bak` 还原，当前 jar 改名 `.prev`；没有 `.bak` 时返回 `why: "没有 .bak 可回滚"`）。
6. 想核对 jar 内容是不是我们那版：`tools\_verify_jar.py` 会查 `Py4jEntryPoint` / `McpServer` / `fabric.mod.json` 的
   `name == Buddy` / `Debug` 里 `BUDDY` 前缀。

---

## 5. 端口被占用（25351 / 25341 / 25566 / 25576）

**判据**

- `server_warden.py ensure` 打印：`[warden] :25566 端口已被占用但 RCON 不就绪——疑似僵尸进程，先人工处理`。
- 另一种"起不来"其实是内存闸，不是端口：`[warden] 可用内存 X.XGB < 阈值 4.5GB，拒绝起服（--min-free-gb 可覆盖）`
  （`tools/server_warden.py` 的 `DEFAULT_MIN_FREE_GB = 4.5`）。
- 端口分工（都可在文件里核对）：
  - 服务端 **25566**、RCON **25576**（`%MC_SERVERS%\paper-1.21.11\server.properties`，`rcon.password=buddy`）；
  - 陪玩 MCP **25351**、Python 网关 **25341**（`launcher/core.py:ensure_settings()` 默认值）；
  - 第 N 个陪玩 = `25350 + N`（`tools/buddy_body.py:use()`）。

**命令**

```powershell
Get-NetTCPConnection -LocalPort 25351,25341,25566,25576 -State Listen |
  Select-Object LocalPort, OwningProcess, @{n='Proc';e={(Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName}}
python -X utf8 tools\server_warden.py status
python -X utf8 -c "import sys; sys.path.insert(0,'launcher'); import core; print(core.diagnostics(r'runs\diag\now'))"
```

**修复**

1. 看上表 `Proc` 列认出占用者（多半是上次没退干净的 `java` 或 `python`），确认后结束它：`Stop-Process -Id <PID>`。
2. 服务端僵尸：**先停服再起**，不要直接再点一次启动（warden 会拒绝，理由就是上面那行）。
3. 内存闸触发：关掉别的吃内存程序；急用可 `--min-free-gb 4.0` 临时放行
   （代价见台账：堆被挤时会 GC 抖动，读数会变差）。
4. 换端口：
   - 服务端改 `server.properties` 的 `server-port` / `rcon.port`；
   - 陪玩侧改启动器「陪玩」页的 MCP 端口，或 `--mcp-port` / `--port 25351`。
   - 改完两侧都要重启，且 `--server` 要跟着改（默认还是 25565，见第 1 条第 5 点）。
5. 诊断包自带端口快照：`core.diagnostics()` 把 25566 / 25576 / 25351 三个端口的开闭写进 `env.json`。

---

## 6. 账号验证不过 / 正版登录

**判据**

- 本机这台服 `online-mode=false`（`%MC_SERVERS%\paper-1.21.11\server.properties`）⇒ **它并不校验 Mojang 账号**，
  所以"进不去"通常**不是**正版/盗版的问题。
- 真正的两种：
  1. 同文件 `enforce-secure-profile=true` —— 1.19+ 聊天签名，客户端会话无效会被踢；
  2. 你连的**不是这台服**（朋友服/公网服 `online-mode` 可能是 `true`）。
- 日志判据：`%MC_SERVERS%\paper-1.21.11\logs\latest.log` 里搜 `Failed to verify username` / `Invalid session` /
  `logged in with entity id`。

**命令**

```powershell
Select-String -LiteralPath "%MC_SERVERS%\paper-1.21.11\server.properties" -Pattern 'online-mode|enforce-secure-profile|white-list|server-port'
Get-Content "%MC_SERVERS%\paper-1.21.11\logs\latest.log" -Tail 100 | Select-String 'verify|session|logged in|Disconnect|moved'
```

**修复**

1. 先确认连的是本机：客户端「多人游戏」里应是 `127.0.0.1:25566`，不是历史地址。
2. 本机自建服保持 `online-mode=false` 即可；要真开正版校验就改 `true`，
   然后**玩家和陪玩客户端都必须登录正版账号**（陪玩是同一个客户端内核，一样要有效会话）。
3. 被 `enforce-secure-profile` 卡：临时改 `false`（会降低聊天防伪，自己权衡）。
4. 用户名：玩家侧 `--player Xiaorun`（默认），陪玩侧 `--name` 或启动器「陪玩」页；**重名会被踢**。
   `white-list=false`，不需要加白名单。
5. 换服先把地址指对：`BUDDY_SERVER` 或 `--server host:port`。忘了改会一直连 25565（`tools/buddy_body.py` 的默认值），
   现象很像"账号有问题"，其实是连错端口。

---

## 7. 黑屏 / 根本看不到窗口（默认看不见是设计）

**判据**

- **默认就是看不到，这是设计**：`launcher/core.py:launch_cmd()` 在 `hidden=True`（默认）时追加
  **`--offscreen`** —— 窗口留在**默认桌面**、被挪到**屏幕外只留 1 像素**。
  这样用户几乎看不见，而**渲染照常**（陪玩的瞄准/挖掘按帧推进，必须能渲染）。
- ⛔ **不要用 `--desktop Buddy` 跑陪玩**（2026-10-30 实测推翻）：独立桌面上的客户端
  **渲染主循环会停** —— 准星 `crosshairTarget` 冻住 ⇒ 挖矿/瞄准全废（闸㉓ 的反向证据）。
  它只留着复现那个坑。
- 想看它动：`hidden=False` 起一份（命令里既没有 `--offscreen` 也没有 `--desktop`）。

**命令**

```powershell
python -X utf8 -c "import sys; sys.path.insert(0,'launcher'); import core; g=core.find_game_dirs()[0]['dir']; v=core.list_instances(g)[0]['version']; print(' '.join(core.launch_cmd(g, v, 'Buddy', connect='127.0.0.1:25566', hidden=False)))"
python -X utf8 launcher\ui\main.py --selftest
python -X utf8 tools\_hidden_desktop_check.py --port 25353 --desktop Default --expect-offscreen   # 屏外 + 仍在渲染
```

**修复**

1. 想看它在干嘛：启动器「陪玩」页点 **「调试：可见启动」**（= `launch_cmd(..., hidden=False)`，
   打印出来的命令里没有 `--desktop`）。上面第一条命令就是打印这条命令，可复制执行。
2. 平时别点可见：保持默认隐藏，它就不会抢你焦点。
3. 隐藏桌面里真黑屏（W6-3 的兜底）：改用 `launcher/win32_desktop.py:move_offscreen(hwnd, keep_px=1)`
   把窗口挪到屏幕外只留 1px（避开 DWM 节流），然后用判据验证渲染：
   **让它挖一块土，看方块是否变空气**（`mineStatus` / 世界状态），不要靠"窗口看起来正常"。
4. 窗口秒退（进程起来了但立刻没了）：看 `%BUDDY_CLIENT%
   （`core.diagnostics()` 抓的就是这个，`client_tail.log` 200 行），
   或前台跑一次看报错：

   ```powershell
   python -X utf8 tools\_mc121_launch.py --name Buddy --workdir "%MC_HOME%" --mcp-port 25351 --host 127.0.0.1 --port 25566 --wait
   ```

   （`--workdir` 用第 1 条命令里 `find_game_dirs()` 打印的真实路径替换。）
5. 自检不过先修自检：`python -X utf8 launcher\ui\main.py --selftest` 应为 OK；
   `python -X utf8 launcher\core.py` 应有 18 项全 ✅（含「隐藏桌面助手可用」「默认启动=隐藏桌面」）。

---

## 8. 准星死 / 让它挖它不动（`@stop` 把 TaskRunner 弄 disable 了）

**判据**

- 聊天反复出现 `Mining aborted … progress=-0.10`；trace 里 `mining_aborted_seen{n:≥2}`。
- `pathStatus.busy` 一直 true（导航腿没清干净，`companion/actions.py:path_busy()`）。
- 发什么都回 ok、什么都不发生 —— TaskRunner 被 disable 的典型特征
  （`tools/buddy_body.py` 注释原文：`@stop` 之后 TaskRunner 被 disable，连 `ConnectToServer` 都停）。
- 已有自愈阶梯（`companion/commander.py` 的 `body_stall` 1~5 级）：第 5 级判据就是
  `mining_aborted_seen ≥ 2` 且冷却已过 ⇒ `body_restart`（换身体）。

**命令**

```powershell
Select-String -Path runs\commander\*\trace.jsonl -Pattern 'body_stall|mining_aborted_seen|body_restart|unstick' | Select-Object -Last 15
python -X utf8 -c "from companion.body import Body; b=Body(25351, r'%BUDDY_CLIENT%'); print(b.call('pathStatus', {}, 8)); print(b.call('takeControl', {'on': False}, 12))"
```

**修复**

1. 别把 `@stop` 当"取消当前动作"用（`HANDOFF-AUTONOMOUS.md` §10）。
   正解：`takeControl(true)` 抢过控制权 → 干完 → `takeControl(false)` 还给它。
2. 已经被 disable：上面第二条命令里的 `takeControl {on:false}` = **`TaskRunner.enable()`**，
   之后 `ConnectToServer` 才会重新 tick。要连服就直接 `python -X utf8 tools\buddy_body.py ensure --index 1 --server 127.0.0.1:25566`（它把这两步串好了）。
3. 导航/瞄准在抢：跑**完整清理序列** `stopPathing → @stop → @idle → stopPathing`，每次读回 `pathStatus.busy`
   来判断是否真的清了（`companion/actions.py:stop_all()`）。**只发一条 `@stop` 不够**——
   TaskRunner 会被它自己的"卡死自救/shimmy"重新武装，导航腿一旦在跑，挖矿就把瞄准让给它。
4. `Mining aborted` ≥2 次：先试挪一格（`actions.unstick`，针对"站在别人身体里/贴着怪"）；
   还不行只能换身体（`companion/body_ctl.py:restart()`，`commander` 在未开 `--no-restart` 时自动做）。
5. 一次只允许一个瞄准者：别在 `mineBlock` 期间又发 `@mineblock`（§0 铁律 4）。

---

## 9. key 无效（BYO key 配错）

**判据**

- trace 里 `ai_done` 的 `ok:false`，或 `ai_preplan_err`；而 `--planner rule` 一切正常 ⇒ 只有模型侧坏。
- 配置优先级（`companion/agent.py` 头注释）：
  环境变量 `BUDDY_AI_KEY` / `DEEPSEEK_API_KEY`、`BUDDY_AI_BASE`、`BUDDY_AI_MODEL` → 盖过 `config/ai.json`；
  默认 `base_url = https://api.deepseek.com`、`model = deepseek-flash`。
- `agent.py` 只对 **429/500/502/503/504** 重试；401/403 不重试 = key/权限本身不对。

**命令**

```powershell
python -X utf8 -c "import json;d=json.load(open('config/ai.json',encoding='utf-8'));k=d.get('api_key') or '';print({'key_tail':k[-4:],'len':len(k),'base_url':d.get('base_url'),'model':d.get('model'),'budget':d.get('daily_token_budget')})"
Select-String -Path runs\commander\*\trace.jsonl -Pattern 'ai_done|ai_preplan_err|ai_call' | Select-Object -Last 10
```

**修复**

1. 只看尾 4 位，别把 key 打全（产品承诺：`docs/COMPLIANCE.md` §4 与 `NOTICE` 都写明 BYO key 不进日志/诊断包）。
2. 填对位置：`config\ai.json` 的 `api_key`（以 `sk-` 开头）；
   `base_url` **不要**带 `/chat/completions`（代码自己拼 `POST /chat/completions`）。
3. 环境变量会盖掉文件里的值 —— 先清掉再试（改完重启指挥层）：

   ```powershell
   Remove-Item Env:BUDDY_AI_KEY -ErrorAction SilentlyContinue
   Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
   ```

4. 换了 key 还不行：单跑一次看 `ai_done.err`：
   ```powershell
   python -X utf8 -m companion.commander --port 25351 --settings "%BUDDY_CLIENT%" --seconds 60 --hz 6 --server 127.0.0.1:25566 --planner ai --ai-model deepseek-flash
   ```
5. 兜底能玩：`--no-ai` 或 `--planner rule`（纯规则执行器，不花 token，能力打折但照样动手）。

---

## 10. 额度用完 / 被限额降级

**判据**

- trace 里出现 `ai_budget_exhausted{used,budget}`，之后每条都是 `ai_budget_exhausted_skip`；
  它还会说一句「今天额度用完了」（`companion/ai_layer.py` 头注释原文）。
- 默认额度 `daily_token_budget = 400000`（`config\ai.json`；`docs/USER-QUICKSTART.md` §2 说"约 100~150 次对话"）。
- 按**自然日**归零：日志里能看到 `ai_budget_reset{day,budget}`。
- 与"上游限流"区分：`ai_done.err` 里 429 = 服务商限流（重试 2 次后放弃），不是本地预算。

**命令**

```powershell
python -X utf8 -c "import json;d=json.load(open('config/ai.json',encoding='utf-8'));print('budget=',d.get('daily_token_budget'))"
Select-String -Path runs\commander\*\trace.jsonl -Pattern 'ai_budget|ai_budget_reset' | Select-Object -Last 12
```

**修复**

1. 提额：改 `config\ai.json` 的 `daily_token_budget`（或面板「AI」页保存），
   按"一次对话约 4000 token"估算要加多少。
2. 省着用：
   - `--planner rule`（完全不动模型）；
   - `--no-ai`（关掉 AI 层）；
   - `--ai-every` 调大（默认 1.0，AI 事件喂模型的节流间隔）。
3. **降级是设计行为，不是 bug**：超预算后仍会动手、仍会回一句，只是不再"想"
   （`ai_layer.py`：「超了就降级成确定性执行器」）。看到"只说不动脑"不要当故障修。
4. 真被服务商限流（429）：等冷却或换 key；本地调 `daily_token_budget` 对它没用。
5. 上报前先自查：诊断包 `core.bundle()` 只收 `trace_tail.jsonl` / `gate.json` / `last_plan.json` /
   `client_tail.log` / `env.json`（`config\support.json` 的 `include` 列表同口径），
   **不含 key、不含 `ai.json`**；`enabled=false` 时 `upload_diag()` 直接返回「未开启或没配 endpoint（本地留存）」。

---

## 11. 客户端起来了，但陪玩"没反应"（界面停在加载/主菜单，MCP 端口不通）

**怎么判断**：客户端窗口在、进程在，但 `python -X utf8 tools\buddy_body.py status` 报
`mcp: false`（`urlopen error 10061 目标计算机积极拒绝`），日志最后一行停在
`Created: … shulker_boxes.png-atlas`。

**最可能的原因**：MC 1.20+ 首次启动会先弹**无障碍引导页**（Accessibility Onboarding）。
我们的模组是在**主菜单初始化时**（`TitleScreen.init()`）才打印 `BUDDY: Global Init` 并启动 MCP 的，
所以引导页一出现，MCP 永远起不来 —— 而且**一句报错都没有**。

**修**（`options.txt` 里这一行必须是 `false`，`…\versions\<版本>\options.txt`）：

```text
onboardAccessibility:false
```

启动器的「一键准备」第 ③ 步会自动写这一行（`launcher/core.py` 的 `tune_client()`）。
手工确认：

```powershell
Select-String -Path "<你的 .minecraft>\versions\<版本>\options.txt" -Pattern onboardAccessibility
```

**其它同类症状**（同一份判据：`mcp: false` + 没有 `BUDDY: Global Init`）：

- 客户端停在**主菜单但 MCP 端口被占**：`Get-NetTCPConnection -LocalPort 25351`，占用就改 `--mcp-port`；
- 客户端根本没进主菜单：看 `…\logs\latest.log` 最后一行，"Creating pipeline for dimension" 之前就停 ⇒ GL/驱动；
- 用 `--desktop`（独立桌面）起的客户端**能进游戏但干不了活**（准星不动）——这条是已知结论，
  改用默认的 `--offscreen`（屏外留 1px）。

---

> 三条都定位不了？按顺序做这三件，基本能收敛到具体模块：
>
> ```powershell
> python -X utf8 tools\companion_gate.py                 # 指挥层 21 项
> python -X utf8 launcher\core.py                        # 启动器内核 18 项
> python -X utf8 -c "import sys;sys.path.insert(0,'launcher');import core;print(core.bundle(r'runs\diag\now'))"   # 一键诊断 zip
> ```
>
> 然后把 zip 发出来。清单与承诺见 `config\support.json`、`docs/COMPLIANCE.md` §6。
