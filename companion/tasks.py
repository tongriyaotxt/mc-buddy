#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/tasks.py —— **上层任务层**：一句话交代一件事，底层怎么干由它自己决定

operator 2026-10-27：「有的太底层工作不需要一步步去交代，给一个上层任务就 ok，
但**要确保对底层能力的掌控能力**」。

于是这一层把"做什么"和"怎么做"分开：

    ┌─ 上层（这里）───────────────┐   ┌─ 底层（AltoClef / actions）─────────────┐
    │ TaskSpec: 目标 / 验收 / 预算 │ → │ obtainItem / runCommandText / mine ...  │
    │ 监督：进度、卡死、超时、重试  │ ← │ userTask / taskChain / progress / aim   │
    └─────────────────────────────┘   └────────────────────────────────────────┘

**不做**的事：不写"先点工作台、再点 3 号格"这种步骤。合成/采集/熔炼/找工作台
都是 AltoClef 的 `ResourceTask` 自己规划，我们只给"要什么、要多少"。

**保留**的掌控点（少一样都不行）：
  · `hook.take_control(True/False)` 抢/还控制权；`cancelAll` 一键停；
  · 进度：MCP `notifications/progress`（message = 它自己的任务链）；
  · 验收：**世界状态谓词**（背包数量 / 距离 / 附近敌对 / 方块是否空气），不信返回值；
  · 卡死：任务链为空 或 63 秒不变 ⇒ 挪一格 → 再不行重启客户端；
  · 预算：每个 TaskSpec 自带 `budget_s`，到点必停（不会堵住人的下一句话）。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field

from . import actions, hook
from .body import truthy
from .registry import Registry

# ── 验收谓词（**只读世界**，禁止凭返回值下结论） ────────────────────────────
def _acc_item(reg: Registry, item: str, n: int) -> tuple[bool, str]:
    have = actions.count_item(reg, item)
    return have >= n, f"{item} {have}/{n}"


def _acc_player_item(reg: Registry, player: str, item: str, n: int) -> tuple[bool, str]:
    have = actions.count_player_item(player, item)
    if have < 0:
        return False, "读不到玩家背包（RCON 不通）"
    return have >= n, f"玩家 {item} {have}/{n}"


def _acc_near(reg: Registry, player: str, le: float) -> tuple[bool, str]:
    st = reg.invoke("getGameState", {}).data or {}
    pl = next((p for p in (st.get("players") or [])
               if str(p.get("name", "")).lower() == player.lower()), None)
    me = reg.pos(st.get("self") or {})
    pp = reg.pos(pl) if pl else None
    if not (me and pp):
        return False, "看不到玩家或自己"
    d = math.dist(me, pp)
    return d <= le, f"距离 {d:.2f}/{le}"


def _acc_no_hostiles(reg: Registry, player: str, radius: float) -> tuple[bool, str]:
    st = reg.invoke("getGameState", {}).data or {}
    pl = next((p for p in (st.get("players") or [])
               if str(p.get("name", "")).lower() == player.lower()), None)
    pp = reg.pos(pl) if pl else None
    if not pp:
        return False, "看不到玩家"
    near = [e for e in actions.nearby_entities(reg, limit=40)
            if e["hostile"] and actions.dist(e["pos"], pp) <= radius]
    return not near, f"附近敌对 {len(near)}"


def _acc_block_air(reg: Registry, pos) -> tuple[bool, str]:
    if not pos:
        return False, "没有目标坐标"
    bid = actions.block_id(reg, *pos)
    return bid.endswith(":air"), f"{pos} 现在是 {bid}"


ACCEPT = {"item": _acc_item, "player_item": _acc_player_item, "near": _acc_near,
          "no_hostiles": _acc_no_hostiles, "block_air": _acc_block_air}


# ── 任务规格 ────────────────────────────────────────────────────────────────
@dataclass
class TaskSpec:
    name: str
    goal: str                        # 人话："拿到两根木头"
    lever: str                       # obtain / run / mine / goto
    args: dict = field(default_factory=dict)
    accept: str = ""                 # ACCEPT 里的键
    accept_args: dict = field(default_factory=dict)
    budget_s: float = 90.0
    note: str = ""


