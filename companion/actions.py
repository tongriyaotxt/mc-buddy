#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/actions.py —— 动作层：把"想法"落成"身体真做到了"

为什么要有这一层（2026-10-26 三次实测的结论）：
  指挥层（commander）只该管"做什么"，可**做没做成必须有人核验**。
  今晚踩过的坑全都是"调用返回 ok，但世界没变"：

    · gotoXYZ 返回 ok，位移 0.00（FastNavigator 放弃路线）
    · mineBlock 返回 ok，方块还在（眼距 4.66 > 4.5 ⇒ PathExecutor 直接 abort）
    · dropItem 返回 ok，玩家没拿到（丢在自己脚下）
    · gesture 返回 ok，但触发判据从来没命中过

  所以这一层的每个函数都遵守同一条契约：
      **看世界 → 动作 → 再看世界 → 只有世界变了才算 ok**。
  返回的 dict 里带 `steps`，每一步都可以回放（trace.jsonl 会原样落盘）。

三根杠杆（都是 AltoClef 原生，不是我们自己造的物理）：
  1. 站位：gotoXYZ / gotoFar（tungsten 的物理腿）
  2. 破坏：mineBlock（tungsten 的 break queue —— 不要准星、自己找面、带工具切换）
  3. 命令：ExecuteCommand <@cmd>（把 AltoClef 自己的任务链当"长活"用）

⚠️ 两条硬约束（源码实证，写进这里免得下次又踩）：
  · 眼到块心 > 4.5 一律 abort（tungsten/path/PathExecutor.java:860）⇒ 我们按 **4.2** 站定。
  · 同时只允许一个瞄准者（:853）⇒ 挖矿期间**绝不下发** `@mineblock` / `@get` 这类会抢准星的任务。
