#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/planner.py —— 决策层：观察 → 意图（**AI 将来就插在这里**）

为什么要接口而不是直接写死规则：
  operator：「以后可能还会接入 ai」。所以决策必须是一个**可替换的实现**，
  而不是散在循环里的 if。三个实现共用同一个契约：

    Planner.plan(obs, memory) -> Intent | None

  · RulePlanner   —— 现在的全部行为（纯规则、可单测、零依赖）
  · AIPlanner     —— 把同一份 obs 发出去，等一个 intent 回来（HTTP/本地模型都行）
  · HybridPlanner —— AI 先说话，AI 沉默/超时/胡说就自动回落到规则（**fail-open**）

  契约刻意做得很窄：AI 只需要输出 `{"name": "...", "action": "...", "args": {...}}`，
  其中的 `action` 可以是**任何一个** AltoClef 工具或 `run_command`，也可以是
  `macro.<mine|get|supply|hero|selfcare|build|follow_player>`（指挥层会把它跑成"做到底并核验"）。
  所以接上 AI 之后能力面不会缩水（这是"保留所有能力"在决策侧的保证）。

`memory` 是一个普通 dict，跨 tick 存活；规则和 AI 都可以读写它（记仇、记约定、记上次失败）。

**请求通道**（`runs/requests.jsonl`，一行一个 JSON）：
  零聊天是铁律，所以"人让陪玩干点什么"不能走 chat。当前用**文件队列**（工具/将来的手势
  识别器都可以往里写），格式：
      {"kind":"get",   "item":"oak_log", "count":3}
      {"kind":"mine",  "target":[-268,108,-4]}
      {"kind":"supply","item":"wooden_pickaxe","count":1}
      {"kind":"hero"}
      {"kind":"selfcare","cmd":"@food"}
      {"kind":"build", "target":[-268,108,-4],"block":"oak_planks"}
  指挥层取走后会把这一行标记成已消费（写 `runs/requests.done` 记 offset）。
