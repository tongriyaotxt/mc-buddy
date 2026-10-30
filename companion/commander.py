#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/commander.py —— 指挥层主循环：观察 → 决策 → 过闸 → 执行 → 记账

这一层**不含任何 Minecraft 知识**（那是 planner 的事），也**不含任何物理**（那是身体的）。
它只保证五件事：

  1. **一 tick 一件事**：决策层可以给一堆意图，指挥层只挑优先级最高的那一个去执行。
  2. **一次只有一个作业**：挖矿/取物这类"长活"跑在单线程池里，跑完才规划下一个，
     否则两个 AltoClef 任务会互抢准星（PathExecutor:853 executorYieldsAimToMiner）。
  3. **界面守护**：GUI 开着时 tungsten 的 WindMouse 每帧直接 return ⇒ 准星死 ⇒ 挖不动。
     每个 tick 先确认没有界面开着（2026-10-26 实证：这是"挖矿永远挖不动"的真因）。
  4. **护栏在意图与执行之间**：换 planner（规则 → AI）不会让护栏失效。
  5. **可回放的账本**：每个 tick 的 obs / intent / verdict / result 全落 `trace.jsonl`。

身体出问题时走降级阶梯：重发 → `@goto` → 关界面 → **重启客户端**（实测约 40~60 秒可恢复）。

用法：
  python -X utf8 -m companion.commander --settings <path> [--seconds 60] [--hz 3]
  python -X utf8 -m companion.commander --settings <path> --planner hybrid \
      --ai-endpoint http://127.0.0.1:11434/v1/chat/completions --ai-model qwen2.5:7b
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):                      # 支持 `python companion/commander.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion import actions, hook as hook_layer        # noqa: E402
from companion import tasks as tasks_layer                # noqa: E402
from companion.agent import Agent                         # noqa: E402
from companion.body import Body, as_obj, truthy          # noqa: E402
from companion.bus import BUS, Bus                        # noqa: E402
from companion.chat_in import ChatIn                      # noqa: E402
from companion.guards import Guards                       # noqa: E402
from companion.memory import Memory                       # noqa: E402
from companion.planner import (AIPlanner, HybridPlanner,   # noqa: E402
                               Intent, Requests, RulePlanner, ray_block, ray_block_batch)
from companion.registry import Registry                   # noqa: E402
from companion.speech import Speech                       # noqa: E402
from companion.voice_io import VoiceIO                    # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

FAR_AT = 20.0        # 超过这个距离用 gotoFar（gotoXYZ 会对远目标冻住）
STILL_S = 6.0        # 下发导航后位移 <0.5 格持续这么久 → 判定卡住
STALL_S = 6.0        # busy=true 但距离不缩短这么久 → 重算
RESTART_COOLDOWN = 180.0
JOB_BUDGET = 240.0   # 单个长活的硬预算：超过就取消（不然队列会被一个卡死的活锁住整晚）

# 这些动作**不需要看到玩家**就能做完（看不到人也要执行，否则请求会饿死在队列里）
PLAYER_FREE = {"macro.say", "macro.ask", "macro.get", "macro.mine", "macro.build",
               "macro.selfcare", "macro.hero"}

# 快车道：作业跑着的时候也能做（不碰准星、不抢寻路）
FAST_KINDS = frozenset({"say", "ask", "stop"})

# 角色分工（2026-10-27 晚：以前 role 只写进黑板、不影响任何决策 ⇒ 现在它是真闸）
#   playmate 全能；guard 只护主不采集；gatherer 采集优先；builder 盖房优先；scout 只跟随探路
ROLE_ALLOW = {
    "playmate": None,                      # None = 不限制
    "guard": {"hero", "supply", "follow", "stop", "say", "ask", "teleport", "free", "selfcare"},
    "gatherer": {"get", "mine", "supply", "follow", "hero", "stop", "say", "ask",
                 "teleport", "free", "selfcare"},
    "builder": {"build", "get", "supply", "follow", "hero", "stop", "say", "ask",
                "teleport", "free", "selfcare"},
    "scout": {"follow", "stop", "say", "ask", "teleport", "free"},
}