"""
from __future__ import annotations

import math
import re
import time
from typing import Iterable

from .body import truthy
from .registry import Registry

EYE_H = 1.62            # 视高：脚 + 1.62
REACH_SAFE = 4.2        # 留 0.3 余量给 4.5 的硬门
REACH_HARD = 4.5


# ── 读世界 ──────────────────────────────────────────────────────────────────
def pos_of(reg: Registry) -> tuple[float, float, float] | None:
    st = reg.invoke("getGameState", {})
    if not st.ok or not isinstance(st.data, dict):
        return None
    return reg.pos(st.data.get("self") or {})


def eye_of(p) -> tuple[float, float, float]:
    return (p[0], p[1] + EYE_H, p[2])


def block_id(reg: Registry, x, y, z) -> str:
    r = reg.invoke("getBlockAt", {"x": int(x), "y": int(y), "z": int(z)})
    if not r.ok or not isinstance(r.data, dict):
        return "?"
    bid = r.data.get("id")
    if bid:
        return str(bid)
    return "minecraft:air" if truthy(r.data.get("is_air")) else "?"


def is_air(reg: Registry, x, y, z) -> bool:
    return block_id(reg, x, y, z) in ("minecraft:air", "minecraft:cave_air", "minecraft:void_air")


def center(x, y, z) -> tuple[float, float, float]:
    return (x + 0.5, y + 0.5, z + 0.5)


def dist(a, b) -> float:
    return math.dist(a, b)


# ── 站位的候选（这就是"两跳站定"的第一跳） ─────────────────────────────────
def stand_candidates(reg: Registry, target, cur=None, radius: int = 2) -> list[dict]:
    """找出所有"能站 + 眼睛够得着目标块"的格子，按(眼距, 走过来的距离)排序。"""
    tx, ty, tz = target
    tc = center(tx, ty, tz)
    out: list[dict] = []
    for dx in range(-radius, radius + 1):
        for dz in range(-radius, radius + 1):
            for dy in (-1, 0, 1, 2):
                x, y, z = tx + dx, ty + dy, tz + dz
                if (x, y, z) == (tx, ty, tz):
                    continue
                if is_air(reg, x, y, z) is False:
                    continue
                if is_air(reg, x, y + 1, z) is False:
                    continue
                if is_air(reg, x, y - 1, z) is True:      # 脚下是空气 = 站不住
                    continue
                e = (x + 0.5, y + EYE_H, z + 0.5)
                d = dist(e, tc)
                if d > REACH_SAFE:
                    continue
                walk = dist(eye_of(cur), e) if cur else 0.0
                out.append({"p": (x, y, z), "eye_dist": round(d, 3), "walk": round(walk, 2)})
    out.sort(key=lambda c: (c["eye_dist"] * 0.5 + c["walk"] * 0.5))
    return out


# ── 导航：下发 + 等到位（不信任返回值，只信任坐标） ──────────────────────────
def goto(reg: Registry, x, y, z, *, timeout: float = 25.0, tol: float = 1.6,
         cancel=None) -> dict:
    """走到某格。三级腿，谁也替不了谁：

      1. `gotoXYZ` / `gotoFar` —— tungsten 的物理腿，快、准，但**垂直落差大时走不上去**
         （实测：玩家在头顶 4 格，gotoXYZ 三次都停在原地不动）。
      2. `@goto` —— AltoClef 自己的任务链，会破障、搭柱、爬台阶（慢，但能上去）。
      3. 腿都失败就如实返回 ok=false，让上层降级，绝不假装到了。

    判据永远是**坐标**，不是返回值。
    """
    tgt = (x + 0.5, y, z + 0.5)
    cur = pos_of(reg)
    far = cur and dist(cur, tgt) > 20.0
    if far:
        r = reg.invoke("gotoFar", {"x": int(x), "y": int(y), "z": int(z), "horizon": 32})
    else:
        r = reg.invoke("gotoXYZ", {"x": int(x), "y": int(y), "z": int(z)})
    t0 = time.time()
    best = 1e9
    first_leg = timeout * 0.6
    used_goto_cmd = False
    while time.time() - t0 < timeout:
        if cancel is not None and cancel():
            # 2026-10-27：作业要能被"停"打断。以前只能等它自己超时（最长 110 秒），
            # 期间说一句话都要排队 —— 这就是面板里"叫它不理"的第二个真因。
            return {"ok": False, "why": "已取消", "sec": round(time.time() - t0, 2),
                    "at": pos_of(reg), "leg": "@goto" if used_goto_cmd else "gotoXYZ"}
        p = pos_of(reg)
        if p:
            d = dist(p, tgt)
            best = min(best, d)
            if d <= tol:
                return {"ok": True, "sec": round(time.time() - t0, 2), "at": p,
                        "dist": round(d, 2), "sent": r.ok, "err": r.err,
                        "leg": "@goto" if used_goto_cmd else "gotoXYZ"}
        if not used_goto_cmd and time.time() - t0 > first_leg:
            used_goto_cmd = True
            reg.invoke("run_command", {"cmd": f"@goto {int(x)} {int(y)} {int(z)}"})
        time.sleep(0.35)
    p = pos_of(reg)
    return {"ok": False, "sec": round(time.time() - t0, 2), "at": p,
            "dist": round(dist(p, tgt), 2) if p else None,
            "best": round(best, 2), "sent": r.ok, "err": r.err, "why": "没走到",
            "leg": "@goto" if used_goto_cmd else "gotoXYZ"}


# ── 破坏：break queue 路线（不需要准星） ────────────────────────────────────
def _break_queue(reg: Registry, target, *, timeout: float = 30.0, cancel=None) -> dict:
    tx, ty, tz = target
    # 2026-10-27：挖之前先看"准星有没有被别人拿着"（导航腿在跑 ⇒ 挖矿把瞄准让给它）
    if path_busy(reg):
        cleared = stop_all(reg)
        if cleared.get("ok"):
            pass
    r = reg.invoke("mineBlock", {"x": int(tx), "y": int(ty), "z": int(tz)})
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        if cancel is not None and cancel():
            return {"ok": False, "why": "已取消", "sec": round(time.time() - t0, 2),
                    "after": block_id(reg, tx, ty, tz)}
        cur = block_id(reg, tx, ty, tz)
        if cur.endswith(":air"):
            return {"ok": True, "sec": round(time.time() - t0, 2),
                    "sent": r.ok, "err": r.err, "status": last}
        last = reg.invoke("mineStatus", {}).data or {}
        time.sleep(0.4)
    # 超时但"它说自己在挖" ⇒ 典型的"瞄准被导航腿拿走"：清场再试一次（短超时）
    if isinstance(last, dict) and truthy(last.get("mining")):
        cleared = stop_all(reg)
        reg.invoke("mineBlock", {"x": int(tx), "y": int(ty), "z": int(tz)})
        t1 = time.time()
        while time.time() - t1 < 8.0:
            if block_id(reg, tx, ty, tz).endswith(":air"):
                return {"ok": True, "sec": round(time.time() - t0, 2), "retried": True,
                        "cleared": cleared}
            time.sleep(0.4)
    return {"ok": False, "sec": round(time.time() - t0, 2), "after": block_id(reg, tx, ty, tz),
            "sent": r.ok, "err": r.err, "status": last,
            "busy_path": path_busy(reg), "why": "方块没变成空气"}


def mine(reg: Registry, target, *, tries: int = 3, log=None, cancel=None) -> dict:
    """挖掉一格：先站到位（眼距 ≤4.2），再走 break queue，最后核验方块真变空气。

    这是 look_mine / 采集类能力的**唯一**执行体。失败时把每一步都留下来。"""
    tx, ty, tz = (int(v) for v in target)
    steps: list[dict] = []
    if block_id(reg, tx, ty, tz).endswith(":air"):
        return {"ok": True, "already": True, "target": [tx, ty, tz], "steps": steps}

    # 0) 眼距已经够得着就直接挖（省掉一次 20 秒的来回走）
    cur = pos_of(reg)
    if cur:
        d0 = dist(eye_of(cur), center(tx, ty, tz))
        if d0 <= REACH_SAFE:
            steps.append({"ev": "already_in_reach", "eye_dist": round(d0, 3)})
            blocked = aim_blocked(reg)
            if blocked:
                steps.append({"ev": "blocked_by_entity", "note": "准星先打到实体，先换个角度"})
            b0 = _break_queue(reg, (tx, ty, tz), cancel=cancel,
                              timeout=8.0 if blocked else 30.0)
            steps.append({"ev": "break", "ok": b0.get("ok"), "sec": b0.get("sec"),
                          "status": b0.get("status"), "after": b0.get("after")})
            if b0["ok"]:
                return {"ok": True, "target": [tx, ty, tz], "eye_dist": round(d0, 3),
                        "attempt": 0, "steps": steps}

    cands = stand_candidates(reg, (tx, ty, tz), cur=cur)
    if not cands:
        steps.append({"ev": "no_stand", "note": "3 格内没有既站得住、眼睛又够得着的点"})
        # 兜底：站到目标正下方一格旁边，靠 break queue 自己的 reach 判定
        return {"ok": False, "target": [tx, ty, tz], "steps": steps, "why": "无站位"}

    tried: set[tuple[int, int, int]] = set()
    for attempt in range(tries):
        if cancel is not None and cancel():
            return {"ok": False, "target": [tx, ty, tz], "steps": steps, "why": "已取消"}
        cand = next((c for c in cands if c["p"] not in tried), None)
        if cand is None:
            break
        tried.add(cand["p"])
        sx, sy, sz = cand["p"]
        g = goto(reg, sx, sy, sz, timeout=22.0, cancel=cancel)
        steps.append({"ev": "stand", "at": [sx, sy, sz], **{k: g[k] for k in ("ok", "sec", "dist") if k in g}})
        if not g["ok"]:
            continue
        p = pos_of(reg)
        e = eye_of(p) if p else None
        d = dist(e, center(tx, ty, tz)) if e else 99.0
        steps.append({"ev": "reach", "eye_dist": round(d, 3)})
        if d > REACH_HARD:
            continue
        b = _break_queue(reg, (tx, ty, tz), cancel=cancel)
        steps.append({"ev": "break", **{k: b.get(k) for k in ("ok", "sec") if k in b},
                      "status": b.get("status"), "after": b.get("after")})
        if b["ok"]:
            return {"ok": True, "target": [tx, ty, tz], "eye_dist": round(d, 3),
                    "attempt": attempt + 1, "steps": steps}
        if log:
            log({"ev": "mine_try_failed", "attempt": attempt + 1, "d": round(d, 3)})
    return {"ok": False, "target": [tx, ty, tz], "steps": steps, "why": "三次都没挖掉"}


# ── 界面守护：GUI 开着 = 准星死 = 挖矿/导航全废 ─────────────────────────────
def screen_state(reg: Registry) -> dict:
    r = reg.invoke("getOpenScreen", {})
    return r.data if r.ok and isinstance(r.data, dict) else {}


def ensure_no_screen(reg: Registry, *, log=None, tries: int = 3) -> dict:
    """GUI 开着就发 ESC 关掉（用 win32 直发按键，不抢人手）。

    实测（2026-10-26）：`WindMouseRotation.applyRenderStep` 在 `currentScreen != null` 时
    每帧直接 return ⇒ 准星永不转 ⇒ `crosshairTarget` 永不等于目标 ⇒ 攻击键永不按下
    ⇒ chat 报 `Mining aborted: ticks=302 … progress=-0.10`。
    关掉界面后，同一批目标 `mineBlock` 立刻 2/2 成功。
    """
    st = screen_state(reg)
    if not st.get("open"):
        return {"open": False, "title": st.get("title")}
    # ⛔ 2026-10-30（W6-2 长跑血证）：**死亡界面用 ESC 关不掉**（原版那个界面只有 Respawn）。
    # 实测：客户端死在 "You Died!" 上，指挥层每 2 秒发一次 ESC ⇒ 界面永远在 ⇒ 准星永远是死的
    # ⇒ 外面看就是"它还活着，但什么都不干"。正确动作是调 `respawnPlayer`（它自己 requestRespawn 并关屏）。
    _title = str(st.get("title") or "")
    if st.get("is_death") or any(k in _title.lower() for k in ("died", "死亡", "你死了")):
        try:
            r = reg.invoke("respawnPlayer", {})
            time.sleep(1.5)
            st2 = screen_state(reg)
            if log:
                log({"ev": "death_respawn", "ok": bool(r.ok), "title": _title,
                     "still_open": bool(st2.get("open"))})
            return {"open": bool(st2.get("open")), "title": st2.get("title"),
                    "method": "respawn"}
        except Exception as e:
            return {"open": True, "title": _title,
                    "err": f"respawn: {type(e).__name__}: {e}"[:120]}
    hwnd = None
    try:
        import sys as _sys
        from pathlib import Path as _P
        _tools = _P(__file__).resolve().parent.parent / "tools"
        if str(_tools) not in _sys.path:
            _sys.path.insert(0, str(_tools))
        import win32_mc as _w32
        for win in _w32.list_windows():
            if "Minecraft" in win.get("title", "") and abs(win["w"] - 853) <= 60 \
                    and abs(win["h"] - 480) <= 60:
                hwnd = win["hwnd"]
        if hwnd is None:
            return {"open": True, "title": st.get("title"), "err": "找不到陪玩窗口"}
        for i in range(tries):
            # 2026-10-27（operator）：**不许抢焦点**。以前这里是 focus(hwnd)+全局 SendInput，
            # 每关一次界面就把人的焦点抢走一次。GLFW 的按键走窗口消息，PostMessage 到后台窗口
            # 一样能收到，前台窗口完全不动。
            # ⚠️ 2026-10-30 血证：这里原来写的是 `w32.key_bg(...)`，而上面 import 的是
            # `win32_mc as _w32` ⇒ 每次"有界面要关"都抛 NameError（被外层 except 吞成
            # `screen_open {err: NameError: name 'w32' is not defined}`）⇒ 界面永远关不掉。
            _w32.key_bg(hwnd, 0x1B)
            time.sleep(0.8)
            st = screen_state(reg)
            if not st.get("open"):
                return {"open": False, "closed_after": i + 1, "method": "postmessage"}
    except Exception as e:
        return {"open": True, "err": f"{type(e).__name__}: {e}"[:120]}
    # ESC 试到底还是关不掉 ⇒ 用 `respawnPlayer` 兜底（活着且没界面时它是 no-op，安全）
    if st.get("open"):
        try:
            r = reg.invoke("respawnPlayer", {})
            time.sleep(1.0)
            st2 = screen_state(reg)
            if not st2.get("open"):
                if log:
                    log({"ev": "screen_closed_by_respawn", "ok": bool(r.ok)})
                return {"open": False, "method": "respawn_fallback"}
        except Exception:
            pass
    return {"open": True, "still": st.get("title")}


# ── 命令类杠杆：下发 + 用世界状态判完成 ─────────────────────────────────────
def path_busy(reg: Registry) -> bool:
    d = reg.invoke("pathStatus", {}).data
    return bool(isinstance(d, dict) and truthy(d.get("busy")))


# ── 放置：把 tryPlace 的 4-tick 限速兜住（2026-10-30）────────────────────────
# Java 侧 `BlockPlaceHelper.tryPlace` 有"每 4 tick 只能放一次"的限速（和原版一致，防这个杠杆
# 比人还快）。被限速时它回 `sent:false`，上一层的 `placeBlockAt` 却已经把"瞄准成功"当成了 ok
# ⇒ 一次 `sent:false` 就等于**这一拍什么也没放**。实测：连着放两格，第二格
# `ok:false, sent:false, cooldown:true`，而目标格一直是 air —— 外面看就是"放置时灵时不灵"。
# 所以重试放在这里：**只有"可重试"的原因才重试**，几何/背包/保护问题立刻返回，别拿 8 次去撞墙。
_PLACE_PERMANENT = ("not replaceable", "no block item", "no reachable supporting face",
                    "placing denied", "not in game", "no block under crosshair",
                    "held item is not a block",
                    # 2026-10-30：click 没发出去 ≠ 限速。前者重试 8 次也只是烧 2 秒
                    "the click did not go through")

# 「自己的身体压住目标格」——**不是**永久失败，换个站位就能放（见下面 `_step_aside`）。
# 2026-10-30 实测：站 (65.0,66,-74) 放 (65,66,-75) 连打四次全 `sent:false`；挪开一格，
# 同一格第一次就成。玩家碰撞箱 x[64.7,65.3] z[-74.3,-73.7] 与目标格 z[-75,-74] 有 0.3 宽重叠，
# 原版不许把方块放进玩家身体里。老 mineflayer 栈的 `recenter()` 治的就是这一条。
_PLACE_BODY_BLOCKED = ("my own hitbox covers that cell", "body_blocked")


def _step_aside(reg: Registry, x: int, y: int, z: int, *, push: float = 0.9,
                sign: float = 1.0, log=None) -> dict:
    """从目标格的水平轴线上退开 `push` 格，好让下一次点击不再被自己的身体挡住。

    只沿"偏离格子中心"的方向退，不绕路、不寻路：这一点位移是放置的前置动作，
    不是策略决定（策略层有自己的 `park_point` / `gotoXYZ`）。

    ⚠️ 2026-10-30 实测两条，都写进参数里了：
      · **0.6 格不够** —— 玩家箱半宽就是 0.3，退 0.6 之后只要原来压着 0.2 以上，还是压着。
      · **0.45 秒不够** —— `gotoXYZ` 是寻路，实测 0.9 格要走 ~0.6 秒（t+0.3 走了 0.6 格，
        t+0.6 到位）。所以这里改成**轮询到真的动了**，而不是睡一个猜出来的时长。
    返回里带 `moved`/`to`，让调用方能说实话：退没退开是能验的，不是假设的。
    """
    def _pos():
        try:
            st = reg.invoke("getGameState", {}).data or {}
            me = st.get("self") or {}
            return [float(v) for v in str(me.get("pos", "")).split(",")]
        except Exception:
            return None

    p0 = _pos()
    if not p0 or len(p0) != 3:
        return {"moved": False, "err": "no position"}
    cx, cz = x + 0.5, z + 0.5
    dx, dz = p0[0] - cx, p0[2] - cz
    if abs(dx) < 1e-3 and abs(dz) < 1e-3:
        dx = 1.0
    d = (dx * dx + dz * dz) ** 0.5
    goal = (p0[0] + dx / d * push * sign, p0[1], p0[2] + dz / d * push * sign)
    try:
        reg.invoke("gotoXYZ", {"x": goal[0], "y": goal[1], "z": goal[2]})
    except Exception:
        pass
    moved = 0.0
    for _ in range(10):                     # 最多 1.5 秒，看到动了就走
        time.sleep(0.15)
        p1 = _pos()
        if p1 and len(p1) == 3:
            moved = max(moved, abs(p1[0] - p0[0]) + abs(p1[2] - p0[2]))
            if moved >= 0.4:
                break
    p1 = _pos() or p0
    out = {"moved": moved >= 0.4, "shift": round(moved, 2),
           "from": ",".join("%.2f" % v for v in p0), "to": ",".join("%.2f" % v for v in p1)}
    if log:
        log({"ev": "place_step_aside", "at": [x, y, z], **out})
    return out


def place_block(reg: Registry, x: int, y: int, z: int, *, tries: int = 8,
                gap: float = 0.25, log=None) -> dict:
    """放一格方块；`cooldown` 类失败自动重试（最多 `tries` 次，间隔 `gap` 秒）。

    判据是**世界状态**（`placed` 字段 = 目标格由 air 变非 air），不是"命令发出去了"。
    `body_blocked`（自己压着目标格）会先退开半步再放一次 —— 重试本身治不了它。
    """
    last: dict = {}
    n = max(1, int(tries))
    pushes = [(0.9, 1.0), (1.3, -1.0)]   # 先往外退 0.9；退不动就**反方向**退 1.3，再不行认输
    step = 0
    for i in range(n):
        r = reg.invoke("placeBlockAt", {"x": int(x), "y": int(y), "z": int(z)})
        d = r.data if (r.ok and isinstance(r.data, dict)) else {}
        reason = str(d.get("reason") or r.err or "")
        last = {"try": i + 1, **d}
        if d.get("placed"):
            if log and i:
                log({"ev": "place_retry_ok", "tries": i + 1, "at": [x, y, z]})
            return {**last, "ok": True, "tries": i + 1}
        if d.get("body_blocked") or any(k in reason for k in _PLACE_BODY_BLOCKED):
            if step >= len(pushes):
                return {**last, "ok": False, "tries": i + 1, "why": reason,
                        "body_blocked": True}
            push, sign = pushes[step]
            mv = _step_aside(reg, x, y, z, push=push, sign=sign, log=log)
            step += 1
            if not mv.get("moved"):
                # 这个方向退不动（墙/水/卡住）⇒ 还有下一个方向就再试，没有就认输。
                # 认输要老实：`body_blocked:true` + `moved:false`，让上层决定去别处站。
                if step >= len(pushes):
                    return {**last, "ok": False, "tries": i + 1, "why": reason,
                            "body_blocked": True, "step_aside": mv}
                continue
            continue          # 退开了，下一轮就是"第一次尝试"
        if d.get("permanent") or reason and any(k in reason for k in _PLACE_PERMANENT):
            return {**last, "ok": False, "tries": i + 1, "why": reason}
        time.sleep(gap)      # cooldown 或"发出去了但还没变" ⇒ 等下一拍
    if log:
        log({"ev": "place_retry_exhausted", "tries": n, "at": [x, y, z], "last": last})
    return {**last, "ok": False, "tries": n,
            "why": last.get("reason") or "重试用尽（限速或服务器拒绝）"}


def place_looking(reg: Registry, *, tries: int = 8, gap: float = 0.25, log=None) -> dict:
    """对着准星那面放一块（`placeBlockLooking` 的限速兜底版）。判据同样是"东西真的变没变"。"""
    last: dict = {}
    n = max(1, int(tries))
    for i in range(n):
        r = reg.invoke("placeBlockLooking", {})
        d = r.data if (r.ok and isinstance(r.data, dict)) else {}
        reason = str(d.get("reason") or r.err or "")
        last = {"try": i + 1, **d}
        if d.get("ok") or d.get("placed"):
            return {**last, "ok": True, "tries": i + 1}
        if any(k in reason for k in _PLACE_PERMANENT):
            return {**last, "ok": False, "tries": i + 1, "why": reason}
        time.sleep(gap)
    return {**last, "ok": False, "tries": n,
            "why": last.get("reason") or "重试用尽（限速或服务器拒绝）"}


def body_lock(reg: Registry, on: bool, *, log=None) -> dict:
    """把身体从 AltoClef 的**竞价系统**里拿过来 / 还回去（`takeControl` = TaskRunner.enable/disable）。

    为什么必须有这么个动作（2026-10-30 血证 + 源码读数）：`TaskRunner.tick()` 每 tick 让所有
    chain **报价**，最高的独占身体 —— UserTaskChain **50**（我们的 `@mineblock` / `@idle` 都在
    这一档）、SupervisorTaskChain **51**、FoodChain「去囤粮」**55**、MobDefenseChain 最高 **70**。
    所以 `@stop`（只 cancel 用户链）**永远抢不过 55/70**：实测 `@mineblock` 一按下就被
    `Collect 12.0 units of food / Cooking…` 或 `NIGERUNDAYOO … skeletons` 顶掉。

    要"现在必须做这件事、谁也别抢"，唯一可靠的手段是把整条 runner **停掉**。
    代价：防怪/逃生/吃饭这些链也一起停 ⇒ 调用方**必须**带超时兜底
    （见 `commander.body_lock()` + `tick_body_lock()`）。
    """
    try:
        r = reg.invoke("takeControl", {"on": bool(on)})
        ok = bool(r.ok)
        if log:
            log({"ev": "body_lock" if on else "body_unlock", "ok": ok})
        return {"ok": ok}
    except Exception as e:
        if log:
            log({"ev": "body_lock_error", "on": on,
                 "err": f"{type(e).__name__}: {e}"[:120]})
        return {"ok": False, "err": f"{type(e).__name__}: {e}"[:120]}


def stop_all(reg: Registry) -> dict:
    """清掉身体里任何自主任务/导航腿 —— **并且确认它真的清了**。

    为什么不能只发一条命令（2026-10-27 血证）：AltoClef 的 TaskRunner 会在 `@stop` 之后
    被它自己的"卡死自救/shimmy"重新武装，导航腿一旦在跑，**挖矿就把瞄准让给它**
    （chat: `Mining aborted … progress=-0.10` + `FastNavigator: at the dig` +
    `yielding to a running navigator leg`）⇒ 看起来是"挖不动"，其实是"准星被别人拿着"。
    所以这里逐级下药并**读回来验**：stopPathing → @stop → @idle。
    """
    seq = [("stopPathing", None), ("run_command", "@stop"),
           ("run_command", "@idle"), ("stopPathing", None)]
    tries = 0
    for name, cmd in seq:
        tries += 1
        if name == "run_command":
            reg.invoke(name, {"cmd": cmd})
        else:
            reg.invoke(name, {})
        time.sleep(0.35)
        if not path_busy(reg):
            return {"ok": True, "cleared_by": cmd or name, "tries": tries}
    return {"ok": not path_busy(reg), "cleared_by": "", "tries": tries,
            "busy": path_busy(reg)}


def inventory(reg: Registry) -> dict:
    """背包全量 {item: count}。走 `getOpenScreen` 的 slots —— 它连工具/食物都列，
    而 `inventorySpace` 只统计 BlockItem（会把木镐/熟肉漏掉）。"""
    r = reg.invoke("getOpenScreen", {})
    d = r.data if r.ok and isinstance(r.data, dict) else {}
    out: dict[str, int] = {}
    for slot in (d.get("slots") or []):
        if not isinstance(slot, dict) or slot.get("empty"):
            continue
        it = str(slot.get("item") or "")
        if not it:
            continue
        out[it] = out.get(it, 0) + int(slot.get("count") or 0)
    return out


def count_item(reg: Registry, item: str) -> int:
    """背包里某物品的数量。走 `@inventory <item>` 的 chat 回执（`<item> COUNT: n`）。

    为什么不读 `getOpenScreen().slots`：那一份**只有界面开着时才有 slots**，
    界面一关就只剩 `{"open": false}` ⇒ 数什么都得 0（这个坑让 supply 判"我没有木镐"）。
    """
    r = reg.invoke("run_command", {"cmd": f"@inventory {item}"})
    if not r.ok:
        return 0
    short = item.split(":")[-1].lower()
    for _ in range(6):
        for line in reversed(read_chat(reg, 20)):
            plain = "".join(ch for ch in str(line) if ch.isprintable())
            low = plain.lower()
            if short not in low or "count" not in low:
                continue
            tail = low.split("count:")[-1].strip()
            num = ""
            for ch in tail:
                if ch.isdigit():
                    num += ch
                else:
                    break
            if num:
                return int(num)
        time.sleep(0.25)
    return 0


def unstick(reg: Registry, *, tries: int = 8, log=None) -> dict:
    """**挪一格**：卡住时最便宜的一招（比关界面、比重启都便宜）。

    2026-10-27 现场：Buddy 与另一个实体（假人 Xiaorun）**站在同一格**，
    准星永远先打到那个实体（`lookingAt: ENTITY`）⇒ `crosshairTarget` 不是方块
    ⇒ `Mining aborted … progress=-0.10` ⇒ 挖不动 ⇒ FastNavigator 想"挖出去"也挖不动
    ⇒ 坐标一动不动、`busy=true`、卡死检测刷到 #102。

    这时唯一有效的动作是**换一格站**（不要挖，走）。8 个方向 × 2 档距离，谁先能站就试谁。
    判据：位移 > 0.6 格。
    """
    p = pos_of(reg)
    if not p:
        return {"ok": False, "why": "读不到坐标"}
    a0 = aim_of(reg)
    for r in (1.4, 2.2, 3.0):
        for off in range(0, 360, 45):
            a = math.radians(a0.get("yaw", 0.0) + off)
            x = int(round(p[0] - math.sin(a) * r))
            z = int(round(p[2] + math.cos(a) * r))
            for dy in (0, 1, -1):
                y = int(round(p[1])) + dy
                if not (is_air(reg, x, y, z) and is_air(reg, x, y + 1, z)
                        and not is_air(reg, x, y - 1, z)):
                    continue
                reg.invoke("gotoXYZ", {"x": x, "y": y, "z": z})
                t0 = time.time()
                while time.time() - t0 < 4.0:
                    q = pos_of(reg)
                    if q and dist(q, p) > 0.6:
                        out = {"ok": True, "from": [round(v, 2) for v in p],
                               "to": [round(v, 2) for v in q], "target": [x, y, z],
                               "moved": round(dist(q, p), 2)}
                        if log:
                            log({"ev": "unstick_ok", **out})
                        return out
                    time.sleep(0.4)
    if log:
        log({"ev": "unstick_failed", "at": [round(v, 2) for v in p]})
    return {"ok": False, "why": "八个方向都挪不动"}


def aim_blocked(reg: Registry) -> bool:
    """准星是不是**被打到实体上**（= 面前站了个人/怪，方块挖不到）。

    这不是"准星坏了"，是"挡着了"。判据与修法都不同：
      · 准星死（渲染不推进）→ 重启客户端；
      · 准星被实体挡 → **挪一格**（actions.unstick），别重启。
    """
    return str(aim_of(reg).get("lookingAt") or "").upper().startswith("ENTITY")


def nearby_entities(reg: Registry, *, limit: int = 32) -> list[dict]:
    """附近的活物/掉落物（getEntities 的规整版：position 拆成三元组）。"""
    r = reg.invoke("getEntities", {"limit": int(limit)})
    d = r.data
    if isinstance(d, dict):
        d = d.get("_raw") or d.get("entities") or []
    out = []
    for e in (d or []):
        if not isinstance(e, dict):
            continue
        try:
            x, y, z = [float(v) for v in str(e.get("position")).split(",")]
        except Exception:
            continue
        out.append({"type": str(e.get("type") or ""),
                    "item": truthy(e.get("is_item")),
                    "hostile": truthy(e.get("is_hostile")),
                    "pos": (x, y, z), "health": e.get("health")})
    return out


def nearby_items(reg: Registry, *, limit: int = 32) -> list[dict]:
    return [e for e in nearby_entities(reg, limit=limit) if e["item"]]


def _item_snapshot(reg: Registry, near) -> set:
    out = set()
    for e in nearby_items(reg):
        if dist(e["pos"], near) <= 6.0:
            out.add((round(e["pos"][0], 1), round(e["pos"][1], 1), round(e["pos"][2], 1)))
    return out


def player_online(name: str, *, port: int = 25576) -> bool | None:
    """玩家在不在线（RCON `list`）。读不到返回 None（网络/权限问题时不要误判）。"""
    try:
        import subprocess
        import sys as _sys
        from pathlib import Path as _P
        rq = _P(__file__).resolve().parent.parent / "tools" / "rq.py"
        out = subprocess.run([_sys.executable, "-X", "utf8", str(rq),
                              "--port", str(port), "list"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=15)
        text = (out.stdout or "") + (out.stderr or "")
        if "players online" not in text:
            return None
        return str(name).lower() in text.lower()
    except Exception:
        return None


def player_pos_rcon(name: str, *, host: str = "127.0.0.1", port: int = 25576,
                    password: str = "buddy") -> tuple[float, float, float] | None:
    """上帝视角读某个玩家的坐标（客户端看不到人时的最后手段）。

    为什么需要（2026-10-29 实测）：新世界里两人相距 **127 格**，客户端的实体列表里
    根本没有对方 ⇒ `getGameState.players` 为空 ⇒ 指挥层每 tick 记 `no_player`、
    站着不动 ⇒ 健康阶梯把"没事干"误判成"卡死"并重启客户端。陪玩得先**找到人**。
    """
    try:
        import subprocess
        import sys as _sys
        from pathlib import Path as _P
        rq = _P(__file__).resolve().parent.parent / "tools" / "rq.py"
        out = subprocess.run([_sys.executable, "-X", "utf8", str(rq), "--port", str(port),
                              f"data get entity {name} Pos"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        text = (out.stdout or "") + (out.stderr or "")
        m = re.search(r"\[([-\d.]+)d?,\s*([-\d.]+)d?,\s*([-\d.]+)d?\]", text)
        if m:
            return (float(m.group(1)), float(m.group(2)), float(m.group(3)))
    except Exception:
        return None
    return None


def _ring_approach(reg: Registry, ppos, *, cancel=None, log=None) -> float | None:
    """绕玩家一圈找"真能站"的格子走过去，返回走完之后的最近距离。

    为什么需要（2026-10-29 实测）：玩家站在 30 格外的坡上时，`goto` 到"玩家 + 1.2"那个点
    走不到（最近 5 格），于是从 5 格外把东西抛出去 ⇒ 落点离玩家 **3.56 格**，谁也捡不到。
    绕圈试 8 方向 × 4 档半径 × 5 层高度，只走不挖；还靠不近就交给 `@goto` 兜底。
    """
    best = None
    for r in (1.4, 2.0, 2.6, 3.2):
        for ang in range(0, 360, 45):
            if cancel is not None and cancel():
                return best
            a = math.radians(ang)
            x = int(round(ppos[0] + math.cos(a) * r))
            z = int(round(ppos[2] + math.sin(a) * r))
            for dy in (0, 1, -1, 2, -2):
                y = int(round(ppos[1])) + dy
                if is_air(reg, x, y, z) and is_air(reg, x, y + 1, z) \
                        and not is_air(reg, x, y - 1, z):
                    goto(reg, x, y, z, timeout=6.0, tol=0.8, cancel=cancel)
                    me = pos_of(reg)
                    d = dist(me, ppos) if me else None
                    if d is not None and (best is None or d < best):
                        best = d
                    break
            if best is not None and best <= 1.8:
                if log:
                    log({"ev": "ring_approach", "best": round(best, 2), "r": r})
                return best
    if log:
        log({"ev": "ring_approach", "best": round(best, 2) if best is not None else None})
    return best


def handover(reg: Registry, ppos, item: str, count: int = 1, *, toward: str,
             cancel=None, allow_teleport: bool = True, log=None) -> dict:
    """**递东西**：把东西放到玩家脚边（拾取判定是服务端碰撞，0.5 格内必收）。

    为什么这么绕（2026-10-27 三轮血证）：
      · `dropItem` 的 `toward` 是 **required**，不传 NPE，报错还像"成功了"；
      · 掉落物**不是丢在脚下**：沿视线抛出约 2.5~2.7 格（实测落点离玩家 0.76~1.73 格）；
      · 拾取是**碰撞**判定（要真的碰到玩家 AABB，约 0.5 格），不是"1.5 格自动吸"。

    所以：**用实测来校准站位**。丢一个、量落点、把站位距离按误差修正，最多三次。
    判据 = 新出现的掉落物离玩家 ≤ 0.9 格（那时真人一走过去就收）。
    """
    ppos = tuple(float(v) for v in ppos)
    steps: list[dict] = []
    before_items = _item_snapshot(reg, ppos)
    standoff = 1.2
    best = None
    d_me = None
    for attempt in range(3):
        if cancel is not None and cancel():
            return {"ok": False, "why": "已取消", "steps": steps}
        me0 = pos_of(reg)
        if not me0:
            return {"ok": False, "why": "读不到坐标", "steps": steps}
        # ⚠️ 每轮都要重新拍快照：上一轮丢出去的掉落物**还躺在地上**，
        #    拿旧快照去比会把"上一件的落点"当成"这一件的落点"（实测三次都读到 1.71）。
        before_items = _item_snapshot(reg, ppos)
        # 站到"玩家 + standoff"位置（方向用我当前方位）
        dx, dz = me0[0] - ppos[0], me0[2] - ppos[2]
        L = math.hypot(dx, dz) or 1.0
        tx = int(round(ppos[0] + dx / L * standoff))
        tz = int(round(ppos[2] + dz / L * standoff))
        g = goto(reg, tx, int(round(ppos[1])), tz, timeout=15.0, tol=0.9, cancel=cancel)
        # 2026-10-29：坡地/高低差上这一跳经常走不到（实测最近 5 格），
        # 于是从 5 格外抛出去 ⇒ 落点离玩家 3.56 格。正解：绕圈找能站的格子；还靠不近就**不丢**。
        me1 = pos_of(reg)
        d_me = dist(me1, ppos) if me1 else None
        if d_me is None or d_me > 2.6:
            d2 = _ring_approach(reg, ppos, cancel=cancel, log=log)
            if d2 is not None and (d_me is None or d2 < d_me):
                d_me = d2
        if d_me is None or d_me > 2.6:
            # 最后一招：AltoClef 的 @goto（它的寻路比我们手算的落点强）
            try:
                reg.invoke("run_command",
                           {"cmd": f"@goto {int(round(ppos[0]))} {int(round(ppos[1]))} "
                                   f"{int(round(ppos[2]))}"})
                time.sleep(6.0)
                me2 = pos_of(reg)
                d3 = dist(me2, ppos) if me2 else None
                if d3 is not None and (d_me is None or d3 < d_me):
                    d_me = d3
            except Exception:
                pass
        if d_me is None or d_me > 2.6:
            steps.append({"ev": "too_far", "attempt": attempt + 1,
                          "dist": round(d_me, 2) if d_me is not None else None})
            continue
        reg.invoke("lookAt", {"x": ppos[0], "y": ppos[1] + 1.1, "z": ppos[2]})
        time.sleep(0.12)
        r = reg.invoke("dropItem", {"item": item, "count": int(count), "toward": toward})
        # 找新出现的掉落物
        landing = None
        t0 = time.time()
        while time.time() - t0 < 3.5:
            now_items = nearby_items(reg)
            for e in now_items:
                key = (round(e["pos"][0], 1), round(e["pos"][1], 1), round(e["pos"][2], 1))
                if key in before_items:
                    continue
                if dist(e["pos"], ppos) <= 5.0:
                    landing = e["pos"]
                    break
            if landing:
                break
            time.sleep(0.25)
        d = round(dist(landing, ppos), 2) if landing else None
        steps.append({"ev": "drop", "attempt": attempt + 1, "standoff": round(standoff, 2),
                      "ok": r.ok, "landing_d": d})
        if log:
            log({"ev": "handover_try", "n": attempt + 1, "standoff": round(standoff, 2),
                 "landing_d": d})
        if d is not None and (best is None or d < best[0]):
            best = (d, landing)
        if d is not None and d <= 0.9:
            break
        # 校准：落点远了就把站位往外挪（抛得远 ⇒ 站远点让落点靠近玩家）
        if d is not None:
            standoff = max(0.8, min(3.2, standoff + (d - 0.6)))
    out = {"ok": bool(best and best[0] <= 0.9), "landing_d": best[0] if best else None,
           "dist": round(d_me, 2) if d_me is not None else None,
           "steps": steps, "item": item, "dropped": count,
           "why": "" if (best and best[0] <= 0.9) else
                  (f"靠不近（最近 {round(d_me, 2)} 格，>2.6 不丢）" if (d_me is None or d_me > 2.6)
                   else "三次都没落到玩家脚边 0.9 格内")}
    return out


def mining_aborted(reg: Registry, n: int = 40) -> int:
    """最近聊天里出现几次 `Mining aborted … progress=-0.10`。

    这是"准星/渲染死了"最可靠的**间接**判据（2026-10-27 两次实测）：
    方块明明在 1 格内、`canReach` 说能挖、`mineStatus` 说在挖，但进度永远是 -0.10 —— 
    这种状态**挪一格没用、@stop 也没用，只有重启客户端能恢复**。
    """
    return sum(1 for line in read_chat(reg, n)
               if "Mining aborted" in line and "progress=-0.10" in line)


# ── 近战（自己打，不靠 @hero 乱跑） ─────────────────────────────────────────
ATTACK_REACH = 3.2          # 眼到实体中心（MC 的实体攻击距离约 3 格）


SWORD_ORDER = ("netherite_sword", "diamond_sword", "iron_sword", "stone_sword",
               "golden_sword", "wooden_sword")
TOOL_ORDER = ("netherite_axe", "diamond_axe", "iron_axe", "stone_axe",
              "wooden_axe", "wooden_pickaxe", "stone_pickaxe")


_HOTBAR: dict = {"at": 0.0, "map": {}}


def scan_hotbar(reg: Registry, *, ttl: float = 90.0) -> dict:
    """读热键栏 0~8 各是什么（`getOpenScreen` 不开界面时**没有 slots**，只能一个个选）。

    实测：`getOpenScreen` 在没开界面时只回 `{"open": false}`，于是"看背包挑武器"永远为空 ——
    正确做法是 `selectHotbar(i)` 之后读 `getGameState.self.held`，**看到什么算什么**。
    """
    now = time.time()
    if _HOTBAR["map"] and now - _HOTBAR["at"] < ttl:
        return _HOTBAR["map"]
    out: dict[int, str] = {}
    for i in range(9):
        reg.invoke("selectHotbar", {"slot": i})
        st = reg.invoke("getGameState", {})
        me = (st.data or {}).get("self") if (st.ok and isinstance(st.data, dict)) else {}
        held = str((me or {}).get("held") or "")
        if held and not held.endswith(":air"):
            out[i] = held
    _HOTBAR.update({"at": now, "map": out})
    return out


def equip_best_weapon(reg: Registry, log=None) -> dict:
    """把手上换成最好的武器（剑 > 斧 > 镐）。

    2026-10-27 实测：拿着**木镐**打僵尸，一下 2 点伤害，两只僵尸要 9 下、自己掉 12 血；
    换成剑/斧 4~7 点，同样的怪 3~5 下就没了。护主腿要先换武器再打。
    """
    hb = scan_hotbar(reg)
    want = list(SWORD_ORDER) + list(TOOL_ORDER)
    best, best_rank = None, 999
    for idx, item in hb.items():
        short = item.split(":")[-1]
        rank = next((k for k, w in enumerate(want) if short == w), None)
        if rank is not None and rank < best_rank:
            best, best_rank = (idx, short), rank
    if not best:
        if log:
            log({"ev": "weapon_none", "hotbar": {k: v.split(":")[-1] for k, v in hb.items()}})
        return {"ok": False, "why": "热键栏里没有武器/工具"}
    reg.invoke("selectHotbar", {"slot": int(best[0])})
    if log:
        log({"ev": "weapon_equipped", "item": best[1], "slot": best[0]})
    return {"ok": True, "item": best[1], "slot": best[0]}


def attack_entity(reg: Registry, target_pos, *, swings: int = 6, cancel=None,
                  kiting: bool = True) -> dict:
    """对着一个实体位置挥 `swings` 下左键（anti-cheat-safe 的输入管道）。

    为什么自己打而不只靠 `@hero`（2026-10-27 夜间实测）：`@hero` 会**跑远追怪**，
    还会 `Pillaring up to y=71` 搭柱子躲 creeper —— 陪玩的职责是**守在玩家身边**。

    `kiting`：挥完一下**退半步**再等冷却。站着连挥会被僵尸按住打
    （首版实测：4 下×0.55 秒站桩，hp 20 → 6 就被迫收手）。
    """
    t = (float(target_pos[0]), float(target_pos[1]), float(target_pos[2]))
    hits = 0
    for i in range(max(1, swings)):
        if cancel is not None and cancel():
            return {"ok": False, "why": "已取消", "hits": hits}
        reg.invoke("lookAt", {"x": int(t[0]), "y": int(t[1]) + 1, "z": int(t[2])})
        time.sleep(0.10)
        reg.invoke("mouseClick", {"button": "left"})
        hits += 1
        if kiting:
            me = pos_of(reg)
            if me:
                dx, dz = me[0] - t[0], me[2] - t[2]
                L = math.hypot(dx, dz) or 1.0
                reg.invoke("gotoXYZ", {"x": int(round(me[0] + dx / L * 1.4)),
                                       "y": int(round(me[1])),
                                       "z": int(round(me[2] + dz / L * 1.4))})
        time.sleep(0.45)          # 攻击冷却（剑约 0.6 秒）
    return {"ok": True, "hits": hits}


def clear_near(reg: Registry, center, *, radius: float = 8.0, budget_s: float = 45.0,
               cancel=None, log=None) -> dict:
    """**护主清怪**：只清"玩家 radius 格内"的敌对生物，绝不跑远。

    判据是"玩家附近的敌对数量下降"，不是"我杀了多少只"。
    每轮：挑离我最近的怪 → 够不着就走过去（但**不许走出 radius**）→ 左键连击 →
    读 getEntities 确认它真的没了/掉血。
    """
    c = (float(center[0]), float(center[1]), float(center[2]))
    t0 = time.time()
    steps: list[dict] = []
    killed = 0
    start = None

    def hostiles():
        return [e for e in nearby_entities(reg, limit=40) if e["hostile"]
                and dist(e["pos"], c) <= radius]

    first = hostiles()
    start = len(first)
    weapon = equip_best_weapon(reg, log=log)
    steps.append({"ev": "start", "near": start, "weapon": weapon.get("item"),
                  "kinds": sorted({e["type"] for e in first})[:5]})
    while time.time() - t0 < budget_s:
        if cancel is not None and cancel():
            break
        mobs = hostiles()
        if not mobs:
            break
        me = pos_of(reg)
        if not me:
            break
        target = min(mobs, key=lambda e: dist(e["pos"], me))
        tp = target["pos"]
        d = dist(eye_of(me), tp)
        if d > ATTACK_REACH:
            # 走过去，但**站到离玩家不超过 radius 的地方**
            gx, gz = tp[0], tp[2]
            if dist((gx, tp[1], gz), c) > radius - 1.0:
                steps.append({"ev": "skip_far", "type": target["type"],
                              "dist_to_player": round(dist(tp, c), 1)})
                break
            g = goto(reg, int(round(gx)), int(round(tp[1])), int(round(gz)),
                     timeout=12.0, tol=2.2, cancel=cancel)
            steps.append({"ev": "approach", "ok": g.get("ok"), "dist": g.get("dist")})
            continue
        hp0 = target.get("health")
        a = attack_entity(reg, tp, swings=3, cancel=cancel)
        steps.append({"ev": "attack", "type": target["type"], "hp0": hp0,
                      "hits": a.get("hits")})
        after = [e for e in hostiles() if dist(e["pos"], tp) < 0.9]
        if after and str(after[0].get("health")) != str(hp0):
            steps.append({"ev": "damaged", "hp_now": after[0].get("health")})
        # 自己被贴脸且血低 → 收手（护主不该把自己搭进去）
        st = reg.invoke("getGameState", {}).data or {}
        hp_me = float((st.get("self") or {}).get("hp") or 20)
        if hp_me <= 10.0:
            steps.append({"ev": "retreat_low_hp", "hp": hp_me})
            break
    end = hostiles()
    killed = max(0, (start or 0) - len(end))
    out = {"ok": len(end) < (start or 0) or (start == 0 and not end),
           "near_before": start, "near_after": len(end), "killed": killed,
           "sec": round(time.time() - t0, 1), "steps": steps[-8:]}
    if log:
        log({"ev": "clear_near", **{k: out[k] for k in ("near_before", "near_after",
                                                       "killed", "sec", "ok")}})
    return out


FOODS = {"bread", "apple", "golden_apple", "cooked_beef", "cooked_porkchop",
         "cooked_chicken", "cooked_mutton", "cooked_cod", "cooked_salmon",
         "baked_potato", "carrot", "potato", "beetroot", "beef", "porkchop", "chicken",
         "mutton", "cod", "salmon", "cookie", "melon_slice", "sweet_berries",
         "dried_kelp", "pumpkin_pie", "rabbit", "cooked_rabbit", "mushroom_stew"}


def eat(reg: Registry, *, max_bites: int = 4, log=None) -> dict:
    """**吃东西**（补上"我饿了"里 AI 一直缺的那个杠杆）。

    2026-10-27 MCP 体检的结论之一：全功能里唯一"只能在世界里找、不能自己吃"的就是食物 ——
    `@food` 是"去找吃的"（会跑很远），而**手上已经有食物时没有工具能吃它**。
    实现：热键栏扫一遍（`scan_hotbar`）→ 选中食物 → `useHeldItem`（右键 = 吃）→
    用 `getGameState.self.food` 核验真的回了饥饿值。
    """
    st = reg.invoke("getGameState", {}).data or {}
    me = st.get("self") or {}
    food0 = float(me.get("food") or 0)
    if food0 >= 20:
        return {"ok": True, "why": "不饿（food=20）", "food": food0, "bites": 0}
    hb = scan_hotbar(reg)
    slot = next((i for i, it in hb.items() if it.split(":")[-1] in FOODS), None)
    if slot is None:
        if log:
            log({"ev": "eat_no_food", "hotbar": {k: v.split(":")[-1] for k, v in hb.items()}})
        return {"ok": False, "why": "热键栏里没有食物", "food": food0}
    reg.invoke("selectHotbar", {"slot": int(slot)})
    bites, food = 0, food0
    for _ in range(max(1, max_bites)):
        reg.invoke("useHeldItem", {})
        bites += 1
        time.sleep(1.0)               # 吃一口约 1.6 秒（咀嚼动画），1 秒够看回饱食度
        st = reg.invoke("getGameState", {}).data or {}
        food = float((st.get("self") or {}).get("food") or 0)
        if food >= 20 or food > food0 + 3:
            break
    out = {"ok": food > food0, "food_before": food0, "food_after": food,
           "bites": bites, "item": hb.get(slot)}
    if log:
        log({"ev": "eat", **out})
    return out


def _slot_item(reg: Registry, idx: int) -> str:
    d = reg.invoke("getOpenScreen", {}).data
    d = d if isinstance(d, dict) else {}
    for s in (d.get("slots") or []):
        if isinstance(s, dict) and int(s.get("index", -1)) == int(idx):
            return "" if s.get("empty") else str(s.get("item") or "")
    return ""


def move_item(reg: Registry, from_slot: int, to_slot: int, *, log=None) -> dict:
    """**背包内挪物品**（AltoClef 只有"选热键栏"，没有通用挪槽 —— 这条只能自己写）。

    用它的 `clickUiSlot` 原语：两次 PICKUP = 一次移动（第一次拿起、第二次放下）。

    ⚠️ 两个坑（2026-10-27 实测）：
    1. **界面必须开着**（背包/箱子/工作台）。没开界面时 `clickUiSlot` 照样回 `{"ok":true}`
       —— 假成功；所以这里先查 `getOpenScreen.open`，再核验**目标槽真的变了**。
    2. `getOpenScreen().slots[].index` 就是 `windowSlot`，别自己算容器偏移。
    """
    st = reg.invoke("getOpenScreen", {}).data
    st = st if isinstance(st, dict) else {}
    if not st.get("open"):
        return {"ok": False, "why": "没有开着的界面（先开背包/箱子/工作台）"}
    before_from, before_to = _slot_item(reg, from_slot), _slot_item(reg, to_slot)
    if not before_from and not before_to:
        # 两个槽都空 ⇒ 点两次当然"没变化"。这不是失败，是没东西可挪（别让上游误判卡住）。
        return {"ok": True, "why": "两个槽都是空的", "nothing_to_move": True,
                "from": from_slot, "to": to_slot}
    r1 = reg.invoke("clickUiSlot", {"windowSlot": int(from_slot), "button": 0,
                                    "action": "PICKUP"})
    time.sleep(0.30)
    r2 = reg.invoke("clickUiSlot", {"windowSlot": int(to_slot), "button": 0,
                                    "action": "PICKUP"})
    time.sleep(0.30)
    after_from, after_to = _slot_item(reg, from_slot), _slot_item(reg, to_slot)
    moved = (after_from != before_from) and (after_to != before_to)
    out = {"ok": bool(moved), "from": from_slot, "to": to_slot,
           "item": before_from.split(":")[-1], "into": before_to.split(":")[-1],
           "after_from": after_from.split(":")[-1], "after_to": after_to.split(":")[-1],
           "err": (r1.err or r2.err or "")[:120],
           "why": "" if moved else "槽位没变化（点击没生效）"}
    if log:
        log({"ev": "move_item", **out})
    return out


def player_inventory(name: str, *, host: str = "127.0.0.1", port: int = 25576,
                     password: str = "buddy") -> dict:
    """读**玩家**背包（客户端拿不到别人的 inventory ⇒ 只能走服务端 RCON）。

    判据/调试都能用；和生产路径要分开看：这是"上帝视角"，不是陪玩自己能看见的东西。
    没有 RCON 时安静降级（`{"ok": False, "why": ...}`），不抛异常。
    """
    try:
        import sys as _sys
        from pathlib import Path as _P
        root = _P(__file__).resolve().parent.parent
        if str(root / "orchestrator") not in _sys.path:
            _sys.path.insert(0, str(root / "orchestrator"))
        import rcon as _rcon                       # type: ignore
        rc = _rcon.Rcon(host, port, password).connect()
        try:
            raw = rc.command(f"data get entity {name} Inventory") or ""
        finally:
            rc.close()
        import re
        items: dict[str, int] = {}
        for m in re.finditer(r'id:\s*"([^"]+)",\s*count:\s*(\d+)', raw):
            items[m.group(1)] = items.get(m.group(1), 0) + int(m.group(2))
        return {"ok": bool(items), "items": items, "raw_len": len(raw),
                "note": "RCON 回复会被截断（实测 ~132 字节）；要精确数量用 clear <p> <item> 0"}
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:120]}


def count_player_item(name: str, item: str, *, host: str = "127.0.0.1", port: int = 25576,
                      password: str = "buddy") -> int:
    """精确数玩家某物品（`clear <p> <item> 0` 只数不清）。没有 RCON 返回 -1。"""
    try:
        import sys as _sys
        from pathlib import Path as _P
        root = _P(__file__).resolve().parent.parent
        if str(root / "orchestrator") not in _sys.path:
            _sys.path.insert(0, str(root / "orchestrator"))
        import re
        import rcon as _rcon                       # type: ignore
        rc = _rcon.Rcon(host, port, password).connect()
        try:
            out = rc.command(f"clear {name} {item} 0") or ""
        finally:
            rc.close()
        m = re.search(r"Found (\d+) matching", out)
        return int(m.group(1)) if m else 0
    except Exception:
        return -1


def read_chat(reg: Registry, n: int = 20) -> list[str]:
    """读最近 n 条 chat（只读，不发）。"""
    r = reg.invoke("getRecentChat", {"n": int(n)})
    d = r.data
    if isinstance(d, list):
        return [str(x) for x in d]
    if isinstance(d, dict):
        for key in ("_raw", "lines", "chat"):
            if isinstance(d.get(key), list):
                return [str(x) for x in d[key]]
    return []


def cmd_watch(reg: Registry, cmd: str, *, seconds: float = 8.0, settle: float = 0.6) -> dict:
    """下发一条 @命令，并把这段窗口里新出现的 chat 原样带回（诊断靠它，不靠猜）。"""
    before = read_chat(reg, 40)
    r = reg.invoke("run_command", {"cmd": cmd})
    time.sleep(settle)
    t0 = time.time()
    seen = list(before)
    while time.time() - t0 < seconds:
        cur = read_chat(reg, 40)
        if cur != seen[-40:]:
            seen.extend(x for x in cur if x not in seen)
        time.sleep(0.6)
    after = read_chat(reg, 40)
    new = [x for x in after if x not in before]
    return {"ok": r.ok, "err": r.err, "cmd": cmd, "new_chat": new[-30:]}


def wait_until(fn, timeout: float, period: float = 0.5, cancel=None) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cancel is not None and cancel():
            return False
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(period)
    return False


# ── 任务链哨兵（读 AltoClef 的目标链，判断"它还在不在干"） ──────────────────
def chain(reg: Registry) -> list[str]:
    """当前任务链（每条是任务的自述）。空列表 = 它手里没有活。

    为什么要读它：AltoClef 的失败不是报错，是**悄悄收工**。
    `AbstractObjectBlacklist` 给每个目标 4 次机会，用满就拉黑 45 秒并换目标；
    任务在 chat 里留下的是 `Costing …: attempt N / 4 — stepping aside for 45s`，
    外面看起来只是"卡住了"。读链才分得清"在干"和"已经放弃"。
    """
    r = reg.invoke("taskChain", {})
    d = r.data
    if isinstance(d, list):
        return [str(x) for x in d]
    if isinstance(d, dict):
        for k in ("_raw", "tasks", "lines"):
            if isinstance(d.get(k), list):
                return [str(x) for x in d[k]]
    return []


def wait_goal(reg: Registry, done_fn, *, timeout: float, expect: str = "",
              empty_grace: float = 8.0, period: float = 1.2, cancel=None) -> dict:
    """等一件事做完，但**不信任**它的"我在忙"：
       · done_fn() 为真 → 成功；
       · 任务链连续 empty_grace 秒为空，且 expect 也不再被提到 → 判"它自己收工了"，提前返回失败；
       · 超时 → 失败。
    """
    t0 = time.time()
    empty_at = None
    while time.time() - t0 < timeout:
        if cancel is not None and cancel():
            return {"ok": False, "sec": round(time.time() - t0, 1), "why": "已取消"}
        try:
            if done_fn():
                return {"ok": True, "sec": round(time.time() - t0, 1)}
        except Exception:
            pass
        tasks = chain(reg)
        joined = " | ".join(tasks)
        busy = bool(tasks) and (not expect or expect in joined)
        if busy:
            empty_at = None
        elif empty_at is None:
            empty_at = time.time()
        elif time.time() - empty_at > empty_grace:
            return {"ok": False, "sec": round(time.time() - t0, 1),
                    "why": "任务链已空（自己收工，不是我们等到超时）",
                    "last_tasks": tasks[-3:]}
        time.sleep(period)
    return {"ok": False, "sec": round(time.time() - t0, 1), "why": "超时"}


# ── 准星体检（2026-10-27 上午血证） ──────────────────────────────────────────
def aim_of(reg: Registry) -> dict:
    st = reg.invoke("getGameState", {})
    me = (st.data or {}).get("self") if (st.ok and isinstance(st.data, dict)) else {}
    me = me or {}
    try:
        return {"yaw": float(me.get("yaw")), "pitch": float(me.get("pitch")),
                "lookingAt": str(me.get("lookingAt"))}
    except Exception:
        return {}


def aim_alive(reg: Registry, *, step: float = 40.0, need: float = 1.0) -> dict:
    """**准星体检**：转一下头，看头部角度真的变了没有。

    为什么需要它（今天的现场）：
      客户端进程活着、命令能执行、聊天能打印、任务链有活 —— 但
      `lookAt` 返回 ok 而 yaw 只从 14.1 抖到 14.0，`Mining aborted … progress=-0.10`，
      `pathStatus.busy=true` 而坐标一动不动，AltoClef 的卡死检测刷到 `#102`。
      这是"渲染帧不再推进 ⇒ WindMouse 的瞄准不再推进"的死法，
      **靠坐标判据要等 10 秒才发现，靠准星判据 0.6 秒就能判**。

    返回 {"alive": bool, "d_yaw": .., "d_pitch": .., "lookingAt": ..}
    """
    a0 = aim_of(reg)
    if not a0:
        return {"alive": False, "why": "读不到朝向"}
    p = pos_of(reg)
    if not p:
        return {"alive": False, "why": "读不到坐标"}
    base_yaw = a0["yaw"]
    new_yaw = (base_yaw + step) % 360.0
    rad = math.radians(new_yaw)
    tx = int(round(p[0] - math.sin(rad) * 8))
    tz = int(round(p[2] + math.cos(rad) * 8))
    reg.invoke("lookAt", {"x": tx, "y": int(round(p[1])) + 2, "z": tz})
    time.sleep(0.6)
    a1 = aim_of(reg)
    d_yaw = abs((a1.get("yaw", base_yaw) - base_yaw + 180) % 360 - 180)
    d_pitch = abs(float(a1.get("pitch", 0.0)) - float(a0.get("pitch", 0.0)))
    alive = (d_yaw >= need) or (d_pitch >= need)
    return {"alive": alive, "d_yaw": round(d_yaw, 2), "d_pitch": round(d_pitch, 2),
            "lookingAt": a1.get("lookingAt"), "yaw": a1.get("yaw")}