def spec_for(kind: str, args: dict, *, player: str = "Xiaorun") -> TaskSpec | None:
    """把"陪玩要做的事"翻译成上层任务（**只有一句话，没有步骤**）。"""
    item = str(args.get("item") or "oak_log").split(":")[-1]
    n = int(args.get("count") or 1)
    if kind == "get":
        return TaskSpec("get", f"拿到 {n} 个 {item}", "obtain",
                        {"item": item, "count": n},
                        "item", {"item": item, "n": n}, budget_s=100.0)
    if kind == "supply":
        return TaskSpec("supply", f"把 {n} 个 {item} 交给 {player}", "give",
                        {"player": player, "item": item, "count": n},
                        "player_item", {"player": player, "item": item, "n": n},
                        budget_s=120.0,
                        note="优先用它自己的 give；读不到玩家背包就退化成丢在脚边")
    if kind == "hero":
        return TaskSpec("hero", f"清掉 {player} 8 格内的怪", "clear_near",
                        {"player": player, "radius": float(args.get("radius") or 8.0)},
                        "no_hostiles", {"player": player, "radius": 10.0}, budget_s=60.0)
    if kind == "follow":
        return TaskSpec("follow", f"走到 {player} 身边", "follow",
                        {"player": player},
                        "near", {"player": player, "le": 3.4}, budget_s=60.0)
    if kind == "mine":
        t = args.get("target")
        return TaskSpec("mine", f"挖掉 {t}", "mine", {"target": t},
                        "block_air", {"pos": t}, budget_s=60.0)
    return None


# ── 可行性预检（2026-10-29 operator：「任务不是死的，没牛就做不出皮甲，选做别的」）────
# 每一项 = 拿到它需要什么"世界里的东西"。查这些比"闷头跑 100 秒然后失败"便宜得多。
NEEDS = {
    "leather": ("entity", ("cow", "mooshroom")),
    "leather_helmet": ("entity", ("cow", "mooshroom")),
    "leather_chestplate": ("entity", ("cow", "mooshroom")),
    "leather_leggings": ("entity", ("cow", "mooshroom")),
    "leather_boots": ("entity", ("cow", "mooshroom")),
    "mutton": ("entity", ("sheep",)),
    "cooked_beef": ("entity", ("cow",)),
    "beef": ("entity", ("cow",)),
    "porkchop": ("entity", ("pig",)),
    "chicken": ("entity", ("chicken",)),
    "egg": ("entity", ("chicken",)),
    "bone": ("entity", ("skeleton",)),
    "string": ("entity", ("spider",)),
    "iron_ingot": ("block", ("iron_ore", "deepslate_iron_ore")),
    "iron_helmet": ("block", ("iron_ore", "deepslate_iron_ore")),
    "iron_chestplate": ("block", ("iron_ore", "deepslate_iron_ore")),
    "iron_sword": ("block", ("iron_ore", "deepslate_iron_ore")),
    "shield": ("block", ("iron_ore", "deepslate_iron_ore")),
    "wooden_sword": ("block", ("oak_log", "birch_log", "spruce_log", "jungle_log",
                               "acacia_log", "dark_oak_log", "mangrove_log", "cherry_log")),
    "wooden_pickaxe": ("block", ("oak_log", "birch_log", "spruce_log", "jungle_log",
                                 "acacia_log", "dark_oak_log", "mangrove_log", "cherry_log")),
    "stone_sword": ("block", ("stone", "cobblestone", "deepslate")),
    "stone_pickaxe": ("block", ("stone", "cobblestone", "deepslate")),
    "crafting_table": ("block", ("oak_log", "birch_log", "spruce_log", "jungle_log",
                                 "acacia_log", "dark_oak_log", "mangrove_log", "cherry_log")),
    "torch": ("any", ("coal", "charcoal")),
    "coal": ("block", ("coal_ore", "deepslate_coal_ore")),
}


