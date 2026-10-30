#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""launcher/buddy_app.py —— 脱离 Minecraft 的 Buddy 控制台（打包成 exe）

operator 2026-10-26/27：
  「最后要做成一个脱离 mc 的 exe，可以直接选择 buddy 的数量、调整参与度等可调参数，
    以及是 ai 的交互界面（mc 中 t 也可以交互）」
  「模型采用 ds 的，apikey 一会给你，agent 层一定要做好，还有界面要优化一下」
  「还要考虑一下多 buddy 的协作模式」

所以这个窗口管四件事：

  1. **开几个陪玩**：每个陪玩 = 一个真 MC 客户端 + 一个指挥层进程
     （独立 MCP 端口 / 独立工作目录 / 独立 altoclef 设置），数量 1~4。
  2. **调参**：参与度（安静/正常/黏人）、说不说话、声音、一键传送、**组队协作**。
  3. **接 AI**：DeepSeek 的 api_key / base_url / 模型（写 `config/ai.json`），一键测连接。
  4. **交互**：输入框就是"跟陪玩说话"，和游戏里按 T 打字走同一条路
     （`runs/requests.jsonl` → 指挥层请求总线）；可以指定**对谁说**（全部 / 某一个）。

多陪玩为什么要"指定对谁说"：请求文件是**共享**的，N 个指挥层都会读到同一行。
没有寻址的话，三个陪玩会同时冲向同一棵树。所以每条请求带 `to`：
  · 缺省 = 只由"说话权持有者"（编号最小、还活着）执行，其余人静默旁听；
  · `to:"all"` = 群体动作（停手、集合）。

为什么用 tkinter：标准库、无第三方依赖、PyInstaller 打出来只有几 MB，且不挑机器。

用法：
  python -X utf8 launcher/buddy_app.py                 # 开窗口
  python -X utf8 launcher/buddy_app.py --selftest      # 自检（exe 也能验）
  （打包）pyinstaller --onefile --noconsole --name Buddy launcher/buddy_app.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 打包成 exe 之后 `__file__` 指向 PyInstaller 的临时解包目录，不能用来找项目。
if getattr(sys, "frozen", False):
    _here = Path(sys.executable).resolve().parent
    if (os.environ.get("BUDDY_ROOT") or "").strip():
        ROOT = Path(os.environ["BUDDY_ROOT"]).resolve()
    elif (_here / "companion").is_dir():
        ROOT = _here
    else:
        # No BUDDY_ROOT and nothing that looks like the project next to the exe: use the directory
        # we were started from. (This used to be an absolute path on the machine it was built on,
        # which meant a copy anywhere else pointed at somebody else's disk.)
        ROOT = Path.cwd()

CONF = ROOT / "launcher" / "config.json"
REQUESTS = ROOT / "runs" / "requests.jsonl"
LOGDIR = ROOT / "runs" / "launcher"
TEAMBOARD = ROOT / "runs" / "team" / "board.json"

sys.path.insert(0, str(ROOT))
from companion import nlu          # noqa: E402

DEFAULTS = {
    "buddies": 1,
    "engagement": "normal",
    "chat": True,
    "voice": False,
    "teleport": True,
    "team": True,
    "player": "Xiaorun",
    "server": "127.0.0.1:25565",
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "roles": ["playmate", "guard", "gatherer", "builder"],
}


def load_conf() -> dict:
    try:
        d = json.loads(CONF.read_text(encoding="utf-8"))
        return {**DEFAULTS, **d}
    except Exception:
        return dict(DEFAULTS)


def save_conf(d: dict) -> None:
    CONF.parent.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def save_ai(api_key: str = "", base_url: str = "", model: str = "") -> Path:
    """把 DS 配置写进 `config/ai.json`（agent 读的那份）。"""
    try:
        from companion.agent import save_config
        return save_config(api_key=api_key, base_url=base_url, model=model)
    except Exception:
        p = ROOT / "config" / "ai.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"api_key": api_key, "base_url": base_url,
                                 "model": model}, ensure_ascii=False, indent=1),
                     encoding="utf-8")
        return p