"""
from __future__ import annotations

import json
import math
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parent.parent
REQUESTS = ROOT / "runs" / "requests.jsonl"


# ── 意图 ───────────────────────────────────────────────────────────────────
@dataclass
class Intent:
    """一条"要做什么"。名字给人和日志看；action/args 是真正发给身体的东西。"""
    name: str
    action: str = "wait"                 # MCP 工具名 / "run_command" / "macro.*" / "wait"
    args: dict = field(default_factory=dict)
    prio: int = 50                       # 小 = 先做
    cooldown: float = 0.0                # 同名的冷却秒数
    why: str = ""                        # 人话理由（进 trace，便于复盘）
    source: str = "rule"                 # rule | ai | operator

    def as_dict(self) -> dict:
        return {"name": self.name, "action": self.action, "args": self.args,
                "prio": self.prio, "why": self.why, "source": self.source}


class Planner(Protocol):
    name: str

    def plan(self, obs: dict, memory: dict) -> Intent | None: ...


# ── 工具函数：从玩家视线解出"他在看哪块" ────────────────────────────────────
LOOK_MAX = 10.0
LOOK_STEP = 0.35
SKIP = ("bedrock", "barrier", "end_portal", "command_block", "water", "lava", "spawner")


def ray_block(eye, yaw_deg, pitch_deg, probe):
    yaw, pitch = math.radians(yaw_deg), math.radians(pitch_deg)
    dx = -math.sin(yaw) * math.cos(pitch)
    dy = -math.sin(pitch)
    dz = math.cos(yaw) * math.cos(pitch)
    last, t = None, 0.5
    while t <= LOOK_MAX:
        x, y, z = (math.floor(eye[0] + dx * t), math.floor(eye[1] + dy * t),
                   math.floor(eye[2] + dz * t))
        if (x, y, z) != last:
            last = (x, y, z)
            b = probe(x, y, z)
            if b and not b.get("_air"):
                name = str(b.get("id") or "")
                short = name.split(":")[-1]
                if short and short != "air" and not any(k in short for k in SKIP):
                    return (x, y, z, name)
        t += LOOK_STEP
    return None


def ray_voxels(eye, yaw_deg, pitch_deg):
    """一条视线射线上**按顺序**穿过的体素（去重）。给批量读用。"""
    yaw, pitch = math.radians(yaw_deg), math.radians(pitch_deg)
    dx = -math.sin(yaw) * math.cos(pitch)
    dy = -math.sin(pitch)
    dz = math.cos(yaw) * math.cos(pitch)
    out, last, t = [], None, 0.5
    while t <= LOOK_MAX:
        v = (math.floor(eye[0] + dx * t), math.floor(eye[1] + dy * t),
             math.floor(eye[2] + dz * t))
        if v != last:
            last = v
            out.append(v)
        t += LOOK_STEP
    return out


def ray_block_batch(eye, yaw_deg, pitch_deg, fetch):
    """和 `ray_block` **同一个判据**，但整条射线的体素**一次**问完（W6-4）。

    为什么：逐体素 `getBlockAt` 是本项目最贵的一步（2026-10-29 实测：一条 10 格射线
    10~28 次往返）。`fetch(list_of_voxels) -> {coord: blockId}` 由调用方给
    （生产里是 `getBlocksBatch`），于是"看一眼"从 10~28 次降到 **1 次**。
    `fetch` 返回空 → 返回 None（调用方自己回退旧的逐体素路径）。
    """
    vox = ray_voxels(eye, yaw_deg, pitch_deg)
    if not vox:
        return None
    got = fetch(vox) or {}
    for (x, y, z) in vox:
        bid = str(got.get(f"{x},{y},{z}") or "")
        if not bid or bid == "?" or "air" in bid:
            continue
        short = bid.split(":")[-1]
        if short and not any(k in short for k in SKIP):
            return (x, y, z, bid)
    return None


# ── 请求队列（零聊天下的"人对陪玩下指令"通道） ──────────────────────────────
class Requests:
    """读 `runs/requests.jsonl` 里还没被消费的行。取走后 offset 记在 memory 里。"""

    def __init__(self, path: Path = REQUESTS):
        self.path = path

    def pop(self, memory: dict) -> dict | None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return None
        off = int(memory.get("req_off", 0))
        for i in range(off, len(lines)):
            raw = lines[i].strip()
            if not raw or raw.startswith("#"):
                memory["req_off"] = i + 1
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                memory["req_off"] = i + 1
                continue
            if isinstance(obj, dict) and obj.get("kind"):
                memory["req_off"] = i + 1
                return obj
            memory["req_off"] = i + 1
        return None


# ── 规则实现（当前的全部行为） ──────────────────────────────────────────────
class RulePlanner:
    """纯规则。所有阈值都是显式常量 —— 真人校准的时候要一眼能改。"""

    FOLLOW_AT = 4.6
    STOP_AT = 3.2
    BACK_AT = 1.9
    PARK = 3.0
    CLOSE_AT = 2.4
    GUARD_HP = 8.0
    LOOK_COOLDOWN = 3.0
    LOOK_MAX_DIST = 14.0
    ACK_COOLDOWN = 8.0
    ACK_DEG = 12.0

    name = "rule"

    def __init__(self, requests: Requests | None = None):
        self.requests = requests

    def plan(self, obs: dict, memory: dict) -> Intent | None:
        me, pl, det = obs.get("me") or {}, obs.get("pl") or {}, obs.get("detail") or {}
        mp, pp, d = obs.get("self_pos"), obs.get("player_pos"), obs.get("dist")

        # P0 请求队列：人明确要的东西，压过一切主动性（但仍排在保命之后）
        if self.requests:
            req = self.requests.pop(memory)
            if req:
                return self._intent_from_request(req)

        if not mp or not pp:
            return Intent("stay", "wait", why="看不到玩家", prio=90)

        # P0 护主：玩家血低 / 玩家身边有敌对生物 → 清场 + 贴身
        #    2026-10-27：补 jar 有了 getEntities，护主终于可以**看**而不是只靠"血少了才知道"。
        #    以前只能等玩家 hp<8 才反应（那时已经挨打了），现在怪一进 12 格就先上。
        php = pl.get("hp")
        if php is not None and float(php) < self.GUARD_HP:
            return Intent("guard", "macro.hero", {"watch": 30}, prio=0,
                          cooldown=30.0, why=f"玩家血 {php}，清场")
        threat = obs.get("threat") or {}
        if threat.get("near_player") and time.time() - memory.get("guard_at", 0.0) > 20.0:
            memory["guard_at"] = time.time()
            return Intent("guard", "macro.hero", {"watch": 30}, prio=0, cooldown=20.0,
                          why=f"玩家 {threat.get('dist')} 格有 {threat.get('type')}")

        # P1 眼神指令：玩家盯着一个方块 → 走过去挖掉它（两跳站定 + break queue + 核验）
        yaw, pitch = det.get("yaw"), det.get("pitch")
        if yaw is not None and pitch is not None and d is not None and d < self.LOOK_MAX_DIST:
            eye = (pp[0], pp[1] + 1.62, pp[2])
            tgt = ray_block(eye, float(yaw), float(pitch), obs.get("probe"))
            if tgt:
                key = f"look:{tgt[0]},{tgt[1]},{tgt[2]}"
                if time.time() - memory.get(key, 0.0) > self.LOOK_COOLDOWN:
                    memory[key] = time.time()
                    return Intent("look_mine", "macro.mine",
                                  {"target": [tgt[0], tgt[1], tgt[2]]},
                                  prio=10, cooldown=4.0, why=f"你在看 {tgt[3]}")

        # P2 应答：玩家正看着我
        if det and d is not None and d <= 8 and time.time() - memory.get("ack_at", 0.0) > self.ACK_COOLDOWN:
            want = math.degrees(math.atan2(-(mp[0] - pp[0]), mp[2] - pp[2]))
            if abs((float(det.get("yaw") or 0.0) - want + 180.0) % 360.0 - 180.0) < self.ACK_DEG:
                memory["ack_at"] = time.time()
                return Intent("ack", "gesture", {"kind": "nod"}, prio=30, cooldown=6.0,
                              why="你在看我")

        # P4 跟随 / 让路 / 停靠
        if d > self.FOLLOW_AT:
            tgt = obs.get("park_point")
            if tgt:
                return Intent("follow", "gotoXYZ",
                              {"x": int(tgt[0]), "y": int(tgt[1]), "z": int(tgt[2])},
                              prio=60, cooldown=1.2, why=f"{d:.1f} 格，跟上去")
        elif d < self.BACK_AT:
            tgt = obs.get("park_point")
            if tgt:
                return Intent("approach", "gotoXYZ",
                              {"x": int(tgt[0]), "y": int(tgt[1]), "z": int(tgt[2])},
                              prio=55, cooldown=2.5, why=f"{d:.1f} 格，太近了退开")
        return Intent("stay", "wait", prio=90, why=f"{d:.1f} 格，待着")

    # ── 请求 → 意图 ────────────────────────────────────────────────────────
    def _intent_from_request(self, req: dict) -> Intent:
        kind = str(req.get("kind"))
        if kind == "get":
            return Intent("get_item", "macro.get",
                          {"item": req.get("item"), "count": int(req.get("count") or 1),
                           "watch": int(req.get("watch") or 90)},
                          prio=5, cooldown=0.0, why=f"要 {req.get('item')}×{req.get('count')}",
                          source="operator")
        if kind == "mine":
            return Intent("look_mine", "macro.mine", {"target": req.get("target")},
                          prio=5, cooldown=0.0, why="指定挖这块", source="operator")
        if kind == "supply":
            return Intent("supply", "macro.supply",
                          {"item": req.get("item"), "count": int(req.get("count") or 1)},
                          prio=4, cooldown=0.0, why=f"递 {req.get('item')}", source="operator")
        if kind == "hero":
            return Intent("hero", "macro.hero", {"watch": int(req.get("watch") or 40)},
                          prio=1, cooldown=0.0, why="清怪", source="operator")
        if kind == "selfcare":
            return Intent("selfservice", "macro.selfcare",
                          {"cmd": req.get("cmd") or "@food",
                           "watch": int(req.get("watch") or 60)},
                          prio=6, cooldown=0.0, why="自己补给", source="operator")
        if kind == "build":
            return Intent("build_place", "macro.build",
                          {"target": req.get("target"), "block": req.get("block")},
                          prio=5, cooldown=0.0, why="盖/放方块", source="operator")
        if kind == "follow":
            return Intent("follow", "macro.follow_player", {}, prio=8, cooldown=0.0,
                          why="跟过来", source="operator")
        return Intent("stay", "wait", prio=90, why=f"未知请求 {kind}")


# ── AI 实现（现在只是接口，接上就用） ───────────────────────────────────────
SYSTEM_PROMPT = """你是一个 Minecraft 陪玩机器人的决策器。你不说话，只输出一个 JSON。