def feasible(reg: Registry, item: str, *, radius: int = 48) -> tuple[bool, str]:
    """这东西现在**做不做得出来**。返回 (可行, 一句话理由)。

    只做便宜检查（一次实体扫描 / 一次方块扫描 / 一次背包计数），查不到就直接说不行，
    让上层换方案 —— 而不是闷头跑 100 秒再失败（实测 `@get leather_chestplate` 在没有牛的地方
    就是这样空转的）。
    """
    short = str(item).split(":")[-1]
    have = actions.count_item(reg, short)
    if have >= 1:
        return True, f"已有 {short}×{have}"
    kind, need = NEEDS.get(short, ("any", ()))
    if kind == "entity":
        try:
            ents = actions.nearby_entities(reg, limit=48)
        except Exception as e:
            return True, f"实体扫描失败({type(e).__name__})，按可行处理"
        seen = sorted({str(e.get("type")).split(":")[-1] for e in ents})
        hit = [t for t in need if t in seen]
        if hit:
            return True, f"附近有 {hit[0]}"
        if short.startswith("leather"):
            # 有皮甲材料就直接做；没有就明确说不行（这条就是 operator 举的例子）
            if actions.count_item(reg, "leather") >= 2:
                return True, "背包里有皮革"
            return False, f"附近 48 格没有牛（最近实体：{','.join(seen[:6]) or '无'}）"
        return False, f"附近没有 {need}（看到：{','.join(seen[:6]) or '无'}）"
    if kind == "block":
        for name in need:
            try:
                r = reg.invoke("scanBlocks", {"name": name, "radius": radius, "limit": 3})
                d = r.data if r.ok else {}
                if isinstance(d, dict) and d.get("_raw"):
                    d = d["_raw"]
                arr = d if isinstance(d, list) else (d.get("blocks") if isinstance(d, dict) else [])
                if arr:
                    return True, f"附近有 {name}"
            except Exception:
                continue
        inv = {n: actions.count_item(reg, n) for n in ("oak_log", "cobblestone", "coal",
                                                       "charcoal", "iron_ore", "raw_iron")}
        if any(v > 0 for v in inv.values()):
            return True, f"背包有原料 {inv}"
        return False, f"附近 {radius} 格没找到 {need[0]}，背包也没原料"
    if kind == "any":
        if any(actions.count_item(reg, n) >= 1 for n in need):
            return True, "已有原料"
        return True, "无法预判（按可行处理，靠超时兜底）"
    return True, "无预检规则"


def pick_alternative(reg: Registry, want: str) -> tuple[str | None, list[dict]]:
    """按"同样能活下来"的目标排替代方案，返回第一个可行的 + 全部候选的判定。

    这就是 operator 说的"变通"：没有牛就别追皮甲 —— 退到铁（要矿）→ 再退到木剑 + 不硬打。
    """
    LADDER = {
        "leather_chestplate": ["leather_chestplate", "iron_chestplate", "wooden_sword"],
        "leather_helmet": ["leather_helmet", "iron_helmet", "wooden_sword"],
        "wooden_sword": ["wooden_sword", "stone_sword"],
        "wooden_pickaxe": ["wooden_pickaxe", "stone_pickaxe"],
        "iron_sword": ["iron_sword", "stone_sword", "wooden_sword"],
    }
    trace = []
    for cand in LADDER.get(want, [want]):
        ok, why = feasible(reg, cand)
        trace.append({"item": cand, "ok": ok, "why": why})
        if ok:
            return cand, trace
    return None, trace


# ── 监督器：给一个任务，盯到它成/败 ─────────────────────────────────────────
def ensure_gear(reg: Registry, *, log=None, want: str = "wooden_sword",
                budget_s: float = 120.0) -> dict:
    """护主前配装：**先查可行性，再按阶梯换方案**（operator 2026-10-29 的要求）。

    旧版只会"没剑就造剑"，在没有树/没有牛的地方会闷头空转 100 秒然后失败。
    现在：`pick_alternative()` 逐级判断（皮甲→铁甲→木剑→石剑），第一件可行才去做；
    全都不行 ⇒ `mode=evade`，明确告诉上层"别硬打"，而不是拿拳头去打僵尸。
    `obtain()` 是**异步**的（返回时任务才刚排进去），所以这里要轮询背包。
    """
    out: dict = {"want": want, "picked": None, "trace": [], "made": False, "sec": 0.0}
    t0 = time.time()
    item, trace = pick_alternative(reg, want)
    out["trace"] = trace
    if item is None:
        out.update({"ok": False, "mode": "evade", "have": 0,
                    "why": "没有可行装备（附近既没材料也没原料）⇒ 不硬打，贴身保命"})
        out["sec"] = round(time.time() - t0, 1)
        if log:
            log({"ev": "ensure_gear", **out})
        return out
    out["picked"] = item
    if actions.count_item(reg, item) <= 0:
        try:
            hook.obtain(reg, item, 1)
        except Exception as e:
            out["err"] = f"{type(e).__name__}: {e}"[:120]
        t1 = time.time()
        while time.time() - t1 < budget_s:
            time.sleep(3.0)
            if actions.count_item(reg, item) >= 1:
                break
    out["have"] = actions.count_item(reg, item)
    out["made"] = out["have"] >= 1
    out["ok"] = out["made"]
    out["mode"] = "fight" if out["made"] else "evade"
    out["sec"] = round(time.time() - t0, 1)
    if log:
        log({"ev": "ensure_gear", **out})
    return out


