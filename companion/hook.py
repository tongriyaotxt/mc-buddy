#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/hook.py —— 把 AltoClef 的**决策层**当成我们的能力面（不重造轮子）

operator 2026-10-27：「很多东西 altoclef 都可以实现，你只需要对他的决策进行 hook 一下……
背包啥的确实需要你写」。

AltoClef 自己就有（我们以前白写了 / 还没用上的）：

| 我们要的 | AltoClef 里已有的 |
|---|---|
| 合成某物（木板→木棍→剑→工作台→镐…） | `TaskCatalogue.getItemTask(name, n)` → `ResourceTask` |
| 把东西交给玩家 | 命令 `give <player> <items>` |
| 把东西存进箱子 | 命令 `deposit` / `stash <x1 y1 z1 x2 y2 z2>` |
| 找结构 / 乱走 / 暂停 | 命令 `locate_structure` / `wander` / `pause` |
| 清怪 / 吃饭 / 自卫 | 命令 `hero` / `food` / `selfcare` |

所以这一层只做三件事：
  1. **发现**：`commands()` / `resources()` —— 把它的能力表原样交给 AI（我们不维护那张表）；
  2. **注入**：`obtain()` / `run()` —— 把任务塞进**它自己的**用户任务链（失败重试也归它）；
  3. **夺权**：`take_control(True)` —— `TaskRunner.disable()`，让它的自主链别跟我们抢。

降级：老 jar（没有 hook 工具）时所有函数返回 `{ok:False, why:"hook 未安装"}`，
调用方必须自己回落（`commander` 里就是这么写的）。
"""
from __future__ import annotations

from .registry import Registry

HOOK_TOOLS = {"listCommands", "obtainableItems", "obtainItem", "runCommandText",
              "takeControl", "userTask"}


def available(reg: Registry) -> bool:
    """这颗身体有没有装上 hook 层（老 jar 没有）。"""
    try:
        return bool(HOOK_TOOLS.issubset(set(reg.tools)))
    except Exception:
        return False


def _call(reg: Registry, name: str, args: dict | None = None) -> dict:
    if not available(reg):
        return {"ok": False, "why": "hook 未安装（jar 还是旧的）", "tool": name}
    r = reg.invoke(name, args or {})
    d = r.data
    # body.as_obj() 对**数组**返回会包成 {"_raw": [...]}，对纯字符串会包成 {"_text": "..."}；
    # 这两种都要在这里摊平，否则上层拿到的是 0 条命令/0 种资源（2026-10-27 实测踩过）。
    if isinstance(d, dict):
        if isinstance(d.get("_raw"), list):
            return {"ok": True, "items": d["_raw"]}
        if "_text" in d:
            return {"ok": True, "data": d["_text"]}
        return d if "ok" in d else {"ok": bool(r.ok), **d}
    if isinstance(d, list):
        return {"ok": True, "items": d}
    return {"ok": bool(r.ok), "data": d, "err": r.err[:160]}


# ── 发现能力面 ─────────────────────────────────────────────────────────────
def commands(reg: Registry) -> list[dict]:
    """AltoClef 全部内建命令（name/description/参数个数）。"""
    d = _call(reg, "listCommands")
    return d.get("items") or []


def resources(reg: Registry) -> list[str]:
    """它自己的资源目录：**这个 bot 到底能弄到哪些东西**。"""
    d = _call(reg, "obtainableItems")
    return d.get("items") or []


def can_obtain(reg: Registry, item: str) -> bool:
    short = item.split(":")[-1].lower()
    return short in {str(x).split(":")[-1].lower() for x in resources(reg)}


# ── 注入任务 ───────────────────────────────────────────────────────────────
def obtain(reg: Registry, item: str, count: int = 1, *, on_progress=None) -> dict:
    """让 AltoClef 自己去弄到某物（采集/合成/烧炼/找台子都在它的 ResourceTask 里）。

    走**流式**调用：长活跑的过程中服务端会推 `notifications/progress`
    （message 就是它自己的任务链），`on_progress` 能实时看到"它现在在干什么"。
    老服务端不支持进度时会自动退化成普通调用。
    """
    if not available(reg):
        return {"ok": False, "why": "hook 未安装（jar 还是旧的）"}
    try:
        d = reg.body.call_stream("obtainItem",
                                 {"item": item.split(":")[-1], "count": int(count)},
                                 on_progress=on_progress, timeout=300.0)
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:140]}
    if isinstance(d, dict):
        return d if "ok" in d else {"ok": bool(d), **d}
    return {"ok": bool(d), "data": d}


def run(reg: Registry, cmd: str) -> dict:
    """跑任意内建命令（`give` / `deposit` / `stash` / `wander`…）。"""
    return _call(reg, "runCommandText", {"cmd": cmd})


def user_task(reg: Registry) -> str:
    """当前用户任务（空 = 闲）。AI 靠它判断"它办完了没"。"""
    d = _call(reg, "userTask")
    return str(d.get("data") or d.get("task") or "")


def pickup_from_container(reg: Registry, pos, item: str, count: int = 1) -> dict:
    """从 (x,y,z) 的容器里按数量取东西 —— 走 AltoClef 的 PickupFromContainerTask
    （它负责走过去、开界面、点格子、背包满了先腾格子；我们不重写）。"""
    x, y, z = (int(round(float(v))) for v in pos)
    return _call(reg, "pickupFromContainer",
                 {"x": x, "y": y, "z": z, "item": item.split(":")[-1],
                  "count": int(count)})


# ── 抢控制权 ───────────────────────────────────────────────────────────────
def take_control(reg: Registry, on: bool = True) -> dict:
    """on=True：停掉它的自主链（我们说了算）；on=False：还给它。"""
    return _call(reg, "takeControl", {"on": bool(on)})
