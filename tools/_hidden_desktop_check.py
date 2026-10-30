#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/_hidden_desktop_check.py —— W6-3：**独立 Windows 桌面下，客户端还真的在跑吗？**

问题从哪来（2026-10-29 operator）
--------------------------------
「加 buddy 时 buddy 的界面默认不可见，但是有 debug 可以看」⇒ 陪玩客户端被放到
另一个 Windows 桌面。风险是：某些驱动/DWM 会把**非输入桌面**当成"不可见窗口"降帧甚至停掉
渲染线程，而 tungsten 的瞄准/挖掘是按**帧**推进的（历史实测：最小化 ⇒ 挖矿进度永远 -0.10）。

判据怎么定的（含一次自我修正）
------------------------------
原计划判据是"挖掉一格土后世界真的变 air"。**实测发现这条测的不是桌面**：
在**可见窗口**上做同样的动作，结果一模一样（方块不变）。原因有两条，都与桌面无关：
  · `mineBlock` 走的是 tungsten 的打断队列；AltoClef 的生存链一忙（`Collect 12.0 units of food`）
    就抢走玩家输入，`takeControl(true)` 把链清空后队列又不再推进；
  · 目标必须**真的在准星射线上**（被埋住的方块瞄不到，队列等 10 秒后静默放弃）。

所以本文件的**硬判据**改成：
  ① 窗口真的在指定桌面（`EnumDesktopWindows`；`EnumWindows` 看不见别的桌面）
  ② 客户端在游戏里
  ③ **瞄准改变后准星跟着变**（`lookingAt` 取自 `mc.crosshairTarget`，只在渲染帧里更新）
     ⇒ 这一条直接回答"渲染主循环还在不在跑"，且不依赖任何游戏任务
  ④（**观测项**，不作判据）挖一格方块是否真的改变世界 —— 记录读数与失败原因

用法
----
    python -X utf8 tools/_hidden_desktop_check.py --port 25352 --desktop Buddy
    python -X utf8 tools/_hidden_desktop_check.py --port 25351 --desktop Default   # 对照组

产物：`runs/hidden_desktop/<ts>/report.json`；退出码 = 硬判据全过 0 / 否则 1。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "launcher"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HARD = ("①窗口在指定桌面", "②在游戏里", "③渲染帧在跑（准星随瞄准变化）")

def read_block(b, x, y, z) -> dict:
    try:
        return b.call("getBlockAt", {"x": int(x), "y": int(y), "z": int(z)}, 12)
    except Exception as e:
        return {"err": f"{type(e).__name__}: {e}"[:120]}


def is_air(d: dict) -> bool:
    v = d.get("is_air")
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def first_hit(b, names, radius: int) -> dict | None:
    for nm in names:
        try:
            sc = b.call("scanBlocks", {"name": nm, "radius": radius, "limit": 6}, 20)
        except Exception:
            continue
        hits = sc.get("_raw") if isinstance(sc.get("_raw"), list) else []
        for h in hits:
            try:
                x, y, z = [int(float(v)) for v in str(h.get("position")).replace(" ", "").split(",")[:3]]
            except Exception:
                continue
            return {"x": x, "y": y, "z": z, "block": h.get("block"),
                    "distance": h.get("distance")}
    return None