# 向后兼容（gate 里检查这个名字）
_ensure_gear = ensure_gear


def execute(reg: Registry, spec: TaskSpec, *, player: str = "Xiaorun",
            cancel=None, log=None, on_progress=None) -> dict:
    """执行一个上层任务并**盯到底**：进度可见、卡死会救、到点必停、验收看世界。"""
    log = log or (lambda o: None)
    t0 = time.time()
    hook.take_control(reg, False)          # 先还给它自主权（它自己会规划）
    started = False
    if spec.lever == "obtain":
        r = hook.obtain(reg, spec.args["item"], spec.args["count"], on_progress=on_progress)
        started = bool(r.get("ok"))
    elif spec.lever == "give":
        r = hook.run(reg, f"give {spec.args['player']} {spec.args['item']} {spec.args['count']}")
        started = bool(r.get("ok"))
    elif spec.lever == "clear_near":
        # 2026-10-29（operator）：**先配装再放怪**；没装备就贴身保命，不拿拳头去打。
        gear = ensure_gear(reg, log=log, want="wooden_sword",
                           budget_s=min(150.0, spec.budget_s * 1.5))
        log({"ev": "guard_gear", "mode": gear.get("mode"), "picked": gear.get("picked"),
             "made": gear.get("made"), "trace": gear.get("trace")})
        if gear.get("mode") == "evade":
            pp = _player_pos(reg, spec.args["player"])
            g = (actions.goto(reg, int(pp[0]), int(pp[1]), int(pp[2]), timeout=20.0, tol=2.6)
                 if pp else {"ok": False})
            r = {"ok": False, "why": "没有可用武器 ⇒ 改为贴身保命，不硬打",
                 "gear": gear, "goto": g}
            started = False
        else:
            r = actions.clear_near(reg, _player_pos(reg, spec.args["player"]),
                                   radius=spec.args.get("radius", 8.0),
                                   budget_s=spec.budget_s * 0.8, cancel=cancel, log=log)
            started = True
    elif spec.lever == "follow":
        pp = _player_pos(reg, spec.args["player"])
        r = actions.goto(reg, int(pp[0]), int(pp[1]), int(pp[2]),
                         timeout=spec.budget_s * 0.8, tol=2.6, cancel=cancel) if pp else {"ok": False}
        started = bool(r.get("ok"))
    elif spec.lever == "mine":
        r = actions.mine(reg, spec.args["target"], cancel=cancel)
        started = bool(r.get("ok"))
    else:
        return {"ok": False, "why": f"未知杠杆 {spec.lever}", "spec": spec.name}
    log({"ev": "task_start", "task": spec.name, "goal": spec.goal, "started": started,
         "lever": spec.lever})

    # 监督循环
    last_sig, last_sig_at = "", time.time()
    acc_ok, acc_why = False, "还没开始验收"
    while time.time() - t0 < spec.budget_s:
        if cancel is not None and cancel():
            return {"ok": False, "why": "已取消", "sec": round(time.time() - t0, 1),
                    "task": spec.name}
        if spec.accept in ACCEPT:
            acc_ok, acc_why = ACCEPT[spec.accept](reg, **spec.accept_args)
            if acc_ok:
                out = {"ok": True, "task": spec.name, "goal": spec.goal,
                       "evidence": acc_why, "sec": round(time.time() - t0, 1),
                       "how": spec.lever}
                log({"ev": "task_done", **out})
                return out
        # 卡死判据：任务链 63 秒一字未变，且验收没动
        sig = " | ".join(actions.chain(reg))[:150]
        if sig != last_sig:
            last_sig, last_sig_at = sig, time.time()
        elif time.time() - last_sig_at > 63.0 and not sig:
            log({"ev": "task_stuck", "task": spec.name, "sig": sig[:60]})
            r2 = actions.unstick(reg, log=log)
            log({"ev": "task_unstick", "ok": r2.get("ok"), "moved": r2.get("moved")})
            last_sig_at = time.time()
        time.sleep(0.8)
    out = {"ok": False, "task": spec.name, "why": f"预算 {int(spec.budget_s)} 秒用完",
           "last": acc_why, "sec": round(time.time() - t0, 1)}
    log({"ev": "task_timeout", **out})
    return out


def _player_pos(reg: Registry, player: str):
    st = reg.invoke("getGameState", {}).data or {}
    pl = next((p for p in (st.get("players") or [])
               if str(p.get("name", "")).lower() == player.lower()), None)
    return reg.pos(pl) if pl else None
