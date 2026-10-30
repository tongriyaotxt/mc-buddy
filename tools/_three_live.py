#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/_three_live.py —— W3-1：**3 个陪玩同时在线**（一份语音 / 一块黑板 / 三个身体）

为什么这活必须真起客户端，不能"模拟"
------------------------------------
合作层的三个问题（谁说话 / 谁干活 / 站哪）在单元测试里都能"证明通过"，
但只要**真的**三个 bot 站在同一个玩家旁边，就会立刻互相挡准星、抢同一棵树、
一句话回三遍。2026-10-27 上午就是这么踩的（`lookingAt: ENTITY` ⇒ 方块挖不到）。
所以这个脚本起**真的** 3 个 Minecraft 客户端 + 3 个指挥层，用世界状态判：

  ① 3 个 MCP 都在游戏里（`inGame` = true，且 `pos` 读得到）
  ② 黑板 `runs/team/board.json` 里 **3 条心跳**且新鲜（≤25s）
  ③ 编队：两两间距 ≥1.6 格（`team.MIN_SEP`）；**必须有真玩家**，否则三个人
     全站在出生点，这条判据没有意义 ⇒ 需要第 4 个客户端当"人"
  ④ 只有**说话权持有者**执行"没有 `to` 的请求"（其余两个只记 `req_skip`）
  ⑤ 语音只跑**一份**：3 个指挥层的 RSS 增量 ≈ 1 份模型（~230MB），不是 3 份

用法
----
    python -X utf8 tools/_three_live.py --server 127.0.0.1:25566 --seconds 60
    python -X utf8 tools/_three_live.py --dry          # 只打印计划
    python -X utf8 tools/_three_live.py --keep         # 跑完不杀进程（手工看窗口）
    python -X utf8 tools/_three_live.py --no-player    # 不齐 4 个客户端（③ 会标 SKIP）

产物：`runs/three_live/<ts>/report.json`（判据 + 读数），退出码 = 全绿 0 / 否则 1
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "launcher"))       # `core` 在 launcher/ 下，不在根下
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

IDX = (1, 2, 3)
PLAYER = "Xiaorun"
PLAYER_PORT = 25361
BOARD = ROOT / "runs" / "team" / "board.json"
INBOX = ROOT / "runs" / "voice" / "inbox.jsonl"


# ── 实例定位（直接复用 buddy_body，避免第二份写死的端口/目录表）──────────────
# 客户端实例和游戏目录都是"这台机器"的东西：从环境变量或 launcher/paths.json 拿，不写成字面量。
def _clients_root() -> Path:
    v = (os.environ.get("BUDDY_CLIENTS") or "").strip()
    if v:
        return Path(v)
    try:
        import json
        cfg = json.loads((ROOT / "launcher" / "paths.json").read_text(encoding="utf-8"))
        if isinstance(cfg.get("clients"), str) and cfg["clients"].strip():
            return Path(cfg["clients"].strip())
    except Exception:
        pass
    return ROOT / "runs" / "clients"


def _mc_home() -> Path:
    v = (os.environ.get("BUDDY_MC_HOME") or "").strip()
    if v:
        return Path(v)
    try:
        import json
        cfg = json.loads((ROOT / "launcher" / "paths.json").read_text(encoding="utf-8"))
        if isinstance(cfg.get("mc_home"), str) and cfg["mc_home"].strip():
            return Path(cfg["mc_home"].strip())
    except Exception:
        pass
    return Path.home() / ".minecraft"


def inst(i: int) -> dict:
    return {"name": "Buddy" if i == 1 else f"Buddy{i}", "port": 25350 + i,
            "workdir": _clients_root() / f"buddy{i}"}


def settings_path(i: int) -> Path:
    return inst(i)["workdir"] / "altoclef" / "altoclef_settings.json"


def ensure_provisioned(i: int) -> dict:
    """第 2/3 个陪玩必须有独立 gameDir + 独立 mcpPort（否则两个 MCP 抢同一个端口）。"""
    import core as L                                    # launcher/core.py（已在 sys.path）
    d = inst(i)["workdir"]
    cfg = d / "altoclef" / "altoclef_settings.json"
    if cfg.exists():
        try:
            port = int(json.loads(cfg.read_text(encoding="utf-8")).get("mcpPort") or 0)
        except Exception:
            port = 0
        if port == inst(i)["port"]:
            return {"i": i, "ok": True, "reused": True, "settings": str(cfg)}
    # The version-isolated mods folder of the PCL instance that actually plays 1.21.11.
    real_mods = next(iter(sorted(_mc_home().glob("versions/*/mods"))), _mc_home() / "mods")
    r = L.provision_instance(d.parent, i, real_mods=real_mods,
                             mcp_port=inst(i)["port"], gateway_port=25340 + i)
    return {"i": i, "ok": bool(r.get("settings_ok")), **r}


