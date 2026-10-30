#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/buddy_body.py —— 陪玩身体（Buddy 客户端）的守护进程：查活 / 拉活 / 重启 / 进服

为什么需要它（2026-10-26 实验结论）：
  身体会"冻住" —— MCP 通、getGameState 正常、hp 满，但 gotoXYZ 下发后位移 **0.00**，
  chat 里是 "FastNavigator: no progress … giving the route up"。
  同一时刻重启客户端（不重置 playerdata）后，同样的 gotoXYZ 4 秒走了 **12.20 格**。
  ⇒ 冻结是**客户端会话状态**，唯一可靠解法是重启客户端（约 40 秒）。

所以"身体可动"不能靠祈祷，要靠这条守护：
  status   —— 打印 pid / MCP / inGame / pos
  restart  —— 杀掉 Buddy 的 javaw（按 --username Buddy 精确定位，不碰真人客户端），
              用 --no-connect 重拉（必须：quickPlay 会让 mod 半初始化，SlotHandler NPE + 无 MCP），
              等 MCP 起来 → ConnectToServer → 等 inGame
  ensure   —— 只在需要时修（MCP 不通 / 不在游戏里）

用法:
  python -X utf8 tools/buddy_body.py status
  python -X utf8 tools/buddy_body.py restart
  python -X utf8 tools/buddy_body.py ensure
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.body import Body  # noqa: E402

NAME = "Buddy"
MCP_PORT = 25351
WORKDIR = Path(r"%BUDDY_CLIENT%")
SETTINGS = WORKDIR / "altoclef" / "altoclef_settings.json"
LAUNCHER = ROOT / "tools" / "_mc121_launch.py"
# ⚠️ 2026-10-29：以前这里写死 25565。换服到 25566 后，健康阶梯的重启会连错端口
# ⇒ 客户端永远进不去 ⇒ 外面看就是"卡死/不出活"。现在走环境变量，命令行也能覆盖。
SERVER = os.environ.get("BUDDY_SERVER", "127.0.0.1:25565")
W, H = 1280, 720


# ── 多实例（2026-10-27）：每个陪玩一套名字/端口/工作目录 ─────────────────────
# 以前这里全写死 Buddy/25351，于是 `restart` 永远只重启第一个客户端，
# 第二个（Buddy2）崩了没有任何工具能修它 —— 双人协作测试就是卡在这。
def instance(i: int) -> dict:
    return {"i": i, "name": "Buddy" if i == 1 else f"Buddy{i}", "port": 25350 + i,
            "workdir": Path(r"%BUDDY_CLIENT%" if i == 1 else rf"%BUDDY_CLIENT%")}


CURRENT = {"name": NAME, "port": MCP_PORT, "workdir": WORKDIR, "settings": SETTINGS,
           "desktop": "", "mem": "4G", "offscreen": False}


def use(i: int | None = None, name: str | None = None) -> dict:
    """选定要操作哪一个陪玩（--index 2 或 --name Buddy2）。"""
    idx = i
    if idx is None and name:
        idx = 1 if name == "Buddy" else int("".join(ch for ch in name if ch.isdigit()) or 1)
    if idx is None:
        return CURRENT
    inst = instance(idx)
    CURRENT.update({"name": inst["name"], "port": inst["port"],
                    "workdir": inst["workdir"],
                    "settings": inst["workdir"] / "altoclef" / "altoclef_settings.json"})
    return CURRENT


# ── 进程定位（按 --username 精确区分两个客户端） ────────────────────────────
def find_pid(name: str | None = None) -> int | None:
    name = name or CURRENT["name"]
    # 客户端是 java.exe（启动器用 JAVA_CONSOLE），不是 javaw.exe —— 少写一个就会永远找不到进程
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='java.exe' or Name='javaw.exe'\" | "
          "Where-Object { $_.CommandLine -match '--username\\s+%s(\\s|$)' } | "
          "Select-Object -ExpandProperty ProcessId" % name)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=40,
                           encoding="utf-8", errors="replace")
    except Exception:
        return None
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def kill(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, timeout=40)
    except Exception:
        pass


# ── MCP 探活 ────────────────────────────────────────────────────────────────
def body() -> Body:
    return Body(CURRENT["port"], CURRENT["settings"])


def probe(b: Body) -> dict:
    """一次拿全：MCP 通不通 / 在不在游戏 / 坐标 / hp。"""
    out = {"mcp": False, "in_game": False, "pos": None, "hp": None}
    try:
        b.call("getGameState", {}, 8)
        out["mcp"] = True
    except Exception as e:
        out["err"] = f"{type(e).__name__}: {e}"[:120]
        return out
    try:
        r = b.call("inGame", {}, 6)
        out["in_game"] = bool(r.get("ok")) if isinstance(r, dict) else False
    except Exception:
        pass
    try:
        st = b.call("getGameState", {}, 10)
        me = (st or {}).get("self") or {}
        out["pos"] = me.get("pos")
        out["hp"] = me.get("hp")
    except Exception:
        pass
    return out


