#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/goal_graph.py —— 把 AltoClef 自己的"目标分解图"暴露给 AI

operator 2026-10-29：「我直接用 ai 下命令的时候很多时候会出问题不知道怎么弄，
这时候你需要用一下 altoclef 的那个目标分解图来实现我们的目标。」

问题出在哪：
  模型看到的是 85 个工具名，但它**不知道这座身体的资源依赖图**（木头→木板→棍→台子→镐→挖铁→烧…）。
  于是它对"给我弄套铁装备""做张床""搭个棚子"这类目标会乱试：调一堆底层工具、反复失败、最后编一句瞎话。

AltoClef 本来就有这张图：
  · `obtainableItems()`  683 种"它自己知道怎么弄到"的资源（能/不能的第一道判据）
  · `TaskCatalogue.getItemTask(name, n)` 会**现搭一棵任务树**（ResourceTask/MineAndCollect…）
  · 这棵树一旦开始跑，`userTask()` / `taskChain()` 就能读到它的**文字分解**
    （例如 `<Get 3 oak_log>` → `<Chop 3 oak_log>` → `<Mine block oak_log>`）

所以这里的做法很直接，**不重造计划器**：
  1) 先问"它到底会不会做这个"（obtainableItems）——不会就当场说不会；
  2) 让它自己**开始跑**这个目标（obtainItem 是异步的，会建树）；
  3) 立刻读回任务树文字，作为"目标分解图"喂给模型（模型据此解释/监督/纠偏，而不是瞎试）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from companion import actions, hook

ROOT = Path(__file__).resolve().parent.parent
_STATE = ROOT / "runs" / "state" / "last_plan.json"


def _remember(plan: dict, *, source: str = "") -> None:
    """把"它打算怎么做"落盘（面板/trace 之外的第三条可见通道）。

    原子写（先 .tmp 再 replace）：面板随时读，永远拿到完整 JSON。
    """
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "source": source, **plan}
        tmp = _STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(_STATE)
    except Exception:
        pass


def can_obtain(reg, item: str) -> tuple[bool, str]:
    """它自己知不知道怎么弄到这个（683 种资源清单）。"""
    short = str(item).split(":")[-1]
    try:
        names = hook.resources(reg)
    except Exception as e:
        return True, f"资源清单读不到（{type(e).__name__}），按可行处理"
    if not names:
        return True, "资源清单为空（按可行处理）"
    pool = {str(n).split(":")[-1].lower() for n in names}
    if short.lower() in pool:
        return True, f"{short} 在它的可获取清单里（共 {len(pool)} 种）"
    # 方块/原料类常常不在"可获取物品"里，但可以直接挖
    try:
        r = reg.invoke("scanBlocks", {"name": short, "radius": 32, "limit": 1})
        if r.ok and r.data:
            return True, f"{short} 不在物品清单，但附近能直接挖到"
    except Exception:
        pass
    return False, f"{short} 不在它的可获取清单里（{len(pool)} 种），也不像能直接挖的方块"


def decompose(reg, item: str, count: int = 1, *, wait_s: float = 3.0,
              log=None, allow_substitute: bool = True) -> dict:
    """开始一个目标并读回 AltoClef 的任务树 —— 这就是"目标分解图"。

    返回 {ok, item, count, tree:[...], raw, reason, substituted, asked}。
    ① 不可行时**自动降级**（皮甲→铁甲→木剑…），并且把"换了什么"写进返回值 ——
       调用方必须把这件事告诉玩家（不许偷偷换）。
    ⚠️ 它**真的会开始干活**（obtainItem 是异步建树 + 执行）；不想要它继续跑就 cancel。
    """
    asked = str(item)
    ok, why = can_obtain(reg, item)
    substituted = False
    if not ok and allow_substitute:
        try:
            from companion import tasks
            alt, trace = tasks.pick_alternative(reg, asked)
        except Exception:
            alt, trace = None, []
        if alt and alt != asked:
            if log:
                log({"ev": "goal_substitute", "asked": asked, "use": alt, "trace": trace})
            item, substituted = alt, True
            ok, why = True, f"改用 {alt}（{asked} 做不了：原判据是「{why}」）"
    if not ok:
        if log:
            log({"ev": "goal_plan_denied", "item": asked, "why": why})
        out = {"ok": False, "item": asked, "count": count, "tree": [], "reason": why,
               "substituted": False, "asked": asked}
        _remember(out, source="denied")
        return out
    r = hook.obtain(reg, item, int(count))
    tree: list[str] = []
    raw = ""
    t0 = time.time()
    while time.time() - t0 < wait_s:
        try:
            raw = hook.user_task(reg) or ""
        except Exception:
            raw = ""
        if not raw:
            try:
                raw = " | ".join(actions.chain(reg))
            except Exception:
                raw = ""
        if raw:
            tree = [x.strip() for x in _split_tree(raw) if x.strip()]
            break
        time.sleep(0.3)
    out = {"ok": bool(raw), "item": item, "count": int(count), "tree": tree[:8],
           "raw": raw[:600], "started": bool(r.get("ok")), "sec": round(time.time() - t0, 1),
           "substituted": substituted, "asked": asked, "reason": "" if substituted else why}
    _remember(out, source="plan")
    if log:
        log({"ev": "goal_plan", "item": item, "asked": asked, "count": count,
             "ok": out["ok"], "substituted": substituted,
             "tree": tree[:5], "started": out["started"]})
    return out