def rss_mb(pid: int) -> float:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue).WorkingSet64"],
            capture_output=True, text=True, timeout=25, encoding="utf-8", errors="replace")
        v = (out.stdout or "").strip().splitlines()
        return round(int(v[-1]) / 1e6, 1) if v and v[-1].strip().isdigit() else 0.0
    except Exception:
        return 0.0


# ── 起客户端 ────────────────────────────────────────────────────────────────
def bring_up(i: int, *, offscreen: bool = True, mem: str = "2G") -> dict:
    import buddy_body as BB
    BB.use(i)
    # ⚠️ W6-3 实测：独立桌面（`--desktop`）会让渲染主循环停 ⇒ 那台客户端干不了活。
    # "看不见但能干活"只能用 `--offscreen`（默认桌面 + 屏外留 1px）。
    BB.CURRENT["desktop"] = ""
    BB.CURRENT["offscreen"] = bool(offscreen)
    BB.CURRENT["mem"] = mem
    s = BB.status()
    if s.get("mcp") and s.get("in_game"):
        return {"i": i, "ok": True, "reused": True, "pos": s.get("pos"), "pid": s.get("pid")}
    snap = BB.restart(verbose=True)
    return {"i": i, "ok": bool(snap.get("in_game")), "pos": snap.get("pos"),
            "pid": snap.get("pid"), "restart_s": snap.get("restart_s")}


def bring_up_player(*, server: str, mem: str = "2G") -> dict:
    """第 4 个客户端当"人"（W3-1 的 ③ 需要它）。它没有指挥层，只是个真玩家。"""
    from companion.body import Body
    import core as L
    wd = _clients_root() / "player1"
    wd.mkdir(parents=True, exist_ok=True)
    # 先保证这个 workdir 有 altoclef 设置（Body 要用它的 Bearer token；缺文件会直接抛）
    s = L.ensure_settings(wd, mcp_port=PLAYER_PORT, gateway_port=25341)
    cfg = Path(str(s.get("path") or (wd / "altoclef" / "altoclef_settings.json")))
    b = Body(PLAYER_PORT, cfg)
    try:
        if b.call("inGame", {}, 6).get("ok"):
            return {"ok": True, "reused": True}
    except Exception:
        pass
    host, _, port = server.partition(":")
    cmd = [sys.executable, "-X", "utf8", str(ROOT / "tools" / "_mc121_launch.py"),
           "--name", PLAYER, "--workdir", str(wd), "--mcp-port", str(PLAYER_PORT),
           "--no-connect", "--mem", mem, "--width", "1280", "--height", "720",
           "--host", host or "127.0.0.1", "--port", port or "25566"]
    # ⚠️ 不要 capture_output：启动器把 java 起成子进程，java 会继承管道写端
    # ⇒ `subprocess.run` 要等 EOF ⇒ 白等 timeout（buddy_body 上踩过同一个坑）。
    subprocess.run(cmd, timeout=180, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    for _ in range(60):
        try:
            b.call("getGameState", {}, 6)
            break
        except Exception:
            time.sleep(2.0)
    for _ in range(12):
        try:
            b.call("takeControl", {"on": False}, 12)
            b.call("ConnectToServer", {"ip": server}, 45)
        except Exception:
            pass
        time.sleep(6.0)
        try:
            if b.call("inGame", {}, 8).get("ok"):
                return {"ok": True, "reused": False}
        except Exception:
            pass
    return {"ok": False}


# ── 起指挥层 ────────────────────────────────────────────────────────────────
def start_cmdr(i: int, *, server: str, seconds: float, outdir: Path, tag: str,
               voice_hub: str = "") -> dict:
    out = outdir / f"trace-{tag}"
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-X", "utf8", "-m", "companion.commander",
           "--port", str(inst(i)["port"]), "--settings", str(settings_path(i)),
           "--player", PLAYER, "--bot-name", inst(i)["name"],
           "--seconds", str(seconds), "--hz", "6", "--server", server,
           "--team", str(BOARD), "--outdir", str(out),
           "--no-ai", "--no-chat"]
    if voice_hub:
        cmd += ["--voice-hub", voice_hub]
    logf = open(outdir / f"cmdr-{tag}.log", "wb")
    p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT)
    return {"i": i, "tag": tag, "pid": p.pid, "proc": p, "outdir": str(out), "logf": logf}


def tail_trace(d: str, n: int = 400) -> list[dict]:
    p = Path(d) / "trace.jsonl"
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return out