def wait_mcp(b: Body, timeout: float = 150.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            b.call("getGameState", {}, 6)
            return True
        except Exception:
            time.sleep(2.0)
    return False


def kick_ghost(name: str | None = None, *, rcon_port: int = 25576,
               rcon_pw: str = "buddy") -> str:
    """踢掉服务端上的**幽灵会话**。

    2026-10-27 血证：客户端被 kill 后，服务端还挂着同名玩家（`list` 里能看到 Buddy），
    这时新客户端 `ConnectToServer` 会**回 ok 然后静默退回主菜单**（服务端判重复登录），
    看起来就是"连不上、也没报错"。实测 `/kick Buddy` 之后同一条命令立刻就进去了。
    """
    nm = name or CURRENT["name"]
    try:
        import sys as _s
        from pathlib import Path as _P
        root = _P(__file__).resolve().parent.parent
        if str(root / "orchestrator") not in _s.path:
            _s.path.insert(0, str(root / "orchestrator"))
        import rcon as _rcon                       # type: ignore
        rc = _rcon.Rcon("127.0.0.1", rcon_port, rcon_pw).connect()
        try:
            listing = rc.command("list") or ""
            if nm in listing:
                rc.command(f"kick {nm}")
                return f"kicked ghost {nm}"
            return "no ghost"
        finally:
            rc.close()
    except Exception as e:
        return f"rcon unavailable: {type(e).__name__}"


def ensure_ingame(b: Body, timeout: float = 200.0) -> bool:
    """让客户端真的进服。

    ⚠️ 2026-10-27 血证两条：
    1. `ConnectToServer` 走的是 AltoClef 的 `gameMenuTaskChain`，**TaskRunner 没解暂停时
       它什么都不做，还照样回 ok**（先 `@unpause` 再连，12 秒进服）；
    2. 客户端被 kill 后服务端会留**幽灵会话**，这时连接也会静默失败 ⇒ 先 `/kick` 掉。

    ⚠️ 2026-10-29 血证第三条（这一版重写的原因）：**`ConnectToServer` 是异步的**。
    实测下令到真的进服要 **30~110 秒**（22:14:56 才连上，而下令在 22:13:0x），
    而旧版每轮迭代自己要阻塞 ~65 秒（takeControl 12 + ConnectToServer 45 + sleep 8），
    于是 120 秒预算里只跑得完 2 轮 —— **客户端刚连上，函数就超时返回 False**。
    现在：**下令一次，然后每 2 秒轮询 inGame**；只有在"等了 100 秒还没进去"时才关屏重下一次令。
    """
    t0 = time.time()
    kicked = False
    rounds = 0
    while time.time() - t0 < timeout and rounds < 4:
        rounds += 1
        try:
            if b.call("inGame", {}, 8).get("ok"):
                return True
        except Exception:
            pass
        if not kicked:
            msg = kick_ghost()
            if "kicked" in msg:
                print(f"[ensure_ingame] {msg}", flush=True)
            kicked = True
        # 一轮 = 关屏 → 解暂停 → 下令 → **每 2 秒轮询 60 秒**
        # （实测：`ConnectToServer` 是异步的，下令到真的进服要 30~110 秒；
        #  而手工能成功、旧循环不成功的差别就藏在"轮询间隔"和"重下令的时机"里）
        try:
            b.call("closeScreen", {}, 10)
        except Exception:
            pass
        try:
            b.call("takeControl", {"on": False}, 12)
        except Exception:
            try:
                b.call("ExecuteCommand", {"cmd": "@unpause"}, 12)
            except Exception:
                pass
        try:
            b.call("ConnectToServer", {"ip": SERVER}, 45)
        except Exception:
            pass
        if rounds == 1:
            print("[ensure_ingame] 已下发连接令，开始轮询（进服本身要 30~110 秒）", flush=True)
        r0 = time.time()
        while time.time() - r0 < 60.0 and time.time() - t0 < timeout:
            time.sleep(2.0)
            try:
                if b.call("inGame", {}, 8).get("ok"):
                    return True
            except Exception:
                pass
    return False


# ── 重启 ────────────────────────────────────────────────────────────────────
def restart(verbose: bool = True) -> dict:
    """杀掉 → 重拉（--no-connect）→ 等 MCP → 进服。返回结果快照。"""
    t0 = time.time()
    old = find_pid()
    if old:
        if verbose:
            print(f"[restart] kill pid={old}", flush=True)
        kill(old)
        for _ in range(30):
            if find_pid() is None:
                break
            time.sleep(1.0)
    time.sleep(3.0)

    cmd = [sys.executable, "-X", "utf8", str(LAUNCHER),
           "--name", CURRENT["name"], "--workdir", str(CURRENT["workdir"]),
           "--mcp-port", str(CURRENT["port"]),
           "--no-connect", "--width", str(W), "--height", str(H),
           "--mem", str(CURRENT.get("mem") or "4G")]
    # 2026-10-29：operator「buddy 的界面默认不可见」。
    # ⚠️ 实测两条路差别巨大（W6-3）：
    #   · `--desktop Buddy`（独立桌面）**渲染主循环会停** ⇒ 准星不更新 ⇒ 挖矿/瞄准全废；
    #   · `--offscreen`（默认桌面 + 屏幕外留 1px）渲染照常，看不见但能干。
    # 所以"看不见"一律走 offscreen；desktop 只留给要复现那个 bug 的探针。
    if CURRENT.get("offscreen"):
        cmd += ["--offscreen"]
    elif CURRENT.get("desktop"):
        cmd += ["--desktop", str(CURRENT["desktop"])]
    if verbose:
        print(f"[restart] launch {' '.join(cmd[2:])}", flush=True)
    # ⚠️ 2026-10-29：**不要用 capture_output**。独立桌面那条路是用 `CreateProcessW` 起客户端的
    # （bInheritHandles=True），java 进程会**继承**这里的管道写端 ⇒ 启动脚本自己早就退出了，
    # 但 `subprocess.run(capture_output=True)` 要等管道 EOF ⇒ 一直等到 180 秒超时，
    # 外面看就是"restart 卡死、什么都不打印"。丢 DEVNULL 即可（真日志在客户端的 log 里）。
    subprocess.run(cmd, timeout=180, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)

    b = body()
    ok = wait_mcp(b, 150)
    if verbose:
        print(f"[restart] mcp={ok} after {time.time() - t0:.1f}s", flush=True)
    ing = ensure_ingame(b, 200) if ok else False
    snap = probe(b) if ok else {"mcp": False, "in_game": False}
    snap["restart_s"] = round(time.time() - t0, 1)
    snap["pid"] = find_pid()
    snap["ingame_ok"] = ing
    if verbose:
        print(f"[restart] done {json.dumps(snap, ensure_ascii=False)}", flush=True)
    return snap


def status() -> dict:
    b = body()
    s = probe(b)
    s["pid"] = find_pid()
    s["port"] = CURRENT["port"]
    s["name"] = CURRENT["name"]
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "restart", "ensure", "kill"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--index", type=int, default=None, help="第几个陪玩（1 起）")
    ap.add_argument("--name", default="", help="用户名（Buddy / Buddy2 …）")
    ap.add_argument("--server", default="",
                    help="服务器地址 host:port（默认读 BUDDY_SERVER 或 127.0.0.1:25565）")
    ap.add_argument("--desktop", default="",
                    help="把这个客户端放到指定 Windows 桌面（⚠️ 实测该桌面渲染会停；"
                         "要'看不见但能干活的'用 --offscreen）")
    ap.add_argument("--offscreen", action="store_true",
                    help="窗口挪到屏幕外只留 1px（看不见 / 渲染照常，推荐）")
    ap.add_argument("--mem", default="",
                    help="堆上限（默认 4G；多开时用 2G/3G 省内存）")
    a = ap.parse_args()
    if a.server:
        globals()["SERVER"] = a.server
    use(a.index, a.name or None)
    if a.desktop:
        CURRENT["desktop"] = a.desktop
    if a.offscreen:
        CURRENT["offscreen"] = True
    if a.mem:
        CURRENT["mem"] = a.mem

    if a.cmd == "status":
        s = status()
        print(json.dumps(s, ensure_ascii=False))
        return 0 if s.get("mcp") else 1

    if a.cmd == "kill":
        p = find_pid()
        if p:
            kill(p)
            print(f"killed {p}")
        else:
            print("no pid")
        return 0

    if a.cmd == "restart":
        s = restart()
        print(json.dumps(s, ensure_ascii=False))
        return 0 if s.get("in_game") else 1

    # ensure：能不动就不动
    s = status()
    if s.get("mcp") and s.get("in_game"):
        print(json.dumps(s, ensure_ascii=False))
        return 0
    if s.get("mcp") and not s.get("in_game"):
        ensure_ingame(body(), 90)
    else:
        restart()
    s = status()
    print(json.dumps(s, ensure_ascii=False))
    return 0 if s.get("mcp") and s.get("in_game") else 1


if __name__ == "__main__":
    raise SystemExit(main())