class Commander:
    def __init__(self, body: Body, planner, *, player: str, outdir: Path,
                 dry: bool = False, verbose: bool = True, clean_start: bool = True,
                 allow_restart: bool = True, allow_chat: bool = True,
                 allow_teleport: bool = True, voice: bool = False,
                 bus: Bus | None = None, engagement: str = "normal",
                 requests_path: str = "", bot_name: str = "Buddy",
                 ai_endpoint: str = "", ai_model: str = "",
                 team_path: str = "", role: str = "playmate", ai_layer: bool = True,
                 voice_hub: str = "", body_lock: bool = True):
        self.body = body
        self.reg = Registry(body, dry=dry)
        self.guards = Guards()
        self.planner = planner
        self.player = player
        self.bot_name = bot_name
        # 2026-10-30：作业期间把身体从 AltoClef 的**竞价系统**里拿过来（见 actions.body_lock）
        self.allow_body_lock = bool(body_lock)
        self._locked = False
        self._locked_until = 0.0
        self._lock_why = ""
        self.role = role or "playmate"
        self.trace = outdir / "trace.jsonl"
        self.dry = dry
        self.verbose = verbose
        self.allow_restart = allow_restart
        self.allow_teleport = allow_teleport

        self.memory: dict = {}
        self.tick = 0
        self.actions = 0
        self.last_pos = None
        self.still_since = None
        self.nav_fail = 0
        self.hero_cd = 0.0
        self.stats: dict[str, int] = {}
        self._last_veto = ""
        self._last_veto_at = 0.0
        self.body_healthy = True
        self.screen_ok_at = 0.0
        self.screen_open = False
        self.restart_at = 0.0
        self.restarts = 0
        self.results: list[dict] = []
        self.session_t0 = time.time()
        self.greeted = False
        self._mem_saved = 0.0
        # 玩家运动状态（礼仪闸 ⑤ 与 P1-1 玩家状态机的第一版：先有"在不在动"，mining 待接 swing 通道）
        self._pp_prev = None
        self._pp_prev_at = 0.0
        self._pp_speed = 0.0
        self._et_at = 0.0
        # 两级观察的缓存（2026-10-29 反射弧改造）：便宜的 tick 只读 getGameState
        self._cached_det: dict = {}
        self._cached_nav: dict = {}
        self._cached_threat: dict = {}
        self._cached_park = None
        self._batch_ok = True          # getBlocksBatch 可用？（老 jar 会把它关掉）
        self._cached_aim = None
        self._cached_park_pp = None
        self._cached_park_at = 0.0
        self._tick_i = 0
        self._full_every = 3
        self._aim_key = None
        self._aim_at = 0.0
        self._obs_ms = 0.0
        self._slow_n = 0
        self._full_skip_until = 0.0
        self._act_at = 0.0
        self._idle_at = 0.0
        self._ack_at = 0.0
        self._guard_at = 0.0
        self._hurt_at = 0.0
        self._look_at = 0.0

        # ── 多陪玩协作（2026-10-27 新增）：黑板 + 认领 + 编队 + 说话权 ──────────
        from companion.team import Team
        self.team = Team(team_path or None, me=bot_name, role=role, log=self.log,
                         enabled=bool(team_path))
        self._claim_keys: set[str] = set()
        self._beat_at = 0.0

        # ── 2026-10-26 夜新增：人怎么跟陪玩说话、陪玩怎么记住这个人 ──────────
        # 2026-10-27：`bus or BUS` 是个陷阱 —— Bus 定义了 __len__，空队列时它是 **falsy**，
        # 于是传进来的空 Bus 会被悄悄换成全局 BUS（面板回执写到别处去了）。
        self.bus = bus if bus is not None else BUS
        self.file_req = Requests(Path(requests_path) if requests_path else
                                 ROOT / "runs" / "requests.jsonl")
        self.persona = Memory(player)
        self.engagement = engagement
        tone = self.persona.p.style
        gap = {"quiet": 60.0, "normal": 8.0, "clingy": 2.5}.get(engagement, 8.0)
        self.speech = Speech(body if not dry else None, enabled=allow_chat,
                             tone=tone, min_gap=gap, log=self.log,
                             gate=(lambda: self.team.may_speak()) if team_path else None)
        self.voice = VoiceIO.auto(enabled=voice, log=self.log)
        # 多实例共享一路麦克风（W3-2）：hub 进程独占模型与麦克风，这里只读它的 inbox。
        # ⚠️ 必须在 `VoiceIO.auto` 之后替换 stt，但**不能**再让本进程开麦（换掉就不会开）。
        if voice_hub:
            try:
                from companion.voice_hub import HubSTT
                self.voice.stt = HubSTT(voice_hub, name=bot_name, log=self.log)
                self.log({"ev": "voice_stt_hub", "path": str(voice_hub),
                          "me": bot_name})
            except Exception as e:
                self.log({"ev": "voice_hub_failed",
                          "err": f"{type(e).__name__}: {e}"[:160]})
        # ── AI 交互层（2026-10-29 operator：不用行为互动，全部走 AI）──────────
        # 指挥层从此只做"执行 + 安全联锁 + 记账"；"做什么/说什么/没事干什么"都由模型决定。
        from companion.agent import Agent
        from companion.ai_layer import AiLayer
        self.ai_agent = None
        try:
            if ai_layer:
                self.ai_agent = Agent(self.reg, base_url=ai_endpoint, model=ai_model,
                                      memory=self.persona, speech=self.speech, log=self.log,
                                      bot_name=bot_name, runner=self._ai_runner,
                                      obs_fn=lambda: getattr(self, "_last_obs", None),
                                      outdir=outdir)
        except Exception as e:
            self.log({"ev": "ai_init_failed", "err": f"{type(e).__name__}: {e}"[:160]})
        self.ai = AiLayer(self.ai_agent, log=self.log, planner=self._ai_planner) \
            if self.ai_agent else None
        if voice:
            self.speech.voice = self.voice
            # 麦克风 → 总线。STT 引擎自己会切句（SAPI 的 EndSilenceTimeout 就是 VAD），
            # 识别出一句就当成一次 "free" 请求：和打字走同一条总线、同一套护栏。
            # ⚠️ 没有 STT 实现时**绝不能起这个线程**：空实现的 listen() 立刻返回 None，
            #    循环会变成 100% CPU 的忙等。
            if self.voice.stt.name != "none":
                self._voice_stop = threading.Event()
                threading.Thread(target=self._voice_loop, daemon=True,
                                 name="voice-in").start()
            else:
                self.log({"ev": "voice_in_off", "why": "没有可用的 STT（没麦或引擎没起来）"})
        self.chat_in = ChatIn(self.reg, self.bus, player=player, me=bot_name,
                              log=self.log,
                              gate=(lambda: self.team.may_speak()) if team_path else None)

        self.pool = ThreadPoolExecutor(max_workers=1)
        self.job: tuple[str, object] | None = None        # 作业取消旗（threading.Event）：`stop` 请求 / 作业超预算 时置位，
        # 长活内部的 goto / break_queue / wait_goal 都会在 0.3~1.2 秒内看见并收手。
        self.cancel_ev = threading.Event()
        self.job_started_at = 0.0
        self.job_budget = JOB_BUDGET
        self.job_start_pos = None
        self._job_task_sig = ""       # 任务描述指纹（60 秒一字未变 = 真没进展）
        self._job_task_at = 0.0
        self.jobs_cancelled = 0

        # 身体掉线（MCP 不通）的观测与自愈：只报一次 + 指数退避重连，不再每 tick 刷一行
        self.no_body_since = 0.0
        self.no_body_n = 0
        self.no_body_logged_at = 0.0
        self.ensure_at = 0.0
        self.ensure_tries = 0

        # 自然语言 → agent（DeepSeek）。没配密钥/没端点时它自己降级到 NLU+规则，
        # 仍然做动作、仍然回一句话 —— 不会静默消失。
        self.ai_endpoint = ai_endpoint
        self.ai_model = ai_model
        self._last_obs: dict = {}
        self.agent = Agent(self.reg, endpoint=ai_endpoint, model=ai_model,
                           memory=self.persona, speech=self.speech, log=self.log,
                           runner=self.run_macro_inline, obs_fn=lambda: self._last_obs,
                           bot_name=self.bot_name,
                           outdir=ROOT / "runs" / "agent" / outdir.name)

        if clean_start and not dry:
            # 清掉身体里残留的自主任务（实测：不清的话 gotoXYZ 会被它自己的觅食链压住）
            self.reg.invoke("run_command", {"cmd": "@stop"})
            self.reg.invoke("stopPathing", {})

    # ── 记账 ────────────────────────────────────────────────────────────────
    def log(self, obj: dict) -> None:
        obj["t"] = round(time.time(), 3)
        obj["ts"] = datetime.now().strftime("%H:%M:%S")
        with self.trace.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        if self.verbose and obj.get("ev") not in ("tick", "job_tick"):
            print(json.dumps(obj, ensure_ascii=False), flush=True)

    # ── 观察 ────────────────────────────────────────────────────────────────
    def _probe(self, x, y, z) -> dict:
        """一次 getBlockAt，顺手把 `_air` 算好（旧版为它多跑了两趟 RPC）。"""
        b = self.reg.block(x, y, z)
        if "is_air" in b:
            b["_air"] = truthy(b.get("is_air"))
        else:
            b["_air"] = str(b.get("id") or "").split(":")[-1] in ("air", "cave_air", "void_air")
        return b

    def _fetch_blocks(self, voxels) -> dict:
        """一批体素**一次**问完（`getBlocksBatch`，≤64 坐标/次）。

        返回值 `{ "x,y,z": "minecraft:stone" }`。老 jar 没有这个工具 ⇒ 关掉 `_batch_ok`，
        调用方自动退回逐体素 `getBlockAt`（宁可慢，不能瞎）。
        """
        out: dict = {}
        if not self._batch_ok:
            return out
        for i in range(0, len(voxels), 60):
            chunk = voxels[i:i + 60]
            keys = [f"{x},{y},{z}" for (x, y, z) in chunk]
            try:
                r = self.reg.invoke("getBlocksBatch", {"coords": ";".join(keys)})
            except Exception:
                self._batch_ok = False
                return {}
            if not r.ok or not isinstance(r.data, dict):
                self._batch_ok = False
                return {}
            out.update(r.data)
        return out

    def observe(self, fast: bool = False) -> dict | None:
        """一次观察。`fast=True` 只发 **1 个** RPC（getGameState），用缓存的 detail/threat/nav。

        为什么（2026-10-29 operator：「反射弧也太长了」）：
          旧版每个 tick 固定发 4 个 RPC：getGameState + playerDetail + pathStatus + getEntities，
          实测单次 30~60ms ⇒ 光"看"就吃掉 120~240ms；再叠加 3Hz 的节拍，
          玩家一个眼神要等 0.3~0.6 秒才被"看见"，加上一次只放一个意图，手感就是"迟钝"。
          现在：便宜的 tick 只读 1 次状态（~30ms），全量感知每 N 个 tick 做一次。
        """
        st = self.reg.invoke("getGameState", {})
        if not st.ok or not isinstance(st.data, dict):
            return None
        me = st.data.get("self") or {}
        mp = self.reg.pos(me)
        pl = next((p for p in (st.data.get("players") or [])
                   if str(p.get("name", "")).lower() == self.player.lower()), None)
        pp = self.reg.pos(pl) if pl else None

        if fast:
            det = self._cached_det
            navd = self._cached_nav
            threat = self._cached_threat
        else:
            # 2026-10-29 (W6-4)：全量感知 **4 次往返 → 2 次**。
            #   旧版 = getGameState + playerDetail + pathStatus + getEntities；
            #   新版 = getGameState + **stateDigest**（一次带回 self/around/target/player/
            #          entities/path，后三样从它里面切）。
            # getGameState 仍要发：它带 `players` 全表与 self 的 yaw/lookingAt，
            # 而 stateDigest 的 `player` 只有"最近的那一个"。
            dg = {}
            dd = self.reg.invoke("stateDigest", {})
            if dd.ok and isinstance(dd.data, dict) and dd.data.get("inGame"):
                dg = dd.data
            if dg:
                p = dg.get("player") or {}
                det = {}
                if p and str(p.get("name", "")).lower() == self.player.lower():
                    det = p                       # 已含 held / sneaking / yaw / pitch / hp
                    if pp is None:
                        pp = self.reg.pos(p)
                elif pl:
                    # 玩家身边还有更近的别人：宁可多一次往返，也要拿到**这个玩家**的朝向
                    d2 = self.reg.invoke("playerDetail", {"player": self.player})
                    if d2.ok:
                        det = d2.data if isinstance(d2.data, dict) else as_obj(d2.data)
                navd = dg.get("path") or {}
                threat = self.threat_from(dg.get("entities") or [], pp)
            else:
                # 老 jar（没有 stateDigest）：原样走旧路 —— 缺工具不等于瞎
                det = {}
                if pl:
                    d = self.reg.invoke("playerDetail", {"player": self.player})
                    if d.ok:
                        det = d.data if isinstance(d.data, dict) else as_obj(d.data)
                nav = self.reg.invoke("pathStatus", {})
                navd = nav.data if nav.ok and isinstance(nav.data, dict) else {}
                threat = self.threat(pp)
            self._cached_det, self._cached_nav, self._cached_threat = det, navd, threat

        obs = {
            "me": me, "pl": pl, "detail": det,
            "self_pos": mp, "player_pos": pp,
            "nav": navd,
            "dist": math.dist(mp, pp) if (mp and pp) else None,
            # ⚠️ 2026-10-29：旧版这里是 `{**block(), "_air": is_air()}` —— 每个体素 **3 次 RPC**
            # （block 两次 + is_air 再 block 一次）。一条 10 格射线 = 最多 28 个体素 ≈ 84 次往返，
            # 实测把"看一眼"拖到 200~300ms。现在一次 getBlockAt 就够（它本来就把 is_air 带回来了）。
            "probe": self._probe,
        }
        # 停靠点很贵（要探一圈能不能站），只在"可能要跟随"时算，并且缓存
        need_park = (obs.get("dist") is not None and float(obs["dist"]) > RulePlanner.FOLLOW_AT)
        moved = (self._cached_park_pp is None or pp is None
                 or math.dist(pp, self._cached_park_pp) > 0.75
                 or time.time() - self._cached_park_at > 2.0)
        if mp and pp and not fast and need_park and moved:
            obs["park_point"] = self.park_point(mp, pp, 3.0)
            self._cached_park, self._cached_park_pp = obs["park_point"], pp
            self._cached_park_at = time.time()
        elif self._cached_park:
            obs["park_point"] = self._cached_park
        # 威胁感知（2026-10-27：补 jar 之后才有 getEntities）
        obs["threat"] = threat

        # ── 玩家状态（P1-1 第一版）──────────────────────────────────────────
        # 位移速度可靠（坐标差分）；mining 需要挥臂动画通道，先如实留 False，
        # 等拿到挥臂字段再填（tools/_player_anim_probe.py 就是为此写的）。
        if pp:
            nowp = time.time()
            if self._pp_prev is not None:
                dt = max(1e-3, nowp - self._pp_prev_at)
                inst = math.dist(pp, self._pp_prev) / dt
                self._pp_speed = 0.7 * self._pp_speed + 0.3 * inst
            self._pp_prev, self._pp_prev_at = pp, nowp
        obs["player_state"] = {
            "speed": round(self._pp_speed, 2),
            "moving": self._pp_speed > 0.4,
            "sprinting": self._pp_speed > 2.2,
            "sneaking": truthy(det.get("sneaking")),
            "held": det.get("held"),
            "mining": False,
        }
        # 玩家视线打到的方块：礼仪闸"不抢你正对着的那块"与看哪挖哪共用同一条射线
        # 2026-10-29：射线原本是**逐体素 RPC**（一条 10 格射线 10~28 次），是全链路最贵的一步。
        # 现在整条射线的体素**一次** `getBlocksBatch` 问完（1 次往返）；老 jar 自动退回逐体素。
        # 另外只在"玩家的头真的转了"（>2°）或超过 0.8 秒才重算 —— 站着不动时没必要重算。
        aim_key = None
        if pp and det.get("yaw") is not None and det.get("pitch") is not None:
            aim_key = (round(float(det["yaw"]), 1), round(float(det["pitch"]), 1))
        if aim_key and (self._aim_key != aim_key or time.time() - self._aim_at > 0.8):
            eye = (pp[0], pp[1] + 1.62, pp[2])
            aim = None
            try:
                aim = ray_block_batch(eye, float(det["yaw"]), float(det["pitch"]),
                                      self._fetch_blocks)
            except Exception:
                aim = None
            if aim is None and not self._batch_ok:
                aim = ray_block(eye, float(det["yaw"]), float(det["pitch"]), obs["probe"])
            obs["player_aim"] = aim
            self._aim_key, self._aim_at = aim_key, time.time()
            self._cached_aim = obs.get("player_aim")
        elif self._cached_aim:
            obs["player_aim"] = self._cached_aim
        if obs.get("park_point"):
            self._cached_park = obs["park_point"]
        return obs

    def reflex(self, obs: dict, now: float) -> bool:
        """反射弧：每 tick 都跑，**作业期间也跑**（旧版作业期间直接 return，等于没反射）。

        分三档，按"该不该打断手上的活"：
          ① 护主（怪贴身）—— 唯一允许抢占：取消当前作业，立刻上；
          ② 应答（你在看我）—— 只动准星/手势，不碰寻路，所以不打断；
          ③ 眼神（你在看一块方块）—— 排队，不打断当前作业。
        返回 True 表示本 tick 已经被反射吃掉（上层不要再派活）。
        """
        if self.dry or not obs:
            return False
        th = obs.get("threat") or {}
        det = obs.get("detail") or {}
        pp, mp, d = obs.get("player_pos"), obs.get("self_pos"), obs.get("dist")

        # ── AI 优先：反射只"上报事件"，做什么由模型决定 ──────────────────────
        if self.ai is not None and self.ai.enabled():
            # ① 威胁/血量 → 交给模型判断"打/躲/贴身/说话"
            if th.get("near_player") and now - self._guard_at > 20.0:
                self._guard_at = now
                self.log({"ev": "reflex", "kind": "threat", "dist": th.get("dist"),
                          "type": th.get("type"), "preempt": self.job_running()})
                if self.job_running():
                    self.cancel_job("AI: 玩家身边有怪")
                self.ai.push("threat", n=int(th.get("count") or 1),
                             kind=str(th.get("type") or "?"),
                             dist=round(float(th.get("dist") or 0.0), 1))
                return True
            ph = (obs.get("pl") or {}).get("hp")
            if ph is not None and float(ph) < 8.0 and now - self._hurt_at > 20.0:
                self._hurt_at = now
                self.ai.push("hurt", hp=round(float(ph), 1),
                             n=int(th.get("count") or 0))
            # ② 应答（你看我）与 ③ 眼神（你指块）也交给模型，但**不抢占作业**
            if (pp and mp and d is not None and d <= 8.0 and det.get("yaw") is not None
                    and now - self._ack_at > 8.0):
                want = math.degrees(math.atan2(-(mp[0] - pp[0]), mp[2] - pp[2]))
                if abs((float(det["yaw"]) - want + 180.0) % 360.0 - 180.0) < 12.0:
                    self._ack_at = now
                    self.ai.push("say_text", text="（玩家正看着我）")
            tgt = obs.get("player_aim")
            if tgt and d is not None and d < 14.0 and now - self._look_at > 6.0:
                self._look_at = now
                self.ai.push("gaze", block=str(tgt[3]), pos=[int(tgt[0]), int(tgt[1]), int(tgt[2])],
                             dist=round(float(d), 1))
            # 保命联锁（不交给模型）：自己血太低就收手
            hp_me = (obs.get("me") or {}).get("hp")
            if hp_me is not None and float(hp_me) < 6.0 and self.job_running():
                self.cancel_job("联锁：自己血低")
                return True
            return False

        # ① 护主
        if th.get("near_player") and now - self._guard_at > 20.0:
            busy_hero = bool(self.job) and "hero" in str(self.job[0])
            if not busy_hero:
                self._guard_at = now
                self.log({"ev": "reflex", "kind": "guard", "dist": th.get("dist"),
                          "type": th.get("type"), "preempt": self.job_running()})
                if self.job_running():
                    self.cancel_job("reflex: 玩家身边有怪")
                try:
                    self.execute(Intent("guard", "macro.hero", {"watch": 25}, prio=0,
                                        why="反射：怪近了", source="reflex"), obs)
                except Exception as e:
                    self.log({"ev": "reflex_err", "kind": "guard", "err": str(e)[:120]})
                return True

        # ② 应答：玩家视线指向我（±12°）且 ≤8 格
        if (pp and mp and d is not None and d <= 8.0
                and det.get("yaw") is not None and now - self._ack_at > 6.0):
            want = math.degrees(math.atan2(-(mp[0] - pp[0]), mp[2] - pp[2]))
            if abs((float(det["yaw"]) - want + 180.0) % 360.0 - 180.0) < 12.0:
                self._ack_at = now
                self.log({"ev": "reflex", "kind": "ack", "d": round(d, 1)})
                try:
                    self.execute(Intent("ack", "gesture", {"kind": "nod"}, prio=30,
                                        why="你在看我", source="reflex"), obs)
                except Exception as e:
                    self.log({"ev": "reflex_err", "kind": "ack", "err": str(e)[:120]})

        # ③ 眼神指令：排队，不抢占；但**手上有活时不排**（否则每 3 秒排一条，作业互相打架）
        tgt = obs.get("player_aim")
        if (tgt and d is not None and d < 14.0 and now - self._look_at > 3.0
                and not self.job_running()):
            self._look_at = now
            self.log({"ev": "reflex", "kind": "gaze", "target": list(tgt[:3])})
            try:
                self.bus.push("mine", target=[int(tgt[0]), int(tgt[1]), int(tgt[2])],
                              use_look=True, **{"from": "reflex"})
            except Exception:
                pass
        return False

    def ensure_in_game(self, now: float) -> bool:
        """客户端在菜单里（`self_pos` 为空）⇒ 先把它送回世界。

        2026-10-29：只做 `reunion` 不够 —— 断线后客户端停在主菜单，`@follow/@goto` 全是空转，
        外面看就是"陪玩一直不动"。这里调 `body_ctl.ensure()`（MCP 活⇒连服；MCP 死⇒重启）。
        """
        if self.dry or not self.allow_restart:
            return False
        if now - float(self.memory.get("conn_at", 0.0)) < 20.0:
            return False
        self.memory["conn_at"] = now
        self.log({"ev": "ensure_in_game"})
        try:
            r = body_ctl.ensure(log=self.log)
            self.log({"ev": "ensure_in_game_done",
                      "in_game": bool(r.get("in_game")), "mcp": bool(r.get("mcp"))})
        except Exception as e:
            self.log({"ev": "ensure_in_game_failed",
                      "err": f"{type(e).__name__}: {e}"[:160]})
        return True

    def reunion(self, now: float) -> bool:
        """看不到玩家 ⇒ **去找人**（不是干等，更不是让健康阶梯判"卡死"）。

        2026-10-29 实测：新世界里两人相距 127 格，客户端实体列表里**没有对方** ⇒
        `no_player` 每 tick 刷屏、位置不动 ⇒ 健康阶梯重启客户端（还连错端口）。
        顺序：① `@follow <player>`（AltoClef 自己找人）→ ② RCON 读坐标 + `@goto`。
        """
        if self.dry or self.job_running():
            return False
        if now - float(self.memory.get("reunion_at", 0.0)) < 25.0:
            return False
        # 玩家根本不在线 ⇒ 别满世界找人（2026-10-29 实测：离线时它反复 @follow/@goto，
        # 位置一动不动，看起来像"卡死"，还白烧 25+110 秒预算）
        online = actions.player_online(self.player)
        if online is False:
            self.memory["reunion_at"] = now
            self.log({"ev": "player_offline", "who": self.player})
            return False
        self.memory["reunion_at"] = now
        self.log({"ev": "reunion", "who": self.player})

        def job() -> dict:
            t0 = time.time()
            try:
                hook_layer.run(self.reg, f"follow {self.player}")
            except Exception as e:
                self.log({"ev": "reunion_cmd_err", "err": f"{type(e).__name__}: {e}"[:120]})
            while time.time() - t0 < 25.0:
                time.sleep(2.0)
                d = as_obj(self.reg.invoke("getGameState", {}).data) or {}
                if d.get("players"):
                    return {"ok": True, "how": "follow", "sec": round(time.time() - t0, 1)}
            pos = actions.player_pos_rcon(self.player)
            if not pos:
                return {"ok": False, "why": "follow 没跟上，RCON 也读不到坐标"}
            try:
                hook_layer.run(self.reg, f"goto {int(pos[0])} {int(pos[1])} {int(pos[2])}")
            except Exception as e:
                return {"ok": False, "why": f"goto 下发失败 {type(e).__name__}: {e}"[:140]}
            while time.time() - t0 < 110.0:
                time.sleep(3.0)
                d = as_obj(self.reg.invoke("getGameState", {}).data) or {}
                if d.get("players"):
                    return {"ok": True, "how": "rcon_goto", "sec": round(time.time() - t0, 1)}
            return {"ok": False, "why": "走了 110 秒还是没看到玩家", "target": list(pos)}

        return self.start_job("reunion:reunion", job)

    def idle_next(self, obs: dict, now: float) -> Intent | None:
        """空闲时**自己找活干**，而且只挑"这个世界里真做得成"的活。

        为什么（operator 2026-10-29 两条）：
          ① 「任务和任务之间的暂停时长太长」—— 旧版一件活做完就 stay，等下一句人话；
          ② 「任务不是死的，没牛就做不出皮甲，选做别的」—— 先查可行性，再换方案。

        优先级（都会先过 `tasks.feasible` 这道便宜检查）：
          吃 → 有武器 → 有口粮 → （以后）插火把/催睡觉
        """
        if self.dry or self.job_running():
            return None
        # AI 交互层：没事干时让模型决定"干什么"（每 25 秒最多一次，避免烧钱/烧延迟）
        if self.ai is not None and self.ai.enabled():
            if now - float(self.memory.get("ai_idle_at", 0.0)) > 25.0:
                self.memory["ai_idle_at"] = now
                self.ai.push("idle", dist=round(float(obs.get("dist") or 0.0), 1))
            return None
        if now - self._idle_at < 1.5:          # 别每 tick 都算一遍（feasible 要扫实体/方块）
            return None
        self._idle_at = now

        def cool(key: str, sec: float) -> bool:
            return now - float(self.memory.get(f"idle_{key}", 0.0)) > sec

        food = (obs.get("me") or {}).get("food")
        # ① 饿了就吃（不占作业池，下一 tick 就能动）
        if food is not None and float(food) < 12 and cool("eat", 30.0):
            if any(actions.count_item(self.reg, f) > 0 for f in
                   ("bread", "cooked_beef", "cooked_porkchop", "cooked_chicken",
                    "cooked_mutton", "apple", "baked_potato", "golden_apple")):
                self.memory["idle_eat"] = now
                return Intent("eat", "macro.eat", {}, prio=20, why="饿了，先吃一口")

        # ② 没武器：先查"这个世界能不能造出来"，能才去做
        sword = max(actions.count_item(self.reg, s) for s in
                    ("wooden_sword", "stone_sword", "iron_sword", "diamond_sword"))
        if sword < 1 and cool("gear", 120.0):
            ok, why = tasks_layer.feasible(self.reg, "wooden_sword")
            self.log({"ev": "idle_check", "what": "gear", "ok": ok, "why": why})
            if ok:
                self.memory["idle_gear"] = now
                return Intent("get_item", "macro.get", {"item": "wooden_sword", "count": 1},
                              prio=25, why=f"手上没武器（{why}）")

        # ③ 没口粮：先看附近有没有能打的动物，没有就跳过（不追不可能的目标）
        if cool("food", 90.0):
            have_food = sum(actions.count_item(self.reg, f) for f in
                            ("bread", "cooked_beef", "cooked_porkchop", "cooked_chicken",
                             "cooked_mutton", "beef", "porkchop", "mutton"))
            if have_food < 2:
                for target in ("cooked_beef", "beef", "mutton", "porkchop"):
                    ok, why = tasks_layer.feasible(self.reg, target)
                    if ok:
                        self.memory["idle_food"] = now
                        self.log({"ev": "idle_check", "what": "food", "ok": True, "why": why})
                        return Intent("get_item", "macro.get",
                                      {"item": target, "count": 2}, prio=30,
                                      why=f"没口粮（{why}）")
                self.log({"ev": "idle_check", "what": "food", "ok": False,
                          "why": "附近没有可打的动物"})
        return None

    def threat(self, pp) -> dict:
        """最近的敌对生物离玩家多远。护主的判据从"血少了"升级成"怪来了"。"""
        try:
            r = self.reg.invoke("getEntities", {"limit": 24})
            d = r.data
            d = d.get("_raw") if isinstance(d, dict) else d
            return self.threat_from(d or [], pp)
        except Exception:
            return {}

    def threat_from(self, entities, pp) -> dict:
        """同一个判据，但实体列表由调用方给 —— 全量观察已经从 stateDigest 里拿到它了，
        再单独发一次 getEntities 就是白花一次往返（W6-4）。"""
        hostiles = [x for x in (entities or []) if str(x.get("is_hostile")) == "true"]
        near = None
        for e in hostiles:
            try:
                ex, ey, ez = [float(v) for v in str(e.get("position")).split(",")]
            except Exception:
                continue
            if not pp:
                continue
            dd = math.dist((ex, ey, ez), pp)
            if near is None or dd < near["dist"]:
                near = {"type": str(e.get("type")), "dist": round(dd, 1),
                        "health": e.get("health")}
        out = {"count": len(hostiles), "near_player": bool(near and near["dist"] <= 12.0)}
        if near:
            out.update(near)
        return out

    def park_point(self, mp, pp, radius):
        """挑一个真能站的停靠点。

        2026-10-27：搜索从"8 个方向 × 一个半径"扩到 **多半径 × 多层高度**。
        旧写法在玩家站在斜坡/树边/半格里时永远找不到点（实测 follow 直接报
        "玩家周围没有可站停靠点"），而"跟随"这种基础能力不该被一个地形卡死。

        2026-10-29（W6-4 收尾）：**逐格探是这里最贵的一步** —— 每个候选要 3 次
        `getBlockAt`（脚下实心 + 脚/头两格空），实测一个全量 tick 因此冲到 **20~29 次 RPC**，
        比"感知"本身贵一个量级。现在用 `getBlocksBatch` 一次读 21 个候选（63 个坐标），
        所以整条搜索通常只要 **1~2 次往返**；老 jar 没有这个工具时自动退回逐格探。
        """
        base = math.atan2(mp[2] - pp[2], mp[0] - pp[0])
        if self.team.enabled:
            # 2026-10-27 实测：两个陪玩都在玩家同一侧时，按"自己相对玩家的方位"找停靠点
            # 会算出**同一格**（实测两人相距 0.92 格 ⇒ 准星打到同伴 ⇒ 挖不动）。
            # 所以编队模式下方位角一律取"我自己的编队槽"，半径也按编号给。
            base = self.team.my_angle(pp)
            radius = self.team.my_radius(radius)
        cands: list[tuple] = []
        for r in (radius, radius + 1, radius + 2, radius + 3):
            for off in (0.0, math.pi / 2, -math.pi / 2, math.pi,
                        math.pi / 4, -math.pi / 4, 3 * math.pi / 4, -3 * math.pi / 4):
                a = base + off
                for dy in (0, 1, -1, 2, -2):
                    cx = pp[0] + math.cos(a) * r
                    cz = pp[2] + math.sin(a) * r
                    cands.append((cx, int(round(pp[1])) + dy, cz,
                                  int(round(cx)), int(round(cz))))

        def _is_air(got: dict, x: int, y: int, z: int, default: bool | None) -> bool | None:
            k = f"{x},{y},{z}"
            if k in got:
                bid = str(got.get(k) or "")
                # "?" = 解析失败 ⇒ 当**不是空气**（宁可不选这个点，也别选到实心里）
                return "air" in bid
            return default

        # ── 批量路径（21 个候选 = 63 个坐标，一次 < 64 上限）─────────────────
        batch_ok = True
        for i in range(0, len(cands), 21):
            chunk = cands[i:i + 21]
            keys: list[str] = []
            for (_cx, y, _cz, x, z) in chunk:
                keys += [f"{x},{y},{z}", f"{x},{y + 1},{z}", f"{x},{y - 1},{z}"]
            try:
                r = self.reg.invoke("getBlocksBatch", {"coords": ";".join(keys)})
                got = r.data if r.ok and isinstance(r.data, dict) else None
            except Exception:
                got = None
            if not got:
                batch_ok = False
                break
            for (cx, y, cz, x, z) in chunk:
                if (_is_air(got, x, y, z, None) and _is_air(got, x, y + 1, z, None)
                        and _is_air(got, x, y - 1, z, None) is False):
                    if not self.team.min_sep_ok((cx, y, cz)):
                        continue
                    return (cx, y, cz)
        if not batch_ok:
            # ── 老 jar 回退：逐格探（原逻辑，慢但正确）────────────────────────
            for (cx, y, cz, x, z) in cands:
                if self.reg.is_air(x, y, z) and self.reg.is_air(x, y + 1, z) \
                        and not self.reg.is_air(x, y - 1, z):
                    if not self.team.min_sep_ok((cx, y, cz)):
                        continue
                    return (cx, y, cz)
        # 实在找不到"标准可站点"：就退到玩家旁边一点（让导航自己去处理落脚）
        dx, dz = mp[0] - pp[0], mp[2] - pp[2]
        L = math.hypot(dx, dz) or 1.0
        return (pp[0] + dx / L * (radius + 0.8), pp[1], pp[2] + dz / L * (radius + 0.8))

    # ── 界面守护 ────────────────────────────────────────────────────────────
    def screen_gate(self, now: float, force: bool = False) -> bool:
        if not force and now - self.screen_ok_at < 2.0:
            return not self.screen_open
        self.screen_ok_at = now
        if self.dry:
            return True
        res = actions.ensure_no_screen(self.reg, log=self.log)
        self.screen_open = bool(res.get("open"))
        if self.screen_open:
            self.log({"ev": "screen_open", **res})
        return not self.screen_open

    # ── 输入：听人说话 / 面板请求 ───────────────────────────────────────────
    def poll_inputs(self) -> None:
        """把"人说的话"收进总线，并把该记的人物信息记下来（只记人，不记场景）。"""
        if self.dry:
            return
        # ① 面板/脚本写进 requests.jsonl 的请求（EXE 面板用这条通道）
        try:
            while True:
                req = self.file_req.pop(self.memory)
                if not req:
                    break
                kind = str(req.pop("kind", "") or "")
                if not kind:
                    continue
                req.pop("id", None)
                # 多陪玩：**同一个文件被 N 个指挥层同时读**，所以必须有人负责"过滤"。
                # `to` 缺省 = 只由"说话权持有者"（编号最小的那个）执行，其余人跳过；
                # `to:"all"` = 大家一起做（停手、跟随这类群体动作）。
                to = str(req.pop("to", "") or "")
                if to and to not in ("all", self.bot_name):
                    continue
                if not to and self.team.enabled and not self.team.may_speak():
                    self.log({"ev": "req_skip", "kind": kind, "why": "不是我的活（说话权）"})
                    continue
                # AI 交互层：面板点的事也交给模型理解（stop 例外，必须即时）
                if self.ai is not None and self.ai.enabled() and kind != "stop":
                    txt = str(req.get("text") or req.get("raw") or kind)
                    self.ai.push("panel", text=txt)
                    self.log({"ev": "ai_route", "kind": kind, "text": txt[:80]})
                    continue
                self.bus.push(kind, **req)
                self.log({"ev": "panel_req", "kind": kind, "to": to or "speaker"})
        except Exception as e:
            self.log({"ev": "panel_error", "err": f"{type(e).__name__}: {e}"[:120]})
        # ② 游戏聊天（玩家打字就是下令）
        try:
            for req in self.chat_in.poll():
                kind = req.get("kind")
                # 玩家在聊天里说的话，所有陪玩都"听见"了；但**只让一个人动手**，
                # 否则三个 bot 会同时冲向同一棵树（真人一眼就看出来是脚本）。
                if self.team.enabled and not self.team.may_speak():
                    self.log({"ev": "heard_defer", "kind": kind})
                    continue
                if kind == "remember":
                    self.persona.note_fact(str(req.get("text") or ""), tag="said")
                    self.speech.say("记住了", force=True)
                # AI 交互层：聊天里说的话整句交给模型（记住类仍走人物记忆，不交给模型）
                if self.ai is not None and self.ai.enabled() and kind != "remember":
                    txt = str(req.get("text") or "")
                    if txt:
                        self.ai.push("say_text", text=txt)
                        self.log({"ev": "ai_route", "kind": kind, "text": txt[:80]})
                        self.persona.note_event("heard_text", text=txt[:60])
                    continue
                elif kind == "get":
                    self.persona.note_event("asked_get", item=req.get("item"))
                elif kind == "mine":
                    self.persona.note_event("asked_mine")
                elif kind == "follow":
                    self.persona.note_event("asked_follow")
                elif kind == "hero":
                    self.persona.note_event("asked_hero")
                elif kind == "build":
                    self.persona.note_event("asked_build")
                elif kind == "supply":
                    self.persona.note_event("asked_supply", item=req.get("item"))
        except Exception as e:
            self.log({"ev": "input_error", "err": f"{type(e).__name__}: {e}"[:160]})

    # ── 语音输入：麦克风 → 总线（和打字同一条路） ────────────────────────────
    def _voice_loop(self) -> None:
        self.log({"ev": "voice_in_start", "stt": self.voice.stt.name})
        while not self._voice_stop.is_set():
            try:
                text = self.voice.listen()
            except Exception as e:
                self.log({"ev": "voice_in_error", "err": f"{type(e).__name__}: {e}"[:120]})
                try:
                    from companion import incident
                    incident.capture(e, where="voice_loop")
                except Exception:
                    pass
                time.sleep(2.0)
                continue
            if not text:
                continue
            self.log({"ev": "voice_heard", "text": text[:80]})
            self.bus.push("free", text=text, **{"from": "voice"})

    def request_to_intent(self, req: dict) -> Intent | None:
        """总线请求 → 意图。动作解析在这里收口，`macro.*` 交给作业池。"""
        kind = str(req.get("kind"))
        who = req.get("from") or self.player
        src = "operator" if req.get("from") else "panel"
        if kind == "stop":
            return Intent("stop", "run_command", {"cmd": "@stop"}, prio=0, why="让我停")
        if kind == "teleport":
            return Intent("teleport", "macro.teleport", {}, prio=1, why="到我身边")
        if kind == "hero":
            return Intent("hero", "macro.hero", {"watch": int(req.get("watch") or 30)},
                          prio=2, why="清怪")
        if kind == "supply":
            return Intent("supply", "macro.supply",
                          {"item": req.get("item") or "bread",
                           "count": int(req.get("count") or 1)}, prio=4,
                          why=f"给他 {req.get('item')}")
        if kind == "get":
            return Intent("get_item", "macro.get",
                          {"item": req.get("item") or "oak_log",
                           "count": int(req.get("count") or 3)}, prio=5, why="帮他要东西")
        if kind == "mine":
            return Intent("look_mine", "macro.mine",
                          {"target": req.get("target"), "use_look": bool(req.get("use_look"))},
                          prio=5, why="挖这块")
        if kind == "build":
            return Intent("build_place", "macro.build",
                          {"target": req.get("target"), "block": req.get("block")},
                          prio=6, why="盖/放方块")
        if kind == "selfcare":
            return Intent("selfservice", "macro.selfcare",
                          {"cmd": req.get("cmd") or "@food"}, prio=6, why="自己补给")
        if kind == "follow":
            return Intent("follow", "macro.follow_player", {}, prio=8, why="跟过来")
        if kind == "ask":
            return Intent("reply", "macro.ask", {"what": req.get("what") or "help",
                                                 "who": who}, prio=10, why="回答他")
        if kind == "free":
            return Intent("free", "macro.free", {"text": req.get("text") or ""}, prio=20,
                          why="自然语言")
        if kind == "say":
            return Intent("say", "macro.say", {"text": req.get("text") or ""}, prio=30,
                          why="说话")
        return None

    def role_allows(self, req: dict) -> bool:
        """这件活归不归我这个角色（点名找我的一律放行）。"""
        allow = ROLE_ALLOW.get(self.role)
        if allow is None:
            return True
        to = str(req.get("to") or "")
        if to and to.lower() in (self.bot_name.lower(), "me", "speaker"):
            return True
        if to:                      # 明确点给别人了
            return False
        return str(req.get("kind")) in allow

    # ── 作业：一次只有一个 ──────────────────────────────────────────────────
    def claim_key(self, kind: str, args: dict) -> str:
        """长活的"招标键"。同一样活全队只能有一个在做（否则三个人抢同一棵树）。"""
        if kind == "get":
            return f"get:{args.get('item')}"
        if kind in ("mine", "build"):
            t = args.get("target") or []
            return f"{kind}:{','.join(str(int(v)) for v in t)}" if t else ""
        if kind == "supply":
            return f"supply:{args.get('item')}"
        if kind == "hero":
            return "hero"
        return ""

    def job_running(self) -> bool:
        return self.job is not None and not self.job[1].done()

    # ── 身体锁：作业期间不让 AltoClef 的竞价链抢走身体（2026-10-30）─────────────
    def body_lock(self, on: bool, why: str = "") -> bool:
        """`takeControl(on)` = `TaskRunner.disable/enable`。

        为什么不能只靠 `@stop`：TaskRunner 每 tick 让所有 chain **报价**，最高的独占身体。
        源码里的报价是 UserTaskChain **50** / SupervisorTaskChain **51** /
        FoodChain「去囤粮」**55** / MobDefenseChain 最高 **70** ⇒ 我们那点 50 分的动作
        （`@mineblock`、`@idle`）**抢不过 55 和 70**，实测一按下就被
        `Collect 12.0 units of food` / `NIGERUNDAYOO … skeletons` 顶掉。
        唯一可靠的是把整条 runner 停掉；代价是防怪/逃生/吃饭也停 ⇒ 所以这里**带超时**：
        超过 `min(JOB_BUDGET, 300)` 秒必自动解锁（见 `tick_body_lock`）。
        """
        if self.dry or not self.allow_body_lock:
            return False
        if on == self._locked:
            if on:
                self._locked_until = max(self._locked_until, time.time() + min(self.job_budget, 300.0))
            return True
        r = actions.body_lock(self.reg, on, log=self.log)
        self._locked = bool(r.get("ok")) and on
        if on:
            self._lock_why = str(why)[:40]
            self._locked_until = time.time() + min(self.job_budget, 300.0)
        else:
            self._lock_why = ""
            self._locked_until = 0.0
        return bool(r.get("ok"))

    def tick_body_lock(self, now: float) -> None:
        """超时兜底：任何情况下都不许让身体永久停在"AltoClef 被 disable"的状态。"""
        if not self._locked:
            return
        if self._locked_until and now > self._locked_until:
            self.log({"ev": "body_lock_expired", "why": self._lock_why,
                      "held_s": round(now - (self._locked_until - min(self.job_budget, 300.0)), 1)})
            self.body_lock(False, "timeout")
        elif self.job is None:
            # 没有作业却还锁着（作业线程异常退出等）⇒ 立刻还回去
            self.body_lock(False, "no-job")

    def poll_job(self) -> bool:
        if self.job is None:
            return False
        name, fut = self.job
        if not fut.done():
            return True
        try:
            res = fut.result()
        except Exception as e:
            res = {"ok": False, "err": f"{type(e).__name__}: {e}"[:200]}
        if isinstance(res, dict) and isinstance(res.get("steps"), list):
            res = {**res, "steps": f"{len(res['steps'])} steps"}
        self.results.append({"job": name, **{k: res[k] for k in ("ok",) if k in res}})
        ran = round(time.time() - self.job_started_at, 1) if self.job_started_at else None
        self.log({"ev": "job_done", "job": name, "ran": ran, "result": res})
        req = getattr(self, "_job_req", None)
        if req is not None:
            self.bus.done(req, bool(isinstance(res, dict) and res.get("ok")),
                          str(res)[:160] if not isinstance(res, dict) else str(res.get("why", "")))
            self._job_req = None
        self.job = None
        self.job_started_at = 0.0
        self.cancel_ev.clear()
        self.body_lock(False, "job done")      # 还回去（AltoClef 的防怪/逃生重新上线）
        for k in list(self._claim_keys):
            self.team.release(k)
            self._claim_keys.discard(k)
        return False

    def start_job(self, name: str, fn) -> bool:
        if self.job_running():
            return False
        self.clean_before_job()
        # 清场之后、真正开工之前：把身体从竞价系统里拿过来（否则 50 分的活会被 55/70 抢走）
        self.body_lock(True, name)
        self.cancel_ev.clear()                  # 上一轮的取消旗不许带进新作业
        self.job_started_at = time.time()
        # 每件新活重新计播报次数（一件活最多 2 句）
        self.memory["narrate_n"] = 0
        self.memory["narrate_at"] = 0.0
        try:
            self.job_start_pos = actions.pos_of(self.reg)   # 卡死判据的基准点
        except Exception:
            self.job_start_pos = None
        self.job = (name, self.pool.submit(fn))
        self.stats[name] = self.stats.get(name, 0) + 1
        self.log({"ev": "job_start", "job": name})
        return True

    def cancel_job(self, why: str) -> bool:
        """让正在跑的长活**尽快**收手（0.3~1.2 秒内，由 actions 的 cancel 钩子兑现）。

        为什么不直接杀线程：作业池只有一个线程，杀不干净会让"单瞄准者"约束失效。
        正确做法是置旗 + 让身体停手，worker 自己走正常出口回来。
        """
        if not self.job_running():
            return False
        self.cancel_ev.set()
        self.jobs_cancelled += 1
        if not self.dry:
            actions.stop_all(self.reg)
        self.log({"ev": "job_cancel", "job": self.job[0] if self.job else None,
                  "why": why, "ran": round(time.time() - self.job_started_at, 1)})
        return True

    def clean_before_job(self) -> None:
        """每个作业前统一清场：停掉自主任务 + 确保没有 GUI。"""
        if self.dry:
            return
        actions.stop_all(self.reg)
        actions.ensure_no_screen(self.reg, log=self.log)

    def run_macro_inline(self, kind: str, args: dict) -> dict:
        """agent 要做的长活 —— **就地跑在作业线程里**。

        为什么不能走 `start_job`：`macro.free` 本身就是作业池里的活（池只有 1 个 worker），
        它再往池里提交一个就会**自己等自己**，永远不返回。而"就地跑"天然满足单瞄准者。
        """
        obs = self._last_obs or self.observe() or {}
        fn = self.macro(kind, args, obs)
        if fn is None:
            return {"ok": False, "why": f"做不到 {kind}"}
        self.clean_before_job()
        try:
            return fn() or {"ok": False, "why": "没有返回"}
        except Exception as e:
            return {"ok": False, "err": f"{type(e).__name__}: {e}"[:200]}

    # ── 执行一条意图 ────────────────────────────────────────────────────────
    def execute(self, intent: Intent, obs: dict) -> bool:
        """返回值 = 这条意图**有没有真的下发出去**（false 时上层要给请求回执失败）。"""
        args = dict(intent.args or {})

        # 宏：长活交给作业池，绝不和别的动作并行（单瞄准者）
        if intent.action.startswith("macro."):
            kind = intent.action.split(".", 1)[1]
            fn = self.macro(kind, args, obs)
            if fn is None:
                return False
            key = self.claim_key(kind, args)
            if key and not self.team.claim(key):
                self.log({"ev": "claim_denied", "key": key,
                          "owner": self.team.owner(key)})
                return False
            if key:
                self._claim_keys.add(key)
            return self.start_job(f"{kind}:{intent.name}", fn)

        # 眼神指令的"先瞄准再放命令"是两步，指挥层负责把这两步串起来
        aim = args.pop("aim", None)
        if aim:
            r = self.reg.invoke("lookAt", {"x": aim["x"], "y": aim["y"], "z": aim["z"]})
            self.log({"ev": "act", "step": "lookAt", "pos": [aim["x"], aim["y"], aim["z"]],
                      "ok": r.ok, "err": r.err})
            if not self.dry:
                time.sleep(0.25)          # mod 说明：准星 raytrace 晚一个 tick 才一致

        # 远目标自动换 gotoFar（gotoXYZ 对远目标会冻住）
        if intent.action in ("gotoXYZ", "gotoFar"):
            mp, tx = obs.get("self_pos"), (args.get("x"), args.get("y"), args.get("z"))
            if mp and all(v is not None for v in tx):
                d = math.dist(mp, tx)
                if d > FAR_AT:
                    intent.action = "gotoFar"
                    args["horizon"] = 32

        r = self.reg.invoke(intent.action, args)
        self.actions += 1
        self._act_at = time.time()          # 健康阶梯用：最近一次真的下发过动作
        self.stats[intent.name] = self.stats.get(intent.name, 0) + 1
        # 先把 data 变成字符串再截断：直接对 dict 切片会 KeyError: slice(...)
        try:
            data = r.data if r.data is None else json.dumps(r.data, ensure_ascii=False)[:160]
        except Exception:
            data = str(r.data)[:160]
        self.log({"ev": "act", "intent": intent.name, "source": intent.source,
                  "action": intent.action, "args": args, "ok": r.ok, "err": r.err,
                  "data": data})
        return True

    # ── 宏：能被"做到底并核验"的长活 ────────────────────────────────────────
    def _ai_planner(self, text: str) -> dict | None:
        """AI 收到人话前的自动前置：认出"要一样东西"就先算可行性/分解图。"""
        from companion import goal_graph
        return goal_graph.auto_plan(self.reg, text, log=self.log)

    def narrate(self, obs: dict, now: float) -> None:
        """③ 里程碑播报（2026-10-29）：长活 >30 秒、玩家在 8 格内、全程最多 2 句。

        **不计入"有效互动"**（`interaction_score` 会跳过 kind=narrate）——
        否则就是拿播报给指标注水，终局判据立刻失效。
        """
        if self.dry or not self.job_running():
            return
        d = obs.get("dist")
        if d is None or float(d) > 8.0:
            return
        if now - self.job_started_at < 30.0:
            return
        n = int(self.memory.get("narrate_n", 0))
        if n >= 2 or now - float(self.memory.get("narrate_at", 0.0)) < 45.0:
            return
        job = str(self.job[0] if self.job else "")
        text = self._narrate_text(job)
        if not text:
            return
        self.memory["narrate_n"] = n + 1
        self.memory["narrate_at"] = now
        self.log({"ev": "narrate", "job": job, "text": text, "n": n + 1, "counted": False})
        try:
            self.speech.say(text, kind="narrate")
        except Exception:
            pass

    @staticmethod
    def _narrate_text(job: str) -> str:
        for pre, line in (("get", "材料还在凑，凑够我就回来"),
                          ("mine", "这块有点硬，还在挖"),
                          ("hero", "怪还没清完，我顶着"),
                          ("supply", "正往你那边走"),
                          ("reunion", "我找路过去，稍等"),
                          ("ai", "我琢磨一下怎么弄")):
            if job.startswith(pre):
                return line
        return ""

    def _ai_runner(self, kind: str, args: dict) -> dict:
        """AI 的工具调用 → 指挥层执行路径（单瞄准者 + 验收 + 护栏）。

        ② 自动降级（2026-10-29）：它自己不会做的目标，**先换一个能做的**，
        并把"换了什么/为什么"塞回结果里 —— 模型必须转告玩家（不许偷偷换）。
        """
        note = ""
        substituted = False
        if kind == "get":
            from companion import goal_graph
            item = str(args.get("item") or "")
            if not item:
                return {"ok": False, "why": "没说要什么"}
            ok, why = goal_graph.can_obtain(self.reg, item)
            if not ok:
                alt = None
                try:
                    from companion import tasks as _tasks
                    alt, _trace = _tasks.pick_alternative(self.reg, item)
                except Exception:
                    alt = None
                if not alt:
                    self.log({"ev": "ai_get_denied", "item": item, "why": why})
                    return {"ok": False, "why": why, "substituted": False}
                self.log({"ev": "ai_get_substitute", "asked": item, "use": alt, "why": why})
                args = {**args, "item": alt}
                substituted = True
                note = (f"原目标 {item} 做不了（{why}）；已改用 {alt}。"
                        "回答玩家时必须说明这一点。")
        fn = self.macro(kind, args or {}, getattr(self, "_last_obs", None) or {})
        if fn is None:
            return {"ok": False, "why": f"{kind} 现在做不了"}
        try:
            res = fn() or {"ok": False, "why": "没有返回"}
        except Exception as e:
            return {"ok": False, "err": f"{type(e).__name__}: {e}"[:200]}
        if note and isinstance(res, dict):
            res = {**res, "note": note, "substituted": True}
        return res

    def macro(self, kind: str, args: dict, obs: dict):
        reg, dry = self.reg, self.dry
        stop = self.cancel_ev.is_set            # 传给 actions：取消旗一置位就收手
        if dry:
            return None

        # ── 上层任务层（2026-10-27，operator）────────────────────────────────
        # 「有的太底层工作不需要一步步去交代，给一个上层任务就 ok，但要确保对底层能力的掌控」。
        # 这几类活**不再在这里逐步下发**：交给 tasks.py 的 TaskSpec（目标 + 验收 + 预算），
        # 由它调 AltoClef 自己的任务/命令，并带着进度、卡死自救、世界验收。
        _spec_kind = {"follow_player": "follow"}.get(kind, kind)
        if _spec_kind in ("get", "supply", "hero", "follow", "mine"):
            spec = tasks_layer.spec_for(_spec_kind, args, player=self.player)
            if spec is not None:
                def _prog(p: dict) -> None:
                    self.log({"ev": "job_progress", "job": _spec_kind,
                              "n": p.get("progress"),
                              "what": str(p.get("message"))[:120]})

                def _run() -> dict:
                    return tasks_layer.execute(reg, spec, player=self.player,
                                               cancel=stop, log=self.log, on_progress=_prog)
                return _run

        if kind == "mine":
            t = args.get("target")
            if not t and args.get("use_look"):
                # "挖掉你看着的那块"：等下一次 look_detect 给坐标
                return None
            if not t:
                return None
            return lambda: actions.mine(reg, t, cancel=stop, log=self.log)

        if kind == "teleport":
            def _tp():
                # ⚠️ 2026-10-27 现场血证：这里原来写的是 `/tp <玩家> Buddy` ——
                #    那是把**玩家**拽到陪玩身上（聊天里能看到 "已将Xiaorun传送至Buddy"）。
                #    需求是"陪玩传送到玩家身边"，而且**绝不能反过来移动玩家**（那是极没分寸的行为）。
                #    现在：`/tp <自己的名字> <玩家>`，并且用**自己的坐标变化**核验。
                p0 = actions.pos_of(reg)
                if self.allow_teleport and p0:
                    pp = obs.get("player_pos")
                    if pp:
                        self.body.say(f"/tp {self.bot_name} {self.player}")
                        time.sleep(1.2)
                        p = actions.pos_of(reg)
                        if p and math.dist(p, p0) > 1.0 and math.dist(p, pp) < 4.0:
                            return {"ok": True, "how": "command",
                                    "dist": round(math.dist(p, pp), 2)}
                pp = obs.get("player_pos")
                if not pp:
                    return {"ok": False, "why": "看不到玩家"}
                g = actions.goto(reg, int(round(pp[0])), int(round(pp[1])),
                                 int(round(pp[2])), timeout=90.0, tol=2.4, cancel=stop)
                return {"ok": bool(g.get("ok")), "how": "walk", "dist": g.get("dist")}
            return _tp

        if kind == "ask":
            what = str(args.get("what") or "help")
            def _ask():
                if what == "name":
                    self.speech.say("Buddy 啊，你的陪玩", force=True)
                elif what == "where":
                    p = actions.pos_of(reg)
                    self.speech.say(f"我在 {int(p[0])} {int(p[1])} {int(p[2])} 这附近"
                                    if p else "我也有点懵，找不着北了")
                else:
                    self.speech.say("挖、砍、盖、打怪、跟着你，你张嘴说就行", force=True)
                return {"ok": True, "what": what}
            return _ask

        if kind == "say":
            text = str(args.get("text") or "")
            return lambda: {"ok": self.speech.say(text, force=True), "text": text}

        if kind == "free":
            text = str(args.get("text") or "")
            def _free():
                # 2026-10-27：这里以前是个**空转占位**（返回 ok:false "需要 AI 规划器"），
                # 于是所有"没被 NLU 命中的自然语言"都静默消失。现在真的接上 agent：
                # 有端点走模型工具循环；没端点它自己降到 NLU+规则，仍然做动作、仍然回一句话。
                # 语音开着时走**流式**：第一句生成出来就开始念（实测首句 1.03 秒）。
                use_stream = bool(self.voice.enabled and self.voice.tts.name != "none"
                                  and self.agent.has_model)
                if not use_stream:
                    return self.agent.run(text, player=self.player)
                spoken: list[str] = []

                def on_sentence(s: str) -> None:
                    spoken.append(s)
                    if self.voice.enabled:
                        self.voice.speak(s)          # 语音：一句一句立刻说
                res = self.agent.run_stream(text, player=self.player, on_sentence=on_sentence)
                full = "".join(spoken).strip()
                if full:
                    self.speech.say(full, force=True)  # 聊天里只留一句总结，不刷屏
                return res
            return _free

        if kind == "get":
            item, n = args.get("item"), int(args.get("count") or 1)
            def _get():
                # 2026-10-27（operator 指正）：**别自己规划合成**。AltoClef 的
                # `TaskCatalogue.getItemTask` 就是"弄到某物"的完整决策链
                # （采集→合成→烧炼→找/放工作台，还自带失败重试）。有 hook 就走它。
                before = actions.count_item(reg, item)
                want = before + n

                def _prog(p: dict) -> None:
                    # MCP 的 notifications/progress（message = AltoClef 自己的任务链）——
                    # 长活跑 20~120 秒期间，外面终于能看到"它在干什么"了。
                    self.log({"ev": "job_progress", "job": "get", "item": item,
                              "n": p.get("progress"), "what": str(p.get("message"))[:120]})

                hook = hook_layer.obtain(reg, item, want, on_progress=_prog)
                how = "altoClef.ResourceTask" if hook.get("ok") else "cmd_get"
                if not hook.get("ok"):
                    actions.cmd_watch(reg, f"@get {item} {want}", seconds=6)
                res = actions.wait_goal(
                    reg, lambda: actions.count_item(reg, item) >= want,
                    timeout=args.get("timeout", 60),
                    expect=item.split(":")[-1], empty_grace=12.0, cancel=stop)
                retried = ""
                if not res.get("ok") and not stop():
                    # 2026-10-27 现场：`<Mine And Collect: [[oak_log] x 2]>` 有时会
                    # **22 秒就自己收工、一个也没弄到**（附近找不到资源，它不会主动跑远）。
                    # 兜底：自己扫一圈，有就走到跟前再让它来一次；没有就如实说没有。
                    short = item.split(":")[-1]
                    near = reg.invoke("scanBlocks", {"name": short, "radius": 48,
                                                     "limit": 4}).data or {}
                    hits = (near.get("_raw") if isinstance(near, dict) else near) or []
                    if hits:
                        try:
                            pos = str(hits[0].get("position")).replace(" ", "").split(",")
                            x, y, z = (int(float(v)) for v in pos)
                            actions.goto(reg, x, y + 1, z, timeout=45.0, tol=2.6, cancel=stop)
                            retried = "walked_to_resource"
                            hook_layer.obtain(reg, item, want, on_progress=_prog)
                            res = actions.wait_goal(
                                reg, lambda: actions.count_item(reg, item) >= want,
                                timeout=args.get("timeout", 40),
                                expect=short, empty_grace=12.0, cancel=stop)
                        except Exception as e:
                            retried = f"retry_err:{type(e).__name__}"
                    else:
                        retried = f"附近 48 格没有 {short}"
                return {"ok": res.get("ok"), "how": how, "before": before,
                        "after": actions.count_item(reg, item), "want": want,
                        "why": res.get("why", ""), "hook": hook.get("why", ""),
                        "retry": retried}
            return _get

        if kind == "supply":
            item = args.get("item")
            count = int(args.get("count") or 1)
            def _supply():
                # 2026-10-27（operator 指正）：AltoClef 自己就有 `give <player> <items>` ——
                # "去弄到 + 交给你"一条命令，比我们"丢在脚边等人捡"高级。先试它。
                before = actions.count_item(reg, item)
                hp = hook_layer.run(reg, f"give {self.player} {item.split(':')[-1]} {count}")
                if hp.get("ok"):
                    gone = actions.wait_until(
                        lambda: actions.count_item(reg, item) < before, 25.0, 1.0, cancel=stop)
                    if gone:
                        return {"ok": True, "how": "cmd_give", "before": before,
                                "after": actions.count_item(reg, item)}
                pp = obs.get("player_pos")
                if not pp:
                    return {"ok": False, "why": "看不到玩家", "hook": hp.get("why", "")}
                h = actions.handover(reg, pp, item, count, toward=self.player,
                                     cancel=stop, allow_teleport=self.allow_teleport,
                                     log=self.log)
                picked, seen = False, False
                t0 = time.time()
                while time.time() - t0 < 12.0 and not stop():
                    items = [e for e in actions.nearby_items(reg)
                             if math.dist(e["pos"], pp) <= 2.5]
                    if items:
                        seen = True
                    elif seen:
                        picked = True
                        break
                    time.sleep(0.5)
                after = actions.count_item(reg, item)
                if not picked and not stop():
                    self.speech.say(f"{item.split(':')[-1]} 放你脚边了", force=True)
                return {"ok": bool(picked), "item": item, "how": "handover",
                        "moving_drop": h.get("moving"), "approach": h.get("approach"),
                        "dist": h.get("dist"), "before": before, "after": after,
                        "hook": hp.get("why", ""),
                        "why": "" if picked else "没看到掉落物被吃（可能没贴近，或东西还在地上）"}
            return _supply

        if kind == "hero":
            def _hero():
                # 2026-10-27 夜：`@hero` 会跑远追怪、还会 pillar up 躲 creeper
                # （实测：敌对 4 → 8，chat 里是 `Pillaring up to y=71`）。
                # 护主的正确姿势是"只清玩家身边的"，所以先用**近战腿**，
                # 它顶不住（够不着/太久）再退回 `@hero`。
                pp = obs.get("player_pos")
                if pp:
                    local = actions.clear_near(reg, pp, radius=float(args.get("radius") or 8.0),
                                               budget_s=float(args.get("watch") or 40.0),
                                               cancel=stop, log=self.log)
                    if local.get("ok") or local.get("near_after", 0) < (local.get("near_before") or 0):
                        return {"ok": True, "how": "melee_local",
                                "near_before": local.get("near_before"),
                                "near_after": local.get("near_after"),
                                "killed": local.get("killed")}
                    if local.get("near_before") == 0:
                        return {"ok": True, "how": "none_near", "note": "玩家附近没怪"}
                r = actions.cmd_watch(reg, "@hero", seconds=args.get("watch", 40))
                return {"ok": r.get("ok"), "how": "cmd_hero",
                        "chat": r.get("new_chat", [])[-6:]}
            return _hero

        if kind == "selfcare":
            cmd = args.get("cmd") or "@food"
            def _self():
                # 2026-10-27：`@food` 是"去找吃的"（会跑很远）。**手上已经有食物时**先自己吃 ——
                # 这是 MCP 体检里"唯一没有杠杆的生存动作"（见 docs/MCP-READINESS-2026-10-27.md）。
                ate = actions.eat(reg, log=self.log)
                if ate.get("ok") and ate.get("food_after", 0) > ate.get("food_before", 0):
                    return {"ok": True, "how": "eat_inventory", **ate}
                # 手上没吃的 / 不饿但有别的诉求 → 才走 AltoClef 的找食物链
                r = actions.cmd_watch(reg, cmd, seconds=args.get("watch", 45))
                chat = r.get("new_chat", [])[-8:]
                ok = bool(r.get("ok")) and not any(
                    ("no " in c.lower() and "found" in c.lower()) or "cannot" in c.lower()
                    for c in chat)
                return {"ok": ok, "cmd": cmd, "chat": chat, "eat": ate,
                        "why": "" if ok else "这条命令没给出可用结果（可能附近没材料）"}
            return _self

        if kind == "build":
            x, y, z = (int(v) for v in args["target"])
            block = args.get("block")
            def _build():
                if block:
                    reg.invoke("run_command", {"cmd": f"@equip {block}"})
                # ⚠️ 2026-10-30：不能只调一次。`tryPlace` 有每 4 tick 一次的限速（原版节奏），
                # 被限速时只是"这一拍没放"，不是失败 ⇒ `actions.place_block` 会把这类重试掉，
                # 而几何/背包/保护类问题立刻返回（不拿 8 次去撞墙）。
                r = actions.place_block(reg, x, y, z, log=self.log)
                ok = bool(r.get("ok")) or actions.wait_until(
                    lambda: block is None or actions.block_id(reg, x, y, z).endswith(block), 20.0)
                return {"ok": bool(ok), "sent": bool(r.get("sent")), "tries": r.get("tries"),
                        "err": r.get("why") or r.get("reason"),
                        "now": actions.block_id(reg, x, y, z)}
            return _build

        if kind == "follow_player":
            pp = obs.get("player_pos")
            if not pp:
                return None
            if self.team.enabled:                    # 编队：按编号站到自己的槽位
                sx, sy, sz = self.team.slot(pp)
                tgt = [int(round(sx)), int(round(sy)), int(round(sz))]
            else:
                tgt = [int(round(v)) for v in pp]
            return lambda: actions.goto(reg, *tgt, timeout=40.0, tol=3.4, cancel=stop)

        return None

    # ── 身体掉线：退避记录 + 主动自愈 ────────────────────────────────────────
    def body_down(self, now: float) -> None:
        if not self.no_body_since:
            self.no_body_since = now
            self.no_body_n = 0
        self.no_body_n += 1
        if now - self.no_body_logged_at >= 30.0:      # 3Hz 不许刷屏
            self.no_body_logged_at = now
            self.log({"ev": "no_body", "n": self.no_body_n,
                      "down_s": round(now - self.no_body_since, 1),
                      "tries": self.ensure_tries})
        if self.dry or not self.allow_restart:
            return
        # 第一下等 8 秒（很多掉线是客户端正在重启），之后每 60 秒一次
        wait = 8.0 if self.ensure_tries == 0 else 60.0
        if now - self.ensure_at < wait:
            return
        self.ensure_at = now
        self.ensure_tries += 1
        self.log({"ev": "body_ensure", "try": self.ensure_tries})
        t0 = time.time()
        try:
            from companion import body_ctl
            snap = body_ctl.ensure(log=self.log)
            self.log({"ev": "body_ensure_done", "try": self.ensure_tries,
                      "sec": round(time.time() - t0, 1),
                      "mcp": bool(snap.get("mcp")), "in_game": bool(snap.get("in_game"))})
        except Exception as e:
            self.log({"ev": "body_ensure_failed", "try": self.ensure_tries,
                      "err": f"{type(e).__name__}: {e}"[:160]})

    def body_up(self, now: float) -> None:
        if not self.no_body_since:
            return
        out = now - self.no_body_since
        self.log({"ev": "body_back", "down_s": round(out, 1),
                  "n": self.no_body_n, "tries": self.ensure_tries})
        self.no_body_since = 0.0
        self.no_body_n = 0
        self.ensure_tries = 0
        self.ensure_at = 0.0

    # ── 快车道：作业跑着也能说话/应答/急停 ───────────────────────────────────
    def fast_lane(self, obs: dict, now: float) -> None:
        if self.dry:
            return
        req = self.bus.pop_only(FAST_KINDS)
        if not req:
            return
        kind = str(req.get("kind"))
        if kind == "stop":
            self.cancel_job("operator stop")
            self.speech.say("停了", force=True)
            self.bus.done(req, True, "已停")
            return
        intent = self.request_to_intent(req)
        if intent is None or not intent.action.startswith("macro."):
            self.bus.done(req, True, "已忽略（无对应动作）")
            return
        fn = self.macro(intent.action.split(".", 1)[1], dict(intent.args or {}), obs)
        if fn is None:
            self.bus.done(req, False, "现在做不了")
            return
        try:
            res = fn()
        except Exception as e:
            res = {"ok": False, "err": f"{type(e).__name__}: {e}"[:160]}
        ok = bool(isinstance(res, dict) and res.get("ok"))
        note = str((res or {}).get("why") or (res or {}).get("text") or "")[:160] \
            if isinstance(res, dict) else ""
        self.bus.done(req, ok, note)
        self.log({"ev": "fast_done", "kind": kind, "ok": ok, "result": res})

    # ── 作业看门狗：一个卡死的活不许锁住整晚 ─────────────────────────────────
    def job_watchdog(self, now: float) -> None:
        if self.job is None or self.job_started_at <= 0:
            return
        ran = now - self.job_started_at
        if ran > self.job_budget and not self.cancel_ev.is_set():
            self.log({"ev": "job_over_budget", "job": self.job[0],
                      "ran": round(ran, 1), "budget": self.job_budget})
            self.cancel_job(f"超过 {int(self.job_budget)} 秒没做完")

    def job_stall_check(self, obs: dict, now: float) -> None:
        """**作业期间的**卡死检测（原来这一步漏了）。

        2026-10-27 血证：`session_test` 报两次 `frozen45s`，而 `health()` 在
        `if self.job_running(): ... return` 那条路上**根本不会被调用** ⇒ 身体冻住 45 秒也没人管。

        ⚠️ 同一天更贵的教训：**"没位移"不等于"卡死"**。合成、挖矿、熔炼都会站着不动 ——
        第一版只看"45 秒位移 <1.5 格"就把一条正在合成的 `@get cobblestone` 任务（78→80）
        当成卡死取消了（operator 当场看到"合成木镐有问题"）。现在必须**四件事同时成立**才敢取消：
          ① 跑满 45 秒；② 位移 <1.5 格；③ 它自己的任务链是空的；④ 挖掘状态不在挖。
        """
        if self.job is None or self.job_started_at <= 0:
            return
        name = self.job[0]
        if name.startswith(("say:", "ask:", "free:")):
            return
        ran = now - self.job_started_at
        if ran < 45.0 or self.cancel_ev.is_set():
            return
        pos = obs.get("self_pos")
        if not pos or not self.job_start_pos:
            return
        moved = math.dist(pos, self.job_start_pos)
        if moved >= 1.5:
            return
        # ③ 它还在干活就**不许**取消；但如果**任务描述 60 秒一字未变**且没位移 ⇒ 那是真没进展
        busy = False
        task_now = ""
        try:
            tasks = actions.chain(self.reg)
            task_now = " | ".join(tasks)[:150]
            busy = bool(tasks)
            st = self.reg.invoke("mineStatus", {}).data
            if isinstance(st, dict) and truthy(st.get("mining")):
                task_now += "|mining"
                busy = True
        except Exception:
            pass
        if busy:
            if task_now != getattr(self, "_job_task_sig", ""):
                self._job_task_sig = task_now
                self._job_task_at = now
            elif now - getattr(self, "_job_task_at", now) < 60.0:
                self.log({"ev": "job_still_working", "job": name, "ran": round(ran, 1),
                          "moved": round(moved, 2), "task": task_now[:70]})
                return
            else:
                self.log({"ev": "job_no_progress", "job": name, "ran": round(ran, 1),
                          "task": task_now[:90]})
                self.cancel_job(f"{name} 的任务 60 秒没变过（{task_now[:40]}）")
                return
        self.log({"ev": "job_frozen", "job": name, "ran": round(ran, 1),
                  "moved": round(moved, 2), "chain": "empty"})
        self.cancel_job(f"{name} 跑了 {int(ran)} 秒只动 {moved:.1f} 格，且任务链是空的")
        self.recover_stuck(now)

    def recover_stuck(self, now: float) -> None:
        """卡死后的**立刻**自救：先挪一格，连犯两次就重启客户端。

        2026-10-27：`session_test` 一直能抓到 `frozen45s` —— 因为作业分支里不跑 `health()`，
        而作业一取消又要等 6 秒×5 级阶梯才轮到重启。这里把"最有效的两级"提前做掉：
        ① `unstick()`（8 方向挪一格，实测 1.3~2.6 格）；
        ② 3 分钟内冻结 ≥2 次 ⇒ 直接重启客户端（今天已两次验证：重启后同一格立刻能挖）。
        """
        if self.dry:
            return
        self._froze_n = getattr(self, "_froze_n", 0) + 1
        if not getattr(self, "_froze_at", 0.0) or now - self._froze_at > 180.0:
            self._froze_at = now
            self._froze_n = 1
        r = actions.unstick(self.reg, log=self.log)
        self.log({"ev": "stuck_unstick", "ok": r.get("ok"), "moved": r.get("moved"),
                  "froze_n": self._froze_n})
        if r.get("ok"):
            return
        if self._froze_n >= 2 and self.allow_restart and now - self.restart_at > 120.0:
            self.restart_at = now
            self.restarts += 1
            self.log({"ev": "body_restart", "n": self.restarts, "why": "冻结两次且挪不动"})
            try:
                from companion import body_ctl
                body_ctl.restart(log=self.log)
                self._froze_n = 0
                self.body_healthy = True
            except Exception as e:
                self.log({"ev": "body_restart_failed", "err": f"{type(e).__name__}: {e}"[:160]})

    # ── 身体健康：两条腿都会卡，必须分开查 ──────────────────────────────────
    def health(self, obs: dict, now: float) -> None:
        mp = obs.get("self_pos")
        moved = math.dist(mp, self.last_pos) if (mp and self.last_pos) else 99.0
        self.last_pos = mp
        # 2026-10-29 血证：看不到玩家 + 没活干时的"静止"不是卡死。
        # 旧版会把它判成 froze → unstick → 重启客户端（而且重启还连错端口），
        # 外面看就是"陪玩卡死十几分钟"。现在：15 秒内没下发过任何动作就不算。
        idle_by_design = (obs.get("player_pos") is None and not self.job_running()
                          and now - getattr(self, "_act_at", 0.0) > 15.0)
        if idle_by_design:
            self.still_since = None
            return
        if moved > 0.5:
            self.still_since, self.nav_fail = None, 0
            if not self.body_healthy:
                self.body_healthy = True
                self.log({"ev": "body_ok"})
            return
        if self.still_since is None:
            self.still_since = now
            return
        if now - self.still_since < STILL_S:
            return
        self.nav_fail += 1
        if self.nav_fail == 1:
            self.reg.invoke("stopPathing", {})
            self.reg.invoke("run_command", {"cmd": "@stop"})
            self.log({"ev": "body_stall", "fail": 1, "next": "retry"})
        elif self.nav_fail == 2:
            pp = obs.get("player_pos")
            if pp:
                self.reg.invoke("run_command",
                                {"cmd": f"@goto {int(pp[0])} {int(pp[1])} {int(pp[2])}"})
            self.log({"ev": "body_stall", "fail": 2, "next": "@goto"})
        elif self.nav_fail == 3:
            self.screen_gate(now, force=True)
            self.log({"ev": "body_stall", "fail": 3, "next": "screen_guard"})
        elif self.nav_fail == 4:
            # 2026-10-27：卡住最常见的真因其实是"站在别人身体里/贴着怪"——
            # 准星先打到实体 ⇒ 挖不动 ⇒ 想挖出去的导航也挖不动。挪一格就够了，别急着重启。
            if not self.dry:
                r = actions.unstick(self.reg, log=self.log)
                if r.get("ok"):
                    self.nav_fail, self.still_since = 0, None
            self.log({"ev": "body_stall", "fail": 4, "next": "unstick"})
        elif self.nav_fail == 5 and not self.dry:
            # "准星死了"的判据：`Mining aborted … progress=-0.10` 反复出现 ⇒
            # 挪一格/@stop 都救不了，只有换一个身体（重启客户端）。
            bad = actions.mining_aborted(self.reg)
            self.log({"ev": "mining_aborted_seen", "n": bad})
            if bad >= 2 and self.allow_restart and now - self.restart_at > RESTART_COOLDOWN:
                self.restart_at = now
                self.restarts += 1
                self.log({"ev": "body_restart", "n": self.restarts,
                          "why": f"Mining aborted ×{bad}（准星/渲染死了）"})
                try:
                    from companion import body_ctl
                    body_ctl.restart(log=self.log)
                    self.nav_fail, self.still_since = 0, None
                    self.body_healthy = True
                except Exception as e:
                    self.log({"ev": "body_restart_failed",
                              "err": f"{type(e).__name__}: {e}"[:160]})
        else:
            if not self.body_healthy:
                self.log({"ev": "body_unhealthy", "fail": self.nav_fail})
            if self.allow_restart and now - self.restart_at > RESTART_COOLDOWN:
                self.restart_at = now
                self.restarts += 1
                self.log({"ev": "body_restart", "n": self.restarts, "why": "位置连续不动"})
                try:
                    from companion import body_ctl
                    body_ctl.restart(log=self.log)
                    self.nav_fail, self.still_since = 0, None
                    self.body_healthy = True
                except Exception as e:
                    self.log({"ev": "body_restart_failed", "err": f"{type(e).__name__}: {e}"[:160]})
            elif self.body_healthy:
                self.body_healthy = False
        self.still_since = now

    # ── 主循环 ──────────────────────────────────────────────────────────────
    def step(self) -> None:
        now = time.time()
        self.tick_body_lock(now)      # 身体锁超时兜底（绝不让 AltoClef 永久停着）
        # 两级观察：便宜的 tick 只读 1 次状态（~30ms），全量感知每 full_every 个 tick 一次
        self._tick_i += 1
        full = ((self._tick_i % max(1, self._full_every)) == 0
                and time.time() >= getattr(self, "_full_skip_until", 0.0))
        t_obs = time.time()
        obs = self.observe(fast=not full)
        self._obs_ms = round((time.time() - t_obs) * 1000.0, 1)
        # 背压：客户端一旦开始卡（一次观察 >600ms），说明身体侧在换区块/GC/编译着色器；
        # 这时候继续全量轮询只会把队列越堆越长（实测单次 getBlockAt 会从 7ms 飙到 930ms）。
        if full:
            if self._obs_ms > 600.0:
                self._slow_n = getattr(self, "_slow_n", 0) + 1
                if self._slow_n >= 2:
                    self._full_skip_until = time.time() + 8.0
                    self._slow_n = 0
                    self.log({"ev": "observe_slow", "ms": self._obs_ms,
                              "next": "fast-only 8s"})
            else:
                self._slow_n = 0
        if not obs:
            # 2026-10-27：以前这里只写一行 no_body 就 return，3Hz ⇒ 5 分钟刷 900 行，
            # 而且**永远不尝试自愈**（MCP 掉线就整晚干等）。现在：退避记录 + 主动 ensure。
            self.body_down(now)
            self.poll_inputs()               # 掉线期间人和面板的话不能丢（进总线排队）
            return
        self.body_up(now)
        self._last_obs = obs
        # 在菜单里（读不到自己坐标）⇒ 先连回世界，别对着菜单空转
        if not obs.get("self_pos") and self.ensure_in_game(now):
            self.health(obs, now)
            self.tick += 1
            return
        # 礼仪计数落账（30 秒一行；终局判据"挡路 0 / 抢块 0"的唯一数据源）
        if now - self._et_at > 30.0:
            self._et_at = now
            self.log({"ev": "etiquette", **self.guards.snapshot()})
            # AI 成本快照（面板读它显示 token/预算；原子写，读到的一定是完整 JSON）
            if self.ai is not None:
                try:
                    p = ROOT / "runs" / "state" / "ai.json"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    tmp = p.with_suffix(".tmp")
                    tmp.write_text(json.dumps(self.ai.snapshot(), ensure_ascii=False, indent=1),
                                   encoding="utf-8")
                    tmp.replace(p)
                except Exception:
                    pass
        # 队伍心跳：把"我在哪、我在干什么"写给同伴看（面板也从这块黑板读状态）
        if self.team.enabled and now - self._beat_at > 2.0:
            self._beat_at = now
            self.team.beat(pos=obs.get("self_pos"),
                           busy=self.job[0] if self.job_running() else "")

        # 界面守护优先于一切：GUI 开着时准星是死的，任何导航/挖掘都会失败
        self.screen_gate(now)

        # 听人说话（聊天框就是最自然的下令方式）
        self.poll_inputs()

        # 反射弧：护主 / 应答 / 眼神 —— 每 tick 都跑，作业期间也跑（2026-10-29）
        if self.reflex(obs, now):
            self.health(obs, now)
            self.tick += 1
            return

        # 见面打招呼 + 定期把人物记忆落盘
        if not self.greeted and obs.get("player_pos"):
            self.greeted = True
            self.speech.hello(self.persona.p.how_to_call())
            self.log({"ev": "persona", "brief": self.persona.brief()})
        if now - self._mem_saved > 120:
            self._mem_saved = now
            try:
                self.persona.note_event("session_tick",
                                        minutes=(now - self.session_t0) / 60.0)
                self.persona.save()
            except Exception as e:
                self.log({"ev": "memory_error", "err": f"{type(e).__name__}: {e}"[:120]})

        has_player = bool(obs.get("player_pos"))
        if self.job_running():
            self.poll_job()
            # 作业跑着也要服务人：说话 / 应答 / 急停不碰准星，走快车道插队
            self.fast_lane(obs, now)
            self.job_watchdog(now)
            self.job_stall_check(obs, now)
            self.narrate(obs, now)
            if full:
                self.log({"ev": "job_tick", "job": self.job[0] if self.job else None,
                          "pos": obs.get("self_pos"),
                          "ran": round(now - self.job_started_at, 1) if self.job else 0})
            return
        self.poll_job()

        # ── AI 交互层：有事件就交给模型（一次一件，走作业池）─────────────────
        if self.ai is not None and self.ai.pending() and not self.job_running():
            ev = self.ai.next_event(now)
            if ev is not None:
                ok = self.start_job(f"ai:{ev.kind}", lambda ev=ev: self.ai.handle(ev))
                if ok:
                    if full:
                        self.log({"ev": "ai_start", "kind": ev.kind, "goal": ev.goal()[:120]})
                    self.health(obs, now)
                    self.tick += 1
                    return

        # 人的要求优先于自主行为。
        # ⚠️ 2026-10-27 血证：这里以前先判 "看不到玩家" 就 return，**把整条请求队列饿死** ——
        #    玩家不在视野里时，连"说句话""取个东西"都不执行，外面看起来就是"陪玩不理我"。
        #    现在：不依赖玩家的活照干，只有真的要贴人（跟随/递物/传送）才等玩家出现。
        req = self.bus.pop() if not self.dry else None
        intent = None
        # AI 交互层：自然语言/说话/提问一律交给模型（不再用规则 NLU 决定做什么）
        if (req is not None and self.ai is not None and self.ai.enabled()
                and str(req.get("kind")) in ("free", "say", "ask")):
            text = str(req.get("text") or req.get("what") or "")
            if text:
                self.ai.push("say_text", text=text)
                self.bus.done(req, True, "交给 AI")
                self.log({"ev": "ai_route", "kind": req.get("kind"), "text": text[:80]})
                req = None
        if req is not None and not self.role_allows(req):
            self.log({"ev": "role_denied", "role": self.role, "kind": req.get("kind"),
                      "to": req.get("to")})
            self.bus.done(req, False, f"我是{self.role}，这件活不归我")
            req = None
        if req is not None:
            intent = self.request_to_intent(req)
            if intent is None:
                self.bus.done(req, True, "已忽略（无对应动作）")
            else:
                self._job_req = req

        if not has_player:
            if intent is None or intent.action not in PLAYER_FREE:
                # 看不到人先去找人（新世界可能相距 100+ 格），别站着等也别被判"卡死"
                if self.reunion(now):
                    self.health(obs, now)
                    self.tick += 1
                    return
                # 2026-10-30（W6-2 长跑血证）：这一行以前**每 tick 写一次**（6Hz ⇒ 两小时 4 万行），
                # 把 trace 淹了、也让"真有事件"看不出来。改成 15 秒一条 + 带累计次数。
                _now_np = time.time()
                self._np_n = int(self.memory.get("no_player_n", 0)) + 1
                self.memory["no_player_n"] = self._np_n
                if _now_np - float(self.memory.get("no_player_at", 0.0)) > 15.0:
                    self.memory["no_player_at"] = _now_np
                    self.log({"ev": "no_player", "n": self._np_n})
                if intent is not None:
                    self.bus.done(req, False, "看不到玩家")
                    self._job_req = None
                self.health(obs, now)
                return

        if intent is None:
            intent = self.planner.plan(obs, self.memory)
        # 空闲不站着：规则说"待着"时，自己找一件**这个世界做得到**的活
        if intent is None or intent.name == "stay":
            idle = self.idle_next(obs, now)
            if idle is not None:
                intent = idle
        if intent is None:
            intent = Intent("stay", "wait", prio=90, why="planner 无输出")

        verdict = self.guards.check(intent, obs, now)
        if not verdict.allow:
            # 护栏拦截按"理由变化 + 1 秒"限流，否则 3Hz 会把 trace 刷满同一句话
            key = f"{intent.name}|{verdict.reason[:24]}"
            if key != self._last_veto or now - self._last_veto_at > 1.0:
                self._last_veto, self._last_veto_at = key, now
                self.log({"ev": "veto", "intent": intent.name, "why": verdict.reason})
            intent = Intent("stay", "wait", prio=90, why="被护栏拦下")

        if intent.action != "wait":
            self.guards.stamp(intent, now)
            sent = self.execute(intent, obs)
            if not sent and req is not None and self._job_req is not None:
                self.bus.done(req, False, "没执行（缺参数或现在做不到）")
                self._job_req = None

        self.health(obs, now)
        self.tick += 1
        if full:
            self.log({"ev": "tick", "d": round(obs["dist"], 2) if obs.get("dist") else None,
                      "intent": intent.name, "src": intent.source,
                      "hp": (obs.get("me") or {}).get("hp"),
                      "p_hp": (obs.get("pl") or {}).get("hp"),
                      "p_held": (obs.get("detail") or {}).get("held")})

    def run(self, seconds: float, hz: float) -> dict:
        t_end = time.time() + seconds
        period = 1.0 / max(0.25, hz)
        while time.time() < t_end:
            t0 = time.time()
            try:
                self.step()
            except Exception as e:
                self.log({"ev": "error", "err": f"{type(e).__name__}: {e}"[:200]})
                # 崩溃自动留证（beta：别人报"它崩了"时，我们要能拿到现场）
                try:
                    from companion import incident
                    d = incident.capture(e, where="commander.step")
                    if d:
                        self.log({"ev": "incident_saved", "dir": str(d)})
                except Exception:
                    pass
            time.sleep(max(0.0, period - (time.time() - t0)))
        return {"ticks": self.tick, "actions": self.actions, "stats": self.stats,
                "body_healthy": self.body_healthy, "restarts": self.restarts,
                "jobs": self.results}