# ── 多开：每个陪玩一套独立配置 ──────────────────────────────────────────────
def instance(i: int) -> dict:
    return {
        "i": i,
        "port": 25350 + i,                       # 25351 起（25350 是真人客户端）
        "name": "Buddy" if i == 1 else f"Buddy{i}",
        "workdir": Path(r"%BUDDY_CLIENT%" if i == 1 else rf"%BUDDY_CLIENT%"),
    }


def settings_for(i: int) -> Path:
    return instance(i)["workdir"] / "altoclef" / "altoclef_settings.json"


def ensure_settings(i: int) -> Path:
    """给第 i 个陪玩准备 altoclef 设置（端口必须互不相同，否则第二个 MCP 绑不上）。

    ⚠️ 2026-10-27 血证：这里以前**每次都重新生成 mcpAuthToken** —— 于是面板点一次"启动"，
    磁盘上的 token 就变成新的，而**正在跑的客户端用的还是旧 token** ⇒ 指挥层一发请求就
    `HTTP 401 Unauthorized`（Buddy2 就是这样变成"活着但没人指挥得动"的）。
    token 一旦存在就必须保持不变，只有缺失时才生成。
    """
    inst = instance(i)
    cfg = settings_for(i)
    src = settings_for(1)
    inst["workdir"].mkdir(parents=True, exist_ok=True)
    (inst["workdir"] / "altoclef").mkdir(parents=True, exist_ok=True)
    if i == 1:
        return cfg
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if isinstance(data, dict):
            changed = False
            if data.get("mcpPort") != inst["port"]:
                data["mcpPort"] = inst["port"]
                changed = True
            if not str(data.get("mcpAuthToken") or "").strip():
                data["mcpAuthToken"] = f"buddy{i}-stable"
                changed = True
            if changed:
                cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            return cfg
    if src.exists():
        data = json.loads(src.read_text(encoding="utf-8"))
        data["mcpPort"] = inst["port"]
        data["mcpAuthToken"] = f"buddy{i}-stable"
        cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def _python_exe() -> list[str]:
    """真正的 Python 解释器（打包后 sys.executable 是 Buddy.exe 自己，不能拿来当解释器）。"""
    env = (os.environ.get("BUDDY_PYTHON") or "").strip()
    if env and Path(env).exists():
        return [env]
    if not getattr(sys, "frozen", False):
        return [sys.executable]
    for name in ("python", "python3", "py"):
        cand = shutil.which(name)
        if cand:
            return [cand]
    for rel in (".venv/Scripts/python.exe", "venv/Scripts/python.exe"):
        p = ROOT / rel
        if p.exists():
            return [str(p)]
    return [sys.executable]