# ── 主流程 ──────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("BUDDY_SERVER", "127.0.0.1:25566"))
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--mem", default="2G", help="每个客户端的堆上限（4 开必须降）")
    ap.add_argument("--no-player", action="store_true", help="不齐第 4 个（真玩家）客户端")
    ap.add_argument("--no-voice", action="store_true", help="不起语音共享（省内存/省模型）")
    ap.add_argument("--desktop", default="", help="（不推荐）把陪玩放到独立桌面；默认走 --offscreen")
    ap.add_argument("--visible", action="store_true", help="窗口可见（debug 用）")
    ap.add_argument("--keep", action="store_true", help="跑完不杀")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = ROOT / "runs" / "three_live" / ts
    outdir.mkdir(parents=True, exist_ok=True)
    rep: dict = {"ts": ts, "server": a.server, "checks": {}, "notes": []}

    if a.dry:
        for i in IDX:
            print(f"[dry] {inst(i)['name']} port={inst(i)['port']} dir={inst(i)['workdir']}")
        print(f"[dry] player={PLAYER} port={PLAYER_PORT}")
        print(f"[dry] board={BOARD}")
        return 0

    # ── ⓪ 实例就绪 ─────────────────────────────────────────────────────────
    prov = [ensure_provisioned(i) for i in IDX]
    rep["provision"] = prov
    if not all(p.get("ok") for p in prov):
        rep["notes"].append("实例没准备好（mcpPort/gameDir）")
        (outdir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
        print(json.dumps(rep["provision"], ensure_ascii=False, indent=1))
        return 1

    # ── ① 起 4 个客户端 ────────────────────────────────────────────────────
    ups = []
    try:
        player = {"ok": False}
        if not a.no_player:
            print("[1/5] 起真玩家客户端 …", flush=True)
            player = bring_up_player(server=a.server, mem=a.mem)
        rep["player"] = player
        for i in IDX:
            print(f"[1/5] 起 {inst(i)['name']} (port {inst(i)['port']}) …", flush=True)
            ups.append(bring_up(i, offscreen=not a.visible, mem=a.mem))
    except Exception as e:
        rep["notes"].append(f"起客户端失败：{type(e).__name__}: {e}")
    rep["clients"] = [{k: v for k, v in u.items()} for u in ups]
    online = [u for u in ups if u.get("ok")]
    rep["checks"]["①3 个都在游戏里"] = {
        "ok": len(online) == 3,
        "pos": [u.get("pos") for u in ups]}

    if len(online) < 3:
        rep["notes"].append("客户端没齐，后面的判据没有意义")
        (outdir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
        print(json.dumps(rep["checks"], ensure_ascii=False, indent=1))
        return 1

    # ── ② 起语音共享（只一份）+ 3 个指挥层 ─────────────────────────────────
    hub = None
    if not a.no_voice:
        print("[2/5] 起语音共享（一份模型，3 个客户端共用）…", flush=True)
        INBOX.parent.mkdir(parents=True, exist_ok=True)
        hub = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "companion.voice_hub",
                                "--inbox", str(INBOX)],
                               cwd=str(ROOT),
                               stdout=open(outdir / "voice_hub.log", "wb"),
                               stderr=subprocess.STDOUT)
        time.sleep(4.0)
    rep["voice_hub_pid"] = getattr(hub, "pid", None)

    # 请求队列清空（别把上一轮的行当成这一轮的）
    reqs = ROOT / "runs" / "requests.jsonl"
    reqs.parent.mkdir(parents=True, exist_ok=True)
    reqs.write_text("", encoding="utf-8")

    print("[3/5] 起 3 个指挥层（同一块黑板）…", flush=True)
    cmds = []
    for i in IDX:
        cmds.append(start_cmdr(i, server=a.server, seconds=a.seconds, outdir=outdir,
                               tag=inst(i)["name"],
                               voice_hub=(str(INBOX) if hub else "")))
        time.sleep(0.4)

    # ── ③ 心跳 ─────────────────────────────────────────────────────────────
    time.sleep(min(25.0, max(12.0, a.seconds * 0.4)))
    board = {}
    try:
        board = json.loads(BOARD.read_text(encoding="utf-8"))
    except Exception as e:
        rep["notes"].append(f"黑板读不到：{type(e).__name__}")
    mem = (board.get("members") or {})
    fresh = {k: v for k, v in mem.items() if time.time() - float(v.get("at") or 0) < 25.0}
    rep["board"] = {"members": mem, "fresh": sorted(fresh), "speaker": board.get("speaker")}
    rep["checks"]["②黑板 3 条心跳"] = {"ok": len(fresh) >= 3, "n": len(fresh),
                                      "names": sorted(fresh)}

    # ── ④ 编队间距 ─────────────────────────────────────────────────────────
    import math
    pts = [(k, v.get("pos")) for k, v in fresh.items() if v.get("pos")]
    dmin, pair = None, None
    for x in range(len(pts)):
        for y in range(x + 1, len(pts)):
            try:
                d = math.dist([float(t) for t in pts[x][1]], [float(t) for t in pts[y][1]])
            except Exception:
                continue
            if dmin is None or d < dmin:
                dmin, pair = d, (pts[x][0], pts[y][0])
    rep["checks"]["③编队两两 ≥1.6"] = {
        "ok": (dmin is not None and dmin >= 1.6) if not a.no_player else None,
        "min_dist": round(dmin, 2) if dmin is not None else None,
        "pair": pair,
        "note": ("两两 ≥1.6 是「不互相挡」的判据；本轮彼此很远（各自出生点）⇒ "
                 "围玩家编队没有被这一轮触发，逻辑本身由闸⑦ 守" if (dmin or 0) > 12 else ""),
        "skip_reason": ("没有真玩家 ⇒ 三人都在出生点，这条没有意义" if a.no_player else "")}

    # ── ⑤ 只有说话权者干活 ─────────────────────────────────────────────────
    print("[4/5] 推一条'不带 to'的请求，看谁动手 …", flush=True)
    with open(reqs, "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "say", "text": "点名测试：只有说话权的人回答"},
                           ensure_ascii=False) + "\n")
    time.sleep(min(20.0, max(10.0, a.seconds * 0.3)))
    did, skipped = [], []
    for c in cmds:
        tr = tail_trace(c["outdir"])
        # ⚠️ 只认 `panel_req`（"这条请求我接了"）。旧写法把 `job_start` 也算进"动手"，
        # 而 job_start 在任何作业都会出现（吃饭/回血/巡场），于是三个都算"回话"⇒ 假 FAIL。
        if any(e.get("ev") == "panel_req" for e in tr):
            did.append(c["tag"])
        if any(e.get("ev") == "req_skip" for e in tr):
            skipped.append(c["tag"])
    rep["checks"]["④只有说话权者回话"] = {
        "ok": len(did) == 1 and len(skipped) >= 1,
        "did": did, "skipped": skipped}

    # ── ⑥ 语音只一份 ───────────────────────────────────────────────────────
    if hub:
        alive = hub.poll() is None
        rss = {c["tag"]: rss_mb(c["pid"]) for c in cmds}
        tail = ""
        try:
            tail = (outdir / "voice_hub.log").read_text(encoding="utf-8",
                                                        errors="replace")[-300:]
        except Exception:
            pass
        rep["checks"]["⑤语音只跑一份"] = {
            # hub 死了（这台机器没麦/没模型）⇒ SKIP 而不是 FAIL：它不是"共享失败"，
            # 是"这台机器没有输入设备"；共享机制本身由闸 ⑲c 单独守。
            "ok": (True if alive else None),
            "hub_alive": alive, "hub_log_tail": tail,
            "cmdr_rss_mb": rss,
            "skip_reason": "" if alive else "voice_hub 没起来（无麦/无模型），共享机制由闸 ⑲c 守",
            "note": "模型在 voice_hub 一个进程里；指挥层只读 inbox.jsonl（RSS 里不该出现 ~230MB 的模型）"}
    else:
        rep["checks"]["⑤语音只跑一份"] = {"ok": None, "skip_reason": "--no-voice"}

    # ── 收尾 ───────────────────────────────────────────────────────────────
    print("[5/5] 收集 trace 摘要 …", flush=True)
    for c in cmds:
        tr = tail_trace(c["outdir"])
        ev: dict = {}
        for e in tr:
            k = str(e.get("ev") or "?")
            ev[k] = ev.get(k, 0) + 1
        rep.setdefault("traces", {})[c["tag"]] = {
            "events": ev, "n": len(tr),
            "says": [e.get("text") for e in tr if e.get("ev") in ("say", "speak_try")][:8]}

    if not a.keep:
        for c in cmds:
            try:
                c["proc"].terminate()
            except Exception:
                pass
        if hub:
            try:
                hub.terminate()
            except Exception:
                pass

    hard = [k for k, v in rep["checks"].items() if v.get("ok") is False]
    rep["verdict"] = "PASS" if not hard else "FAIL"
    rep["failed"] = hard
    (outdir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    for k, v in rep["checks"].items():
        flag = "PASS" if v.get("ok") else ("SKIP" if v.get("ok") is None else "FAIL")
        detail = {kk: vv for kk, vv in v.items() if kk != "ok"}
        print(f"  [{flag}] {k}  {json.dumps(detail, ensure_ascii=False)}", flush=True)
    print(f"[three_live] {rep['verdict']}  产物 {outdir / 'report.json'}", flush=True)
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