输入：一个 JSON，含 me(自己)、pl(玩家)、detail(玩家朝向/手持)、dist(距离)、
      catalog(可用能力清单)、memory(跨 tick 记忆)。
输出：{"name": "...", "action": "...", "args": {...}, "prio": 0-99, "why": "一句话"}

规则：
- action 可以是 catalog.tools 里的任意工具名，或 "run_command"（args.cmd = "@命令"），
  或 "macro.mine/get/supply/hero/selfcare/build/follow_player"（做到底并自动核验），或 "wait"。
- 绝不能输出聊天内容（禁止 ChatMessage）。
- prio 越小越先做：0 保命 / 5 玩家明确要求 / 10 眼神指令 / 30 应答 / 60 跟随 / 90 待命。
- 拿不准就输出 {"name":"stay","action":"wait","prio":90}。
"""


class AIPlanner:
    """把观察发给模型，拿回一个意图。任何异常都返回 None（由 Hybrid 回落规则）。"""

    name = "ai"

    def __init__(self, endpoint: str, model: str = "local", timeout: float = 8.0,
                 catalog: dict | None = None):
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout
        self.catalog = catalog or {}
        self.last_err = ""

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def plan(self, obs: dict, memory: dict) -> Intent | None:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(
                    {"me": obs.get("me"), "pl": obs.get("pl"), "detail": obs.get("detail"),
                     "dist": obs.get("dist"), "catalog": self.catalog,
                     "memory": {k: v for k, v in memory.items() if not k.startswith("look:")}},
                    ensure_ascii=False)},
            ],
            "temperature": 0.2,
        }
        try:
            resp = self._post(payload)
        except Exception as e:
            self.last_err = f"{type(e).__name__}: {e}"[:160]
            return None
        try:
            text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
            text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            obj = json.loads(text)
            if not isinstance(obj, dict) or "action" not in obj:
                self.last_err = "AI 输出不是合法意图"
                return None
            if str(obj["action"]) == "ChatMessage":
                self.last_err = "AI 试图发聊天（铁律 5）"
                return None
            return Intent(name=str(obj.get("name") or obj["action"]),
                          action=str(obj["action"]),
                          args=dict(obj.get("args") or {}),
                          prio=int(obj.get("prio", 50)),
                          why=str(obj.get("why", ""))[:120],
                          source="ai")
        except Exception as e:
            self.last_err = f"解析失败 {type(e).__name__}: {e}"[:160]
            return None


class HybridPlanner:
    """AI 优先，AI 沉默就规则兜底。以后接 AI 只需要换这一层，指挥循环一行不改。"""

    name = "hybrid"

    def __init__(self, ai: AIPlanner, rule: RulePlanner | None = None, ai_every: float = 1.0):
        self.ai = ai
        self.rule = rule or RulePlanner()
        self.ai_every = ai_every
        self.last_ai = 0.0
        self.last_src = "rule"

    def plan(self, obs: dict, memory: dict) -> Intent | None:
        now = time.time()
        if now - self.last_ai >= self.ai_every:
            self.last_ai = now
            it = self.ai.plan(obs, memory)
            if it is not None:
                self.last_src = "ai"
                return it
            self.last_src = "rule"
        return self.rule.plan(obs, memory)