def port_open(port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False


def launch_client(i: int) -> subprocess.Popen | None:
    """起第 i 个 MC 客户端（--no-connect：必须停在主菜单 mod 才完整初始化）。"""
    inst = instance(i)
    py = _python_exe()
    if i == 1:
        cmd = py + ["-X", "utf8", str(ROOT / "tools" / "buddy_body.py"), "ensure"]
        return subprocess.Popen(cmd, cwd=str(ROOT),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cmd = py + ["-X", "utf8", str(ROOT / "tools" / "_mc121_launch.py"),
                "--name", inst["name"], "--workdir", str(inst["workdir"]),
                "--mcp-port", str(inst["port"]), "--no-connect",
                "--width", "854", "--height", "480"]
    return subprocess.Popen(cmd, cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launch_brain(i: int, conf: dict) -> subprocess.Popen:
    """起第 i 个指挥层进程。"""
    LOGDIR.mkdir(parents=True, exist_ok=True)
    log = LOGDIR / f"buddy{i}.log"
    inst = instance(i)
    roles = conf.get("roles") or DEFAULTS["roles"]
    role = roles[(i - 1) % len(roles)] if isinstance(roles, list) else "playmate"
    args = _python_exe() + ["-X", "utf8", "-u", "-m", "companion.commander",
                            "--port", str(inst["port"]),
                            "--settings", str(settings_for(i)),
                            "--player", conf.get("player") or "Xiaorun",
                            "--bot-name", inst["name"],
                            "--seconds", "86400", "--hz", "3",
                            "--engagement", conf.get("engagement", "normal"),
                            "--role", role]
    if conf.get("team", True):
        # 组队：一条黑板。指挥层靠它分工、编队、决定谁说话。
        args += ["--team", str(TEAMBOARD)]
    if not conf.get("chat", True):
        args.append("--no-chat")
    if not conf.get("teleport", True):
        args.append("--no-teleport")
    if conf.get("voice"):
        args.append("--voice")
    fh = open(log, "ab")
    return subprocess.Popen(args, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT)


def send_request(text: str, who: str = "Xiaorun", to: str = "") -> dict:
    """把一句话送进请求总线（和游戏里打字同一条路）。

    ⚠️ 2026-10-27 修：以前这里把"没听懂的句子"当成 `say` 发下去 ——
    陪玩会把你说的话**原样复读**一遍（看起来像鹦鹉）。现在保留原话进 `raw`，
    但 kind 一律走 `free`（交给 agent：有 DS 就走模型，没有就规则回落），
    只有显式"说：xxx"才当 `say`。
    """
    text = (text or "").strip()
    parsed = nlu.parse(text, me="buddy", player=who)
    kind = str(parsed.pop("kind", "") or "")
    conf = float(parsed.pop("conf", 0.0) or 0.0)
    parsed.pop("text", None)
    if kind in ("chat", "") or conf < 0.35:
        kind, parsed = "free", {"text": text}
    parsed.pop("raw", None)
    parsed.setdefault("from", "panel")
    if to:
        parsed["to"] = to
    REQUESTS.parent.mkdir(parents=True, exist_ok=True)
    with REQUESTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": kind, **parsed, "raw": text,
                             "t": time.time()}, ensure_ascii=False) + "\n")
    return {"kind": kind, **parsed}


# ── 队伍黑板（面板的状态表就读它） ─────────────────────────────────────────
def team_snapshot() -> dict:
    try:
        d = json.loads(TEAMBOARD.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def last_plan() -> str:
    """① 面板上显示"它打算怎么做"（goal_graph 写的 runs/state/last_plan.json）。

    operator 2026-10-29：分解图不只在 trace 里，面板要能一眼看到 —— 否则出问题时
    你还是只能靠猜"它到底想干什么"。
    """
    p = ROOT / "runs" / "state" / "last_plan.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ""
    try:
        age = time.time() - p.stat().st_mtime
    except Exception:
        age = 999
    if age > 300:                        # 5 分钟前的计划不再展示（免得误导）
        return ""
    item = d.get("item") or d.get("asked") or ""
    sub = f"（原 {d.get('asked')} 换成它）" if d.get("substituted") else ""
    tree = d.get("tree") or []
    head = " → ".join(str(x)[:36] for x in tree[:2]) if tree else (d.get("reason") or "")
    return f"打算：{item}{sub} {head}"[:120]


def last_event(i: int) -> str:
    """该实例日志的最后一条非 tick 事件（拼成一行给面板看）。"""
    p = LOGDIR / f"buddy{i}.log"
    try:
        with p.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 6000))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return ""
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        ev = e.get("ev")
        if ev in ("tick", "job_tick", "no_body"):
            continue
        if ev == "say":
            return f"说：{e.get('text')}"
        if ev == "job_start":
            return f"开工：{e.get('job')}"
        if ev == "job_done":
            r = e.get("result") or {}
            return f"完工：{e.get('job')} {'✓' if r.get('ok') else '✗'} {str(r.get('why') or '')[:24]}"
        if ev == "heard":
            return f"听到：{e.get('said')}"
        if ev == "fast_done":
            return f"快答：{e.get('kind')}"
        if ev in ("body_stall", "body_ensure", "body_back", "claim_denied", "veto"):
            return f"{ev} {str(e)[:40]}"
    return ""