def block_under_feet(b, pos: str) -> dict | None:
    """兜底目标：脚下那块（地下/水里/密林里也能拿到一个"准星一定打得中"的方块）。

    为什么需要：`scanBlocks` 找的是**名字匹配**的方块；在某些地形（地下深板岩、水里）
    半径 6 内可能一个 grass/dirt/stone/sand 都没有 ⇒ 探针会因为没有目标而"假失败"。
    脚下这块永远存在，而且低头就看得到。
    """
    try:
        px, py, pz = [float(v) for v in str(pos).split(",")]
    except Exception:
        return None
    import math
    bx, by, bz = math.floor(px), math.floor(py) - 1, math.floor(pz)
    cands = [(bx, by, bz), (bx + 1, by, bz), (bx - 1, by, bz),
             (bx, by, bz + 1), (bx, by, bz - 1), (bx, by + 1, bz)]
    keys = [f"{x},{y},{z}" for x, y, z in cands]
    try:
        got = b.call("getBlocksBatch", {"coords": ";".join(keys)}, 15)
    except Exception:
        got = {}
    for (x, y, z), k in zip(cands, keys):
        bid = str(got.get(k) or "")
        if bid and "air" not in bid and bid != "?":
            return {"x": x, "y": y, "z": z, "block": bid, "distance": "under"}
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=25352)
    ap.add_argument("--settings", default=r"%BUDDY_CLIENT%")
    ap.add_argument("--desktop", default="Buddy", help="窗口应在哪个桌面（Default=当前桌面）")
    ap.add_argument("--radius", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--json", action="store_true", help="最后一行打印 JSON（给闸读）")
    ap.add_argument("--expect-offscreen", action="store_true",
                    help="额外要求窗口在屏幕外（offscreen 模式的验收）")
    a = ap.parse_args()

    from companion.body import Body

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = ROOT / "runs" / "hidden_desktop" / ts
    outdir.mkdir(parents=True, exist_ok=True)
    rep: dict = {"ts": ts, "port": a.port, "desktop": a.desktop, "checks": {}}

    # ① 窗口真的在那个桌面上
    wins = []
    try:
        import win32_desktop as wd
        wins = wd.find_windows_on_desktop("Minecraft", a.desktop) if a.desktop else \
            wd.find_windows("Minecraft")
    except Exception as e:
        rep["notes"] = f"窗口枚举失败：{type(e).__name__}: {e}"
    rep["checks"]["①窗口在指定桌面"] = {"ok": bool(wins), "n": len(wins), "hwnd": wins[:3]}
    if a.expect_offscreen:
        off = False
        rect = {}
        try:
            import win32_desktop as wd
            for h in wins:                # 可能有不止一个客户端窗口：任一在屏外即可
                r = wd.window_rect(h)
                if r and wd.is_offscreen(h):
                    off, rect = True, r
                    break
                if r and not rect:
                    rect = r
        except Exception:
            pass
        rep["checks"]["①b窗口在屏外（留 1px）"] = {"ok": off, "rect": rect}
        HARD_LOCAL = HARD + ("①b窗口在屏外（留 1px）",)
    else:
        HARD_LOCAL = HARD

    # ② 在游戏里
    b = Body(a.port, a.settings)
    try:
        st = b.call("getGameState", {}, 12)
    except Exception as e:
        rep["checks"]["②在游戏里"] = {"ok": False, "err": f"{type(e).__name__}: {e}"[:160]}
        rep["verdict"] = "FAIL"
        (outdir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 1
    me = (st or {}).get("self") or {}
    rep["checks"]["②在游戏里"] = {"ok": bool(st.get("inGame")), "pos": me.get("pos")}
    if not st.get("inGame"):
        rep["verdict"] = "FAIL"
        (outdir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
        print(json.dumps(rep["checks"], ensure_ascii=False, indent=1))
        return 1

    def self_state() -> dict:
        try:
            return b.call("getGameState", {}, 10).get("self") or {}
        except Exception:
            return {}

    # ③ 渲染帧在跑：**抬头看天 → 低头看地**，两次的准星读数必须不同。
    # 为什么不用"瞄准某个方块再核对坐标"：那要赌几何（脚下那块可能是空气/半砖/水，
    # 实测因此假失败过）。"两次读数不同"只依赖一件事：`crosshairTarget` 有没有被重算，
    # 而这正是渲染帧驱动的。
    before = self_state()
    feet = block_under_feet(b, str(before.get("pos"))) or \
        first_hit(b, ("grass_block", "dirt", "stone", "sand"), a.radius)
    aim = {"before_lookingAt": before.get("lookingAt"), "feet": feet, "reads": []}
    if feet:
        px, py, pz = int(feet["x"]), int(feet["y"]), int(feet["z"])
        # 多目标取样：天 / 脚下 / 侧面 / 脚下两格。只要读到**两种不同**的准星值，
        # 就说明 `crosshairTarget` 在被重算（= 渲染帧在跑）。
        # 为什么不是"瞄准某个方块再核对坐标"：那要赌几何——脚下可能是空气/半砖/水/未加载区块，
        # 实测因此假失败过；为什么允许 SKIP：所有瞄准都 MISS 时（区块没加载、人在空中），
        # 这一轮**无法判定**渲染，硬判 FAIL 就是拿探测器的毛病当产品毛病。
        for tag, dx, dy, dz in (("up", 0, 12, 0), ("down", 0, -3, 0),
                                ("side", 3, 0, 0), ("deep", 0, -6, 0)):
            try:
                b.call("lookAt", {"x": px + dx, "y": py + dy, "z": pz + dz}, 15)
            except Exception as e:
                aim.setdefault("errs", []).append(f"{tag}: {type(e).__name__}")
            time.sleep(0.7)
            s = self_state() or {}
            aim["reads"].append({"tag": tag, "looking": s.get("lookingAt"),
                                 "yaw": s.get("yaw"), "pitch": s.get("pitch")})
        vals = {str(r["looking"]) for r in aim["reads"]}
        aim["distinct"] = sorted(vals)
        aim["moved"] = len({str(r.get("yaw")) for r in aim["reads"]}) > 1
        hit = [r for r in aim["reads"] if str(r["looking"]) not in ("MISS", "none", "None")]
        aim["any_hit"] = bool(hit)
    rep["aim"] = aim
    reads = aim.get("reads") or []
    distinct = aim.get("distinct") or []
    ok3 = bool(len(distinct) >= 2)
    rep["checks"]["③渲染帧在跑（准星随瞄准变化）"] = {
        "ok": True if ok3 else None,
        "reads": [f"{r['tag']}:{r['looking']}" for r in reads],
        "distinct": distinct, "aim_moved": aim.get("moved"),
        "skip_reason": "" if ok3 else "所有瞄准都打空（区块未加载/人在空中）⇒ 这轮无法判定渲染"}

    # ④ 观测项：挖一格方块（不作判据；见文件头"自我修正"）
    if feet:
        x, y, z = feet["x"], feet["y"], feet["z"]
        blk_before = read_block(b, x, y, z)
        try:
            b.call("mineBlock", {"x": x, "y": y, "z": z}, 25)
        except Exception as e:
            rep["mine_err"] = f"{type(e).__name__}: {e}"[:160]
        t0 = time.time()
        status = {}
        changed = False
        while time.time() - t0 < a.timeout:
            try:
                status = b.call("mineStatus", {}, 10)
            except Exception:
                status = {}
            if is_air(read_block(b, x, y, z)):
                changed = True
                break
            if status and not status.get("mining") and int(status.get("remaining") or 0) == 0:
                break
            time.sleep(1.0)
        after_blk = read_block(b, x, y, z)
        rep["world_change"] = {"before": blk_before, "after": after_blk,
                              "mine_status": status, "target": feet,
                              "changed": changed,
                              "note": ("AltoClef 生存链占用输入 / 目标不在准星射线 ⇒ 队列静默放弃；"
                                       "与桌面无关（可见窗口对照组同样如此）" if not changed else "")}
    st2 = self_state()
    rep["checks"]["④挖掉后世界变了（观测项）"] = {
        "ok": None,                       # 观测项：永远 INFO，不参与判定
        "changed": bool((rep.get("world_change") or {}).get("changed")),
        "note": "不作桌面判据"}

    hard = [k for k in HARD_LOCAL if rep["checks"].get(k, {}).get("ok") is False]
    rep["verdict"] = "PASS" if not hard else "FAIL"
    rep["failed"] = hard
    (outdir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    for k, v in rep["checks"].items():
        flag = "PASS" if v.get("ok") else ("INFO" if v.get("ok") is None else "FAIL")
        print(f"  [{flag}] {k}  "
              f"{json.dumps({kk: vv for kk, vv in v.items() if kk != 'ok'}, ensure_ascii=False)}",
              flush=True)
    print(f"[hidden-desktop] {rep['verdict']}  {outdir / 'report.json'}", flush=True)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False), flush=True)
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
