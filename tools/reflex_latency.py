#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/reflex_latency.py —— 量"反射弧"到底多长（operator 2026-10-29：「反射弧也太长了」）

反射弧 = 玩家做出一个动作（说一句 / 看一格 / 怪贴上来）到陪玩**开始动**的时间。
它由四段串起来，这个脚本四段都量：

    ① 一次 MCP 往返        getGameState / playerDetail / getEntities   ← 网络 + 客户端线程
    ② 一次观察            observe(fast=True)  vs  observe()            ← 旧版每 tick 4 个 RPC
    ③ 一次循环节拍         1 / hz                                     ← 旧版 3Hz = 333ms 量化误差
    ④ 请求 → 开始动作      bus.push → step() → execute()               ← 旧版作业期间直接 return

用法（身体活着时；会真的发请求，所以它会说一句话）：
    python -X utf8 tools/reflex_latency.py
    python -X utf8 tools/reflex_latency.py --n 12 --no-say

判据（本项目自定）：④ 的 p95 ≤ 600ms 就算"不迟钝"；① 的 p95 > 150ms 说明身体侧该查了。
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SETTINGS = str(Path(r"%BUDDY_CLIENT%"))


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1)))))
    return xs[k]


def ms(x: float) -> float:
    return round(x * 1000.0, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=25351)
    ap.add_argument("--player", default="Xiaorun")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--no-say", action="store_true", help="不真说话（只量 ①②③）")
    ap.add_argument("--json", action="store_true", help="最后一行打印 JSON（给闸读）")
    a = ap.parse_args()

    from companion.body import Body
    from companion.bus import Bus
    from companion.commander import Commander
    from companion.planner import RulePlanner
    from companion.registry import Registry

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = ROOT / "runs" / "reflex" / ts
    outdir.mkdir(parents=True, exist_ok=True)

    body = Body(a.port, SETTINGS)
    reg = Registry(body)
    rep: dict = {"ts": ts, "port": a.port, "n": a.n}

    # ① MCP 往返
    rpc: dict[str, list[float]] = {"getGameState": [], "playerDetail": [], "getEntities": []}
    for _ in range(a.n):
        t0 = time.time()
        reg.invoke("getGameState", {})
        rpc["getGameState"].append(time.time() - t0)
        t0 = time.time()
        reg.invoke("playerDetail", {"player": a.player})
        rpc["playerDetail"].append(time.time() - t0)
        t0 = time.time()
        reg.invoke("getEntities", {"limit": 8})
        rpc["getEntities"].append(time.time() - t0)
    rep["rpc_ms"] = {k: {"p50": ms(st.median(v)), "p95": ms(pct(v, 95)),
                         "p99": ms(pct(v, 99)), "max": ms(max(v))} for k, v in rpc.items()}

    # ② 观察成本
    cmdr = Commander(body, RulePlanner(requests=None), player=a.player,
                     outdir=outdir, clean_start=False, allow_restart=False,
                     bot_name="Buddy", bus=Bus())
    fast_t, full_t, full_rpc = [], [], []
    for i in range(max(3, a.n // 2)):
        t0 = time.time()
        cmdr.observe(fast=True)
        fast_t.append(time.time() - t0)
        n0 = getattr(body, "rpc_count", 0)
        t0 = time.time()
        cmdr.observe(fast=False)
        full_t.append(time.time() - t0)
        full_rpc.append(getattr(body, "rpc_count", 0) - n0)
    rep["observe_ms"] = {"fast_p50": ms(st.median(fast_t)), "fast_p95": ms(pct(fast_t, 95)),
                         "full_p50": ms(st.median(full_t)), "full_p95": ms(pct(full_t, 95))}
    # W6-4 判据：全量观察 **RPC ≤10**（结构指标，机器再忙也不该变）+ 延迟按"单次往返"折算
    # ⚠️ 绝对毫秒数会被机器负载左右（同一份代码：空闲时 66.9ms，客户端忙时 199ms；
    #    getGameState 本身 p50 就从 21ms 涨到 82ms）。所以延迟判据取
    #    `max(80ms, 3 × 单次往返)` —— 结构没退化就不该红，负载高时看放大倍数。
    base_ms = max(rep["rpc_ms"]["getGameState"]["p50"], 1.0)
    rep["observe_rpc"] = {"full_max": max(full_rpc) if full_rpc else 0,
                          "full_avg": round(sum(full_rpc) / len(full_rpc), 2) if full_rpc else 0}
    w64 = {"full_p95_ms": rep["observe_ms"]["full_p95"],
           "rpc_max": rep["observe_rpc"]["full_max"],
           "base_rpc_ms": base_ms,
           "budget_ms": round(max(80.0, 3.0 * base_ms), 1), "budget_rpc": 10}
    w64["ok"] = (w64["full_p95_ms"] <= w64["budget_ms"]) and (w64["rpc_max"] <= w64["budget_rpc"])
    rep["w6_4"] = w64

    # ③ 节拍
    rep["tick_ms"] = {"hz_default": 6.0, "period_ms": ms(1.0 / 6.0),
                      "old_hz": 3.0, "old_period_ms": ms(1.0 / 3.0)}

    # ④ 请求 → 开始动作（真的走一次 step()）
    lat = []
    if not a.no_say:
        for i in range(max(2, a.n // 3)):
            cmdr.bus.push("say", text=f"延迟测试{i}")
            t0 = time.time()
            try:
                cmdr.step()
            except Exception as e:
                rep.setdefault("step_err", []).append(f"{type(e).__name__}: {e}"[:120])
                break
            lat.append(time.time() - t0)
            time.sleep(0.2)
    rep["request_to_action_ms"] = {"p50": ms(st.median(lat)), "p95": ms(pct(lat, 95)),
                                   "samples": [ms(x) for x in lat]}

    budget = 600.0
    ok = (not lat) or pct(lat, 95) <= budget / 1000.0
    rep["verdict"] = "PASS" if ok else "FAIL"
    rep["budget_ms"] = budget
    (outdir / "reflex.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                        encoding="utf-8")

    print(f"[reflex] MCP 往返   " + "  ".join(
        f"{k} p50={v['p50']} p95={v['p95']} p99={v['p99']} max={v['max']}ms"
        for k, v in rep["rpc_ms"].items()), flush=True)
    print(f"[reflex] 观察       fast p50={rep['observe_ms']['fast_p50']}ms "
          f"p95={rep['observe_ms']['fast_p95']}ms ｜ full p50={rep['observe_ms']['full_p50']}ms "
          f"p95={rep['observe_ms']['full_p95']}ms", flush=True)
    print(f"[reflex] 全量 RPC   max={rep['observe_rpc']['full_max']} 次/tick "
          f"（均 {rep['observe_rpc']['full_avg']}） 预算 {w64['budget_rpc']} → "
          f"{'PASS' if w64['rpc_max'] <= w64['budget_rpc'] else 'FAIL'}", flush=True)
    print(f"[reflex] W6-4       full p95 {w64['full_p95_ms']}ms vs 折算预算 {w64['budget_ms']}ms"
          f"（单次往返 {w64['base_rpc_ms']}ms × 3）→ {'PASS' if w64['ok'] else 'FAIL'}", flush=True)
    print(f"[reflex] 节拍       现在 6Hz=167ms（旧 3Hz=333ms）", flush=True)
    if lat:
        print(f"[reflex] 请求→动作  p50={rep['request_to_action_ms']['p50']}ms "
              f"p95={rep['request_to_action_ms']['p95']}ms  预算 {budget:.0f}ms → "
              f"{rep['verdict']}", flush=True)
    print(f"[reflex] 产物: {outdir / 'reflex.json'}", flush=True)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False), flush=True)
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