# ── 窗口 ────────────────────────────────────────────────────────────────────
def run_gui() -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    conf = load_conf()
    procs: dict[str, subprocess.Popen] = {}
    stop_flag = {"v": False}

    root = tk.Tk()
    root.title("Buddy 陪玩控制台")
    root.geometry("1040x720")
    root.minsize(900, 620)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("Big.TButton", padding=(10, 4))
    style.configure("Treeview", rowheight=22)

    # ── 顶部：数量 / 参与度 / 开关 ─────────────────────────────────────────
    top = ttk.LabelFrame(root, text="陪玩设置", padding=10)
    top.pack(fill="x", padx=10, pady=(10, 6))

    ttk.Label(top, text="数量").grid(row=0, column=0, sticky="w")
    n_var = tk.IntVar(value=int(conf.get("buddies", 1)))
    ttk.Spinbox(top, from_=1, to=4, width=4, textvariable=n_var).grid(row=0, column=1, sticky="w")

    ttk.Label(top, text="参与度").grid(row=0, column=2, padx=(16, 4), sticky="w")
    ENG = {"quiet": "安静", "normal": "正常", "clingy": "黏人"}
    e_var = tk.StringVar(value=ENG.get(conf.get("engagement", "normal"), "正常"))
    ttk.Combobox(top, textvariable=e_var, width=6, state="readonly",
                 values=["安静", "正常", "黏人"]).grid(row=0, column=3, sticky="w")

    chat_var = tk.BooleanVar(value=bool(conf.get("chat", True)))
    voice_var = tk.BooleanVar(value=bool(conf.get("voice", False)))
    tp_var = tk.BooleanVar(value=bool(conf.get("teleport", True)))
    team_var = tk.BooleanVar(value=bool(conf.get("team", True)))
    ttk.Checkbutton(top, text="会说话", variable=chat_var).grid(row=0, column=4, padx=(16, 2))
    ttk.Checkbutton(top, text="声音", variable=voice_var).grid(row=0, column=5, padx=2)
    ttk.Checkbutton(top, text="一键传送", variable=tp_var).grid(row=0, column=6, padx=2)
    ttk.Checkbutton(top, text="组队协作", variable=team_var).grid(row=0, column=7, padx=(8, 2))

    ttk.Label(top, text="玩家名").grid(row=1, column=0, pady=(8, 0), sticky="w")
    p_var = tk.StringVar(value=conf.get("player", "Xiaorun"))
    ttk.Entry(top, textvariable=p_var, width=14).grid(row=1, column=1, pady=(8, 0), sticky="w")

    ttk.Label(top, text="服务器").grid(row=1, column=2, padx=(16, 4), pady=(8, 0), sticky="w")
    s_var = tk.StringVar(value=conf.get("server", "127.0.0.1:25565"))
    ttk.Entry(top, textvariable=s_var, width=18).grid(row=1, column=3, pady=(8, 0), sticky="w")

    # ── AI（DeepSeek） ────────────────────────────────────────────────────
    ai = ttk.LabelFrame(root, text="AI（DeepSeek · 一句话 → 一串工具调用）", padding=10)
    ai.pack(fill="x", padx=10, pady=(0, 6))

    ttk.Label(ai, text="API Key").grid(row=0, column=0, sticky="w")
    k_var = tk.StringVar(value=conf.get("api_key", ""))
    k_entry = ttk.Entry(ai, textvariable=k_var, width=44, show="•")
    k_entry.grid(row=0, column=1, columnspan=2, sticky="w", padx=6)
    show_var = tk.BooleanVar(value=False)

    def toggle_key():
        k_entry.configure(show="" if show_var.get() else "•")

    ttk.Checkbutton(ai, text="显示", variable=show_var, command=toggle_key).grid(row=0, column=3)

    ttk.Label(ai, text="base_url").grid(row=1, column=0, pady=(6, 0), sticky="w")
    b_var = tk.StringVar(value=conf.get("base_url", "https://api.deepseek.com"))
    ttk.Entry(ai, textvariable=b_var, width=44).grid(row=1, column=1, columnspan=2,
                                                     pady=(6, 0), sticky="w", padx=6)
    ttk.Label(ai, text="模型").grid(row=1, column=3, pady=(6, 0), sticky="e")
    m_var = tk.StringVar(value=conf.get("model", "deepseek-flash"))
    ttk.Combobox(ai, textvariable=m_var, width=18,
                 values=["deepseek-flash", "deepseek-v4-pro", "deepseek-chat"]).grid(
        row=1, column=4, pady=(6, 0), sticky="w")

    ai_state = tk.StringVar(value="未配置（陪玩仍可干活，自然语言走规则回落）")
    ttk.Label(ai, textvariable=ai_state, foreground="#666").grid(
        row=2, column=0, columnspan=5, pady=(6, 0), sticky="w")

    # ── 状态表 ────────────────────────────────────────────────────────────
    mid = ttk.LabelFrame(root, text="陪玩状态（每 2 秒刷新 · 数据来自队伍黑板与各自日志）",
                         padding=6)
    mid.pack(fill="both", expand=True, padx=10, pady=(0, 6))
    cols = ("name", "port", "online", "role", "busy", "pos", "speak", "last")
    tv = ttk.Treeview(mid, columns=cols, show="headings", height=6)
    heads = {"name": "陪玩", "port": "端口", "online": "在线", "role": "角色",
             "busy": "正在做", "pos": "位置", "speak": "说话权", "last": "最近"}
    widths = {"name": 80, "port": 60, "online": 50, "role": 80, "busy": 140,
              "pos": 150, "speak": 60, "last": 240}
    for c in cols:
        tv.heading(c, text=heads[c])
        tv.column(c, width=widths[c], anchor="w")
    tv.pack(fill="both", expand=True)

    # ── 日志 ──────────────────────────────────────────────────────────────
    bodyf = ttk.Frame(root, padding=(10, 0))
    bodyf.pack(fill="both", expand=True)
    log = tk.Text(bodyf, height=10, wrap="word", state="disabled", font=("Consolas", 9))
    sb = ttk.Scrollbar(bodyf, command=log.yview)
    log.configure(yscrollcommand=sb.set)
    log.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    def append(line: str) -> None:
        log.configure(state="normal")
        log.insert("end", f"{time.strftime('%H:%M:%S')}  {line}\n")
        log.see("end")
        log.configure(state="disabled")

    # ── 逻辑 ──────────────────────────────────────────────────────────────
    def cur_conf() -> dict:
        return {
            "buddies": int(n_var.get()),
            "engagement": {"安静": "quiet", "正常": "normal", "黏人": "clingy"}[e_var.get()],
            "chat": bool(chat_var.get()), "voice": bool(voice_var.get()),
            "teleport": bool(tp_var.get()), "team": bool(team_var.get()),
            "player": p_var.get().strip() or "Xiaorun",
            "server": s_var.get().strip() or "127.0.0.1:25565",
            "api_key": k_var.get().strip(), "base_url": b_var.get().strip(),
            "model": m_var.get().strip(),
            "roles": (conf.get("roles") or DEFAULTS["roles"]),
        }

    status = tk.StringVar(value="未启动")

    def start() -> None:
        c = cur_conf()
        save_conf(c)
        if c["api_key"] or c["base_url"] or c["model"]:
            p = save_ai(c["api_key"], c["base_url"], c["model"])
            ai_state.set(f"已保存到 {p}" + ("（有密钥，agent 可直连模型）" if c["api_key"]
                                            else "（还没填密钥，自然的问法会走规则回落）"))
        for i in range(1, c["buddies"] + 1):
            ensure_settings(i)
            key = f"client{i}"
            if key not in procs:
                procs[key] = launch_client(i)
                append(f"拉起客户端 #{i}（{instance(i)['name']} · 端口 {instance(i)['port']}）")
            key = f"brain{i}"
            if key not in procs:
                procs[key] = launch_brain(i, c)
                append(f"拉起指挥层 #{i}（{instance(i)['port']} → {LOGDIR / f'buddy{i}.log'}）")
        status.set(f"运行中：{c['buddies']} 个陪玩 · 参与度={e_var.get()}"
                   f" · 组队={'开' if c['team'] else '关'}")

    def stop() -> None:
        stop_flag["v"] = True
        for k, p in list(procs.items()):
            try:
                p.terminate()
                append(f"停止 {k}")
            except Exception:
                pass
            procs.pop(k, None)
        status.set("已停止（MC 客户端进程仍在，可用体检/重启管理）")

    def doctor() -> None:
        def _run():
            r = subprocess.run(_python_exe() + ["-X", "utf8",
                                               str(ROOT / "tools" / "watch_body.py"), "doctor"],
                               cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            for line in (r.stdout or "").splitlines():
                append(line)
        threading.Thread(target=_run, daemon=True).start()

    def tile() -> None:
        """把窗口平铺排开（叠在一起会互相遮挡，遮挡的那个可能不渲染 ⇒ 准星冻结）。"""
        def _run():
            r = subprocess.run(_python_exe() + ["-X", "utf8",
                                               str(ROOT / "tools" / "_tile_windows.py")],
                               cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            for line in (r.stdout or "").splitlines():
                append(line)
        threading.Thread(target=_run, daemon=True).start()

    def ping_ai() -> None:
        c = cur_conf()
        save_ai(c["api_key"], c["base_url"], c["model"])
        save_conf(c)
        ai_state.set("正在测连接…")

        def _run():
            r = subprocess.run(_python_exe() + ["-X", "utf8", "-m", "companion.agent",
                                               "--ping"],
                               cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            out = (r.stdout or "").strip()
            ok = '"ok": true' in out.replace("\n", " ")
            ai_state.set(("✓ " if ok else "✗ ") + out.replace("\n", " ")[:150])
        threading.Thread(target=_run, daemon=True).start()

    # 刷新状态表
    def refresh() -> None:
        try:
            snap = team_snapshot()
            members = snap.get("members") or {}
            speaker = snap.get("speaker") or ""
            for row in tv.get_children():
                tv.delete(row)
            n = int(n_var.get())
            for i in range(1, n + 1):
                inst = instance(i)
                m = members.get(inst["name"]) or {}
                pos = m.get("pos")
                pos_s = f"{pos[0]:.0f},{pos[1]:.0f},{pos[2]:.0f}" if pos else "-"
                online = bool(m) and (time.time() - float(m.get("at") or 0) < 25)
                tv.insert("", "end", values=(
                    inst["name"], inst["port"],
                    ("✓" if port_open(inst["port"]) else "✗") + ("·在线" if online else ""),
                    m.get("role") or "-",
                    (m.get("busy") or "空闲"),
                    pos_s,
                    "说话" if (speaker in ("", inst["name"])) else "",
                    (last_plan() + " ｜ " + last_event(i)) if i == 1 else last_event(i),
                ))
            if not members and n > 0:
                tv.insert("", "end", values=("（黑板还是空的）", "", "", "", "", "", "", ""))
        except Exception:
            pass
        root.after(2000, refresh)

    # ── 按钮条 ────────────────────────────────────────────────────────────
    bar = ttk.Frame(root, padding=(10, 0, 10, 6))
    bar.pack(fill="x")
    ttk.Label(bar, textvariable=status, foreground="#444").pack(side="left")
    ttk.Button(bar, text="启动", style="Big.TButton", command=start).pack(side="right", padx=3)
    ttk.Button(bar, text="停止指挥层", command=stop).pack(side="right", padx=3)
    ttk.Button(bar, text="平铺窗口", command=tile).pack(side="right", padx=3)
    ttk.Button(bar, text="体检", command=doctor).pack(side="right", padx=3)
    ttk.Button(bar, text="测 AI 连接", command=ping_ai).pack(side="right", padx=3)

    # ── 交互（和游戏里按 T 同一条路） ─────────────────────────────────────
    sendbar = ttk.Frame(root, padding=(10, 0, 10, 10))
    sendbar.pack(fill="x")
    ttk.Label(sendbar, text="对").pack(side="left")
    to_var = tk.StringVar(value="自动")
    to_box = ttk.Combobox(sendbar, textvariable=to_var, width=8, state="readonly")
    to_box.pack(side="left", padx=(2, 8))
    QUICK = [("过来", "过来一下"), ("跟着我", "跟着我"), ("清怪", "把附近的怪清一下"),
             ("砍三根木头", "帮我砍三根木头"), ("自己补给", "自己找点吃的"),
             ("停手", "别干了")]
    say_var = tk.StringVar()
    entry = ttk.Entry(sendbar, textvariable=say_var)
    entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

    def send(*_):
        text = say_var.get().strip()
        if not text:
            return
        to = "" if to_var.get() in ("自动", "") else (
            "all" if to_var.get() == "全部" else to_var.get())
        r = send_request(text, who=p_var.get().strip() or "Xiaorun", to=to)
        append(f"→ {text}   [{r.get('kind')}]  → {to or '说话者'}")
        say_var.set("")

    entry.bind("<Return>", send)

    def quick(text: str):
        def _go():
            to = "all" if text == "别干了" else ""
            r = send_request(text, who=p_var.get().strip() or "Xiaorun", to=to)
            append(f"→ {text}   [{r.get('kind')}]")
        return _go

    qbar = ttk.Frame(root, padding=(10, 0, 10, 6))
    qbar.pack(fill="x")
    for label, text in QUICK:
        ttk.Button(qbar, text=label, command=quick(text)).pack(side="left", padx=2)
    ttk.Button(sendbar, text="发送", command=send).pack(side="left")

    def update_to_values(*_):
        vals = ["自动", "全部"] + [instance(i)["name"] for i in range(1, int(n_var.get()) + 1)]
        to_box.configure(values=vals)

    n_var.trace_add("write", update_to_values)
    update_to_values()

    append("提示：游戏里按 T 打字和这里效果一样（都进请求总线）。")
    append("多陪玩时用「对」选择谁来执行；「自动」= 说话权持有者一个人做，其余人旁观。")
    append(f"工作目录 {ROOT}")
    append("接 AI：填 DeepSeek 的 API Key 再点「测 AI 连接」即可（模型 deepseek-flash）。")

    root.after(300, refresh)
    root.mainloop()
    return 0


if __name__ == "__main__":
    if "--set-key" in sys.argv:
        # 命令行塞密钥（不想开窗口时用）：python -X utf8 launcher/buddy_app.py --set-key sk-xxx
        i = sys.argv.index("--set-key")
        key = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        c = load_conf()
        c["api_key"] = key
        save_conf(c)
        p = save_ai(key, c.get("base_url", ""), c.get("model", ""))
        print(f"已写入 {p}（长度 {len(key)}）")
        sys.exit(0)
    if "--selftest" in sys.argv:
        lines = []
        try:
            c = load_conf()
            lines.append("config: " + json.dumps({**c, "api_key": "(略)"}, ensure_ascii=False))
            lines.append(f"ROOT={ROOT}  frozen={getattr(sys, 'frozen', False)}")
            for i in (1, 2, 3):
                inst = instance(i)
                lines.append(f"instance{i}: port={inst['port']} name={inst['name']} "
                             f"settings={ensure_settings(i)}")
            lines.append("send_request: " + json.dumps(send_request("帮我砍三根木头"),
                                                       ensure_ascii=False))
            lines.append("send_free: " + json.dumps(send_request("你觉得这片林子怎么样"),
                                                    ensure_ascii=False))
            lines.append("team_snapshot: " + json.dumps(team_snapshot(), ensure_ascii=False)[:200])
            lines.append("python_exe: " + json.dumps(_python_exe(), ensure_ascii=False))
            lines.append("OK")
        except Exception:
            import traceback
            lines.append("FAIL " + traceback.format_exc()[-800:])
        out = ROOT / "launcher" / "selftest.txt"
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass
        print("\n".join(lines))
        sys.exit(0 if lines and lines[-1] == "OK" else 1)
    raise SystemExit(run_gui())