def build_planner(a) -> object:
    from companion.planner import Requests
    req = Requests(Path(a.requests) if getattr(a, "requests", None) else
                   ROOT / "runs" / "requests.jsonl")
    rule = RulePlanner(requests=req)
    if a.planner == "rule":
        return rule
    ai = AIPlanner(a.ai_endpoint, model=a.ai_model or "deepseek-flash", timeout=a.ai_timeout)
    if a.planner == "ai":
        return ai
    return HybridPlanner(ai, rule, ai_every=a.ai_every)


def main() -> int:
    ap = argparse.ArgumentParser(description="陪玩指挥层")
    ap.add_argument("--port", type=int, default=25351)
    ap.add_argument("--settings", required=True)
    ap.add_argument("--player", default="Xiaorun")
    ap.add_argument("--bot-name", default="Buddy", help="自己在服务器里的用户名（传送命令要用）")
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--hz", type=float, default=6.0,
                    help="控制循环频率（默认 6：便宜的 tick 只读 1 次状态，反射弧才短）")
    ap.add_argument("--full-every", type=int, default=3,
                    help="每 N 个 tick 做一次全量感知（playerDetail/pathStatus/getEntities）")
    ap.add_argument("--server", default="",
                    help="服务器 host:port（身体重启时要用；不填读 BUDDY_SERVER 或 127.0.0.1:25565）")
    ap.add_argument("--no-ai", dest="ai_layer", action="store_false", default=True,
                    help="关掉 AI 交互层（离线回归/没有 key 时用；默认开）")
    ap.add_argument("--planner", choices=["rule", "ai", "hybrid"], default="rule")
    ap.add_argument("--ai-endpoint", default="", help="留空 = 用 config/ai.json 或环境变量里的 DeepSeek 配置")
    ap.add_argument("--ai-model", default="", help="留空 = 用配置里的模型（默认 deepseek-flash）")
    ap.add_argument("--ai-timeout", type=float, default=45.0)
    ap.add_argument("--ai-every", type=float, default=1.0)
    ap.add_argument("--team", default="", help="多陪玩黑板路径（留空=单人模式，不参与编队/认领/说话权）")
    ap.add_argument("--role", default="playmate",
                    help="在队伍里的角色：playmate / guard / gatherer / builder / scout")
    ap.add_argument("--dry", action="store_true", help="只记账不发动作")
    ap.add_argument("--no-clean-start", action="store_true")
    ap.add_argument("--no-restart", action="store_true", help="禁止自动重启客户端")
    ap.add_argument("--no-chat", action="store_true", help="不说话（回归模式：老用例按零聊天打分）")
    ap.add_argument("--no-teleport", action="store_true", help="禁用一键传送（只走过去）")
    ap.add_argument("--no-body-lock", action="store_true",
                    help="作业期间不禁用 AltoClef 的 TaskRunner（回归对照用；默认启用身体锁）")
    ap.add_argument("--voice", action="store_true", help="开声音（TTS 朗读 / STT 预留）")
    ap.add_argument("--voice-hub", default="",
                    help="多实例共享麦克风：填 hub 的 inbox.jsonl 路径（留空=本进程自己开麦）")
    ap.add_argument("--engagement", choices=["quiet", "normal", "clingy"], default="normal",
                    help="参与度：quiet=少说话保持距离 / normal / clingy=黏人")
    ap.add_argument("--requests", default="", help="请求队列 jsonl 路径（默认 runs/requests.jsonl）")
    ap.add_argument("--outdir", default="",
                    help="trace 落盘目录（默认 runs/commander/<ts>；多实例并行时必须分开）")
    ap.add_argument("--catalog", action="store_true", help="打印全量能力清单后退出")
    a = ap.parse_args()

    body = Body(a.port, a.settings)
    if a.catalog:
        reg = Registry(body)
        cat = reg.catalog()
        print(json.dumps(cat, ensure_ascii=False, indent=1)[:20000])
        print(f"\n工具 {len(cat['tools'])} 个 + 命令 {len(cat['commands'])} 条")
        return 0

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    # 2026-10-29：3 个陪玩并行时，同一秒启动会落进**同一个** runs/commander/<ts>，
    # 三条 trace 混在一个文件里 ⇒ "谁回的话/谁干的话" 不可归因。--outdir 给测试用。
    outdir = Path(a.outdir) if a.outdir else ROOT / "runs" / "commander" / ts
    outdir.mkdir(parents=True, exist_ok=True)

    # 2026-10-30（W6-2 两小时长跑的血证）：跑到 28 分钟时 trace **突然不再增长**，
    # 而进程还活着、RSS 32MB —— 主循环卡在某个调用里，外面完全看不出卡在哪。
    # `faulthandler` 每 180 秒把所有线程栈 dump 到 stderr（long_run 已把 stderr 收进
    # commander.log）⇒ 下次卡住直接有现场，不用再猜。
    try:
        import faulthandler
        faulthandler.enable()
        faulthandler.dump_traceback_later(180, repeat=True)
    except Exception:
        pass

    cmdr = Commander(body, build_planner(a), player=a.player, outdir=outdir,
                     dry=a.dry, clean_start=not a.no_clean_start,
                     allow_restart=not a.no_restart, allow_chat=not a.no_chat,
                     allow_teleport=not a.no_teleport, voice=a.voice,
                 body_lock=not a.no_body_lock,
                     engagement=a.engagement, requests_path=a.requests,
                     bot_name=a.bot_name,
                     ai_endpoint=(a.ai_endpoint if a.planner != "rule" else ""),
                     ai_model=a.ai_model, team_path=a.team, role=a.role,
                     ai_layer=a.ai_layer, voice_hub=a.voice_hub)
    cmdr._full_every = max(1, int(a.full_every))
    if a.server:
        import os
        os.environ["BUDDY_SERVER"] = a.server   # 健康阶梯重启时要连的服
        print(f"[commander] body={a.port} player={a.player} planner={a.planner} "
          f"tools={len(cmdr.reg.tools)} dry={a.dry} chat={not a.no_chat} "
          f"voice={a.voice} trace={cmdr.trace}", flush=True)
    print(f"[commander] 人物记忆: {cmdr.persona.brief()}", flush=True)

    summary = cmdr.run(a.seconds, a.hz)
    (outdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    print(f"[commander] done {json.dumps(summary, ensure_ascii=False)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