def _split_tree(raw: str) -> list[str]:
    """把 `<Get 3 oak_log> ... <Chop ...>` 这种树文字拆成节点（保序、去重）。"""
    txt = str(raw)
    for ch in "<>":
        txt = txt.replace(ch, "\n")
    parts = [p.strip(" -|\t") for p in txt.replace("  ", " ").split("\n")]
    seen, out = set(), []
    for p in parts:
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def prompt_block(plan: dict) -> str:
    """给模型看的一小段（别把整棵树塞进 prompt）。"""
    if plan.get("substituted"):
        head = (f"【替换】你要的是 {plan.get('asked')}，但做不到（{plan.get('reason') or '材料不在'}）；"
                f"**已经改用 {plan.get('item')}**。回答玩家时必须说清换了什么、为什么。\n")
    else:
        head = ""
    if not plan.get("ok"):
        return (f"【它自己不会做】{plan.get('reason', '')} —— 直接告诉玩家做不到，别硬试。")
    lines = plan.get("tree") or []
    body = " → ".join(lines[:5]) if lines else (plan.get("raw") or "")[:200]
    return (head + "【它自己的分解】" + body +
            "\n（这是这座身体自己拆的步骤：照着监督/解释即可，不要用底层工具重造这些步骤。）")


# ── 自动化前置（2026-10-29 operator：这部分可以自动化）──────────────────────
# 不让模型"想起来"去查分解图 —— **在它看到问题之前**，我们先把分解图/可行性算好塞进去。
GOAL_VERBS = ("弄", "做", "造", "建", "搞", "要", "给我", "来一", "来套", "准备", "弄点", "整")
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 60.0


def wants_goal(text: str) -> bool:
    t = str(text or "")
    return any(v in t for v in GOAL_VERBS)


def auto_plan(reg, text: str, *, log=None) -> dict | None:
    """自动前置：从人话里认出"要一样东西"，先把可行性/分解图算好。

    两种强度（**只在明确是"去弄/去做"时才真的开始任务**，避免聊天里提一句就乱跑）：
      · 只是提到（"铁镐好用吗"）  → 只查 `obtainableItems`（不算副作用）
      · 明确要去做（"帮我弄把铁镐"）→ 开始任务并读回任务树（分解图）
    """
    from companion import nlu
    text = str(text or "")
    item = nlu.find_item(text)
    if not item:
        return None
    cache_key = (item, wants_goal(text))
    hit = _CACHE.get(str(cache_key))
    now = time.time()
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    if not wants_goal(text):
        ok, why = can_obtain(reg, item)
        plan = {"mode": "feasible", "ok": ok, "item": item, "reason": why, "tree": []}
    else:
        plan = {"mode": "plan", **decompose(reg, item, 1, log=log)}
    _CACHE[str(cache_key)] = (now, plan)
    if log:
        log({"ev": "auto_plan", "item": item, "mode": plan.get("mode"),
             "ok": plan.get("ok"), "tree": (plan.get("tree") or [])[:3]})
    return plan

