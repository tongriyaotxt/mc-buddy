#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""launcher/core.py —— "类 PCL"的内核（UI 无关）

operator 2026-10-29：「最后要做成一个类似 PCL 的软件」。

PCL 真正好用的地方不是界面，是这四件事（界面只是它们的壳）：
  1. **实例/版本隔离**：每个版本一套 mods/config/存档，互不污染；
  2. **模组管理**：把 jar 放到"对的目录"（我们踩过：`versions/<ver>/mods` 和 `.minecraft/mods`
     **两个地方都要放**，否则 mod 不生效）；
  3. **一键启动 + 参数**（Java/内存/账号/自动连服）；
  4. **故障可诊断**（日志在哪、崩了留什么、怎么回滚）。

所以这个文件只做上面四件事，**不 import 任何 UI**：
    find_game_dirs() / list_instances() / install_mod() / ensure_settings()
    launch_cmd() / server_status() / diagnostics() / selftest()

UI（Tk 或 Qt）以后只调这里。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

def _root() -> Path:
    """项目根。

    · 源码运行：`launcher/core.py` 的上两级 = 项目根；
    · **打包运行**（PyInstaller onefile）：`__file__` 在临时解包目录里，拿它算根会算到
      `%TEMP%\\_MEIxxxx` ⇒ 换成 **exe 所在目录**。`tools/` `config/` 这些会被
      `unpack_assets()` 在首次运行时解到那里（见 `launcher/ui/main.py` 的 `--run-py`）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _root()
JARS = ROOT / "_refs" / "unionclef-0.95.63" / "versions" / "1.21.11" / "build" / "libs"


def _latest_published_jar() -> Path:
    """发布指针 `_releases/latest.json` 指的那一个 jar。

    2026-10-30：这里原来写死 `...buddy14.jar`。每次发版都要手改这一行，忘一次就是
    "面板起的是上一版代码"。改成读指针，指针不在（比如干净 clone 没跑过发布）才回落到
    本地构建产物。`_releases/` 不入库，所以回落分支是必须的，不是防御性冗余。
    """
    ptr = ROOT / "_releases" / "latest.json"
    try:
        info = json.loads(ptr.read_text(encoding="utf-8"))
        name = info.get("file") or ""
        for cand in (ROOT / "_releases" / name, JARS / name):
            if name and cand.exists():
                return cand
    except Exception:
        pass
    return JARS / "unionclef-1.21.11-0.95.63-local.jar"


DEFAULT_JAR = _latest_published_jar()
SETTINGS_REL = Path("altoclef") / "altoclef_settings.json"


def python_exe() -> str:
    """找一个真 Python（打包成 exe 后 `sys.executable` 是 exe 本身，不能再拿它跑 .py）。"""
    exe = sys.executable or ""
    name = Path(exe).name.lower()
    if name.startswith(("python", "pythonw")):
        return exe
    for cand in ("python", "python3", "py"):
        w = shutil.which(cand)
        if w:
            return w
    return exe


def py_prefix() -> list[str]:
    """跑一个 `.py` 的命令前缀。

    · 源码：`[python, -X, utf8]`；
    · 打包：`[Buddy.exe, --run-py]` —— **不需要机器上装 Python**。
      `--run-py` 由 `launcher/ui/main.py` 接住，用打包内的解释器 `runpy` 跑那个脚本。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-py"]
    return [python_exe(), "-X", "utf8"]


def missing(path: Path | str) -> str:
    """给 UI 用：文件不存在时返回一句能直接照着修的话。"""
    p = Path(path)
    return "" if p.exists() else f"未找到文件：{p}\n（请确认没有被移动/删除；jar 在 _refs 下，启动器在 tools 下）"


# 打包后需要"落在 exe 旁边"的目录（子进程 `--run-py` 是真的按 ROOT/tools/xxx.py 去读盘的）
UNPACK_DIRS = ("config", "tools", "companion", "policy", "launcher")
# 其中**代码**：只要 exe 换了版本就整份刷新（否则升级后跑的还是上一版的工具）；
# `config/` 是**用户的东西**（支持开关、语音选择都在里面）⇒ 永远只补缺、不覆盖。
REFRESH_DIRS = ("tools", "companion", "policy", "launcher")
UNPACK_STAMP = ".buddy_unpack.json"


def _bundle_version() -> str:
    """exe 的构建指纹（用 mtime 就够：同一次构建 = 同一个值，重建必变）。"""
    try:
        return str(int(Path(sys.executable).stat().st_mtime))
    except Exception:
        return "0"


def unpack_assets() -> list[str]:
    """打包运行：把 exe 内带的纯 Python/JSON 资源解到 exe 旁边。

    · **首次**：全部补上；
    · **换了 exe 版本**：`tools/ companion/ policy/ launcher/` 整份刷新（升级要真的生效，
      否则 `Buddy.exe --run-py tools/xxx.py` 跑的是旧工具）；
    · `config/` **永远只补缺** —— 那里面是用户自己的开关和选择，升级不许冲掉。
    """
    if not getattr(sys, "frozen", False):
        return []
    src_root = Path(getattr(sys, "_MEIPASS", "") or "")
    if not src_root.exists():
        return []
    ver = _bundle_version()
    stamp = ROOT / UNPACK_STAMP
    old_ver = ""
    try:
        old_ver = str(json.loads(stamp.read_text(encoding="utf-8")).get("version") or "")
    except Exception:
        old_ver = ""
    refresh = old_ver != ver
    made: list[str] = []
    for name in UNPACK_DIRS:
        src = src_root / name
        if not src.is_dir():
            continue
        dst = ROOT / name
        code_dir = name in REFRESH_DIRS
        for f in src.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(src)
            target = dst / rel
            if target.exists():
                # ⚠️ 2026-10-30 血证：如果只按"版本戳变了才覆盖"，`Copy-Item` 保留 mtime
                # 会让两个 exe 的指纹一样 ⇒ 升级后 exe 旁边跑的仍是**上一版的工具**
                # （实测：新 exe 打出来，`--run-py` 跑的还是旧的 launcher/core.py，
                #  一键准备 ⑥ 还在打印 --desktop）。所以逐个文件按 **大小 + mtime** 判：
                # 小了/旧了就覆盖。`config/` 不参与（用户的东西）。
                if not code_dir:
                    continue
                try:
                    st_t, st_s = target.stat(), f.stat()
                    if st_t.st_size == st_s.st_size and st_t.st_mtime >= st_s.st_mtime:
                        continue
                except Exception:
                    pass
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                made.append(f"{name}/{rel.as_posix()}")
            except Exception:
                pass
    if made or refresh:
        try:
            stamp.write_text(json.dumps({"version": ver, "files": len(made),
                                         "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
    return made


# ── ⓿ 这台机器的"根目录"（不许写成绝对字面量）────────────────────────────────
# 这三个目录只在本机有意义：游戏目录、我们的客户端实例、语音模型。它们以前是写死的字面量，
# 后果有两个——别人拿去跑不起来，而且这个文件一旦公开就等于公开了某台机器的磁盘结构。
# 现在的顺序：环境变量 → `launcher/paths.json`（git 忽略，本机自己填）→ 家目录下的通用默认。
def _load_paths() -> dict:
    try:
        return json.loads((Path(__file__).resolve().parent / "paths.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


_PATHS = _load_paths()


def _root(env_name: str, key: str, default: Path) -> Path:
    for v in (os.environ.get(env_name), _PATHS.get(key)):
        if isinstance(v, str) and v.strip():
            return Path(v.strip())
    return default


#: 游戏（.minecraft）所在根：PCL / 官方 / 我们自己的实例都挂在它下面
MC_HOME = _root("BUDDY_MC_HOME", "mc_home", Path.home() / ".minecraft")
#: 我们的无头客户端实例（buddy1..N，每个一套 mods/config）
BUDDY_CLIENTS = _root("BUDDY_CLIENTS", "clients", Path.home() / ".mc-buddy" / "clients")
#: 语音/识别模型
MODELS_DIR = _root("BUDDY_MODELS", "models", Path.home() / ".mc-buddy" / "models")


# ── ① 找游戏目录（PCL / 官方 / 我们自己的实例）────────────────────────────────
def find_game_dirs() -> list[dict]:
    """扫出可能的 .minecraft 目录，并标出它们像谁。"""
    cands: list[Path] = []
    env = os.environ.get("BUDDY_MC_DIRS", "")
    for x in env.split(os.pathsep):
        if x.strip():
            cands.append(Path(x.strip()))
    for base in (MC_HOME, Path.home() / "AppData" / "Roaming", BUDDY_CLIENTS):
        if not base.exists():
            continue
        for p in base.rglob(".minecraft"):
            if p.is_dir():
                cands.append(p)
        # PCL 的版本隔离目录：<.minecraft>/versions/<ver>/
        for p in base.rglob("versions"):
            if p.is_dir() and p.parent.name == ".minecraft":
                cands.append(p.parent)
    out, seen = [], set()
    for p in cands:
        rp = p.resolve()
        if str(rp).lower() in seen or not (rp / "versions").exists():
            continue
        seen.add(str(rp).lower())
        vers = [v.name for v in (rp / "versions").iterdir() if v.is_dir()]
        kind = "PCL" if "PCL" in str(rp) else ("官方" if "Roaming" in str(rp) else "自定义")
        out.append({"dir": str(rp), "kind": kind, "versions": sorted(vers)})
    return out


# ── ② 实例（版本）清单：mods 数、是否已装陪玩 jar ─────────────────────────────
def _mods_dirs(game_dir: Path, version: str) -> list[Path]:
    """两个都要放：版本隔离目录 + 全局 mods（2026-10-29 血证）。"""
    return [game_dir / "versions" / version / "mods", game_dir / "mods"]


def list_instances(game_dir: Path) -> list[dict]:
    gd = Path(game_dir)
    out = []
    vroot = gd / "versions"
    if not vroot.exists():
        return out
    for v in sorted(vroot.iterdir()):
        if not v.is_dir():
            continue
        dirs = _mods_dirs(gd, v.name)
        jars = []
        for d in dirs:
            if d.exists():
                jars += [p.name for p in d.glob("*.jar")]
        buddy = [j for j in jars if "unionclef" in j.lower() or "buddy" in j.lower()]
        out.append({"version": v.name, "mods_dirs": [str(d) for d in dirs],
                    "mods": len(jars), "buddy_jar": buddy[-1] if buddy else "",
                    "has_fabric": any("fabric" in j.lower() for j in jars)})
    return out


# ── ③ 装/更新陪玩 jar（带备份 + 两个目录都放）────────────────────────────────
def install_mod(game_dir: Path, version: str, jar: Path | str = DEFAULT_JAR) -> dict:
    jarp = Path(jar)
    if not jarp.exists():
        return {"ok": False, "why": f"jar 不存在: {jarp}"}
    gd = Path(game_dir)
    placed, backed = [], []
    for d in _mods_dirs(gd, version):
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*unionclef*.jar"):
            if old.name == jarp.name:
                continue
            bak = old.with_suffix(old.suffix + ".bak")
            shutil.copy2(old, bak)
            backed.append(str(bak))
            old.unlink(missing_ok=True)
        dst = d / jarp.name
        shutil.copy2(jarp, dst)
        placed.append(str(dst))
    return {"ok": True, "placed": placed, "backed_up": backed, "jar": jarp.name}


# ── ④ 写 altoclef 设置（存在就不改；token 缺失才补）───────────────────────────
def ensure_settings(game_dir: Path, *, mcp_port: int = 25351,
                    gateway_port: int = 25341) -> dict:
    p = Path(game_dir) / SETTINGS_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    created = False
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            d = {}
    else:
        d, created = {}, True
    before = dict(d)
    d.setdefault("mcpEnabled", True)
    d.setdefault("mcpPort", int(mcp_port))
    d.setdefault("pythonGatewayPort", int(gateway_port))
    if not d.get("mcpAuthToken"):
        d["mcpAuthToken"] = str(uuid.uuid4())
    # ── companion 档：**不做家务**（2026-10-30）───────────────────────────────
    # `FoodChain` 是**竞价链**：`cachedFoodScore < foodUnitsToCollect` 时它报 **55 分**，
    # 而用户任务只有 **50**、强制任务 51 ⇒ 只要"背包食物折算 < 12"这条恒成立，
    # 它每 tick 抢走身体，任何手动动作（@mineblock / @idle / 我们的作业）都排不上队
    # （实测链永远是 `Collect 12.0 units of food / Cooking…`）。
    # 12 不是上游默认值 —— `Settings.java:162` 的代码默认是 **0**，12 是早期模板写进来的。
    # 陪玩本来就不该自己去种地做饭（玩家就在旁边），所以这里落回上游默认；
    # **不影响吃**：吃走 `autoEat` + `minimumFoodAllowed`，是另一条分支。
    d.setdefault("foodUnitsToCollect", 0)
    if d.get("foodUnitsToCollect") not in (0, None):   # 旧模板留下的 12 → 归零
        d["foodUnitsToCollect"] = 0
    changed = d != before
    if changed:
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(p), "created": created, "changed": changed,
            "mcpPort": d.get("mcpPort"), "token_tail": str(d.get("mcpAuthToken"))[-6:]}


# ── ⑤ 启动参数（复用已验证的启动器，不重复造）────────────────────────────────
def launch_cmd(game_dir: Path, version: str, username: str = "Buddy",
               *, connect: str = "", mcp_port: int = 25351,
               hidden: bool = True, desktop: str = "Buddy") -> list[str]:
    launcher = ROOT / "tools" / "_mc121_launch.py"
    cmd = py_prefix() + [str(launcher),
           "--name", username, "--workdir", str(game_dir),
           "--mcp-port", str(mcp_port)]
    if connect:
        host, _, port = connect.partition(":")
        cmd += ["--host", host or "127.0.0.1", "--port", port or "25565"]
    else:
        cmd += ["--no-connect"]
    # 2026-10-29 operator：加 buddy 时窗口**默认不可见**；debug 时用 hidden=False 起一份可见的。
    # ⚠️ W6-3 实测：独立桌面（`--desktop`）会让渲染主循环停 ⇒ 准星不更新 ⇒ 干不了活。
    # "看不见但能干活"的正解是 `--offscreen`（默认桌面 + 屏幕外留 1px）。
    if hidden:
        cmd += ["--offscreen"]
    return cmd


# ── ⑥ 服务端（本地 Paper）：状态/起停走已有的 warden，不重写 ─────────────────
def server_status(port: int = 25566) -> dict:
    w = ROOT / "tools" / "server_warden.py"
    try:
        out = subprocess.run(py_prefix() + [str(w), "status"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=25)
        line = next((ln for ln in (out.stdout or "").splitlines()
                     if str(port) in ln), "")
        return {"ok": bool(line), "line": line.strip()}
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:120]}


# ── ⑦ 诊断包（beta 必备：崩了以后一键收集）──────────────────────────────────
def diagnostics(out_dir: Path | str, *, game_dir: Path | None = None) -> dict:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    items = []

    def grab(src: Path, name: str, tail: int = 200) -> None:
        try:
            if not src.exists():
                return
            lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
            (d / name).write_text("\n".join(lines[-tail:]), encoding="utf-8")
            items.append(name)
        except Exception:
            pass

    runs = ROOT / "runs"
    cmd_dir = max((p for p in runs.glob("commander/*") if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, default=None)
    if cmd_dir:
        grab(cmd_dir / "trace.jsonl", "trace_tail.jsonl", 200)
    grab(ROOT / "runs" / "gate" / "last.json", "gate.json", 400)
    grab(ROOT / "runs" / "state" / "last_plan.json", "last_plan.json", 200)
    logs = sorted((BUDDY_CLIENTS / "logs").glob("client-Buddy-*.log"),
                  key=lambda p: p.stat().st_mtime)
    if logs:
        grab(logs[-1], "client_tail.log", 200)
    env = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "python": sys.version.split()[0],
           "cwd": str(ROOT), "game_dir": str(game_dir) if game_dir else "",
           "ports": {str(p): _port_open(p) for p in (25566, 25576, 25351)}}
    (d / "env.json").write_text(json.dumps(env, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    items.append("env.json")
    return {"ok": True, "dir": str(d), "items": items}


def _port_open(port: int) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


# ── ③b 多实例开通（端口/目录/设置一次配好）──────────────────────────────────
def provision_instance(base_dir: Path | str, index: int, *, real_mods: Path | str,
                       mcp_port: int = 25351, gateway_port: int = 25341,
                       name: str = "") -> dict:
    """给第 N 个陪玩准备一个独立 gameDir：自己的 altoclef 设置 + 指向真实 mods 的目录联接。

    为什么不是复制 mods：一个 jar 60MB，复制 4 份纯浪费；`mklink /J` 是目录联接
    （不需要管理员权限），Fabric 看到的就是一个普通 mods 目录。
    """
    idx = int(index)
    d = Path(base_dir) / f"buddy{idx}"
    (d / "altoclef").mkdir(parents=True, exist_ok=True)
    mods = Path(real_mods)
    link = d / "mods"
    out = {"dir": str(d), "mods": str(link), "mcp_port": mcp_port,
           "gateway_port": gateway_port, "name": name or f"Buddy{idx if idx > 1 else ''}"}
    if not link.exists():
        try:
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(mods)],
                           capture_output=True, text=True, timeout=30)
            out["junction"] = link.exists()
        except Exception as e:
            out["junction"] = False
            out["err"] = f"{type(e).__name__}: {e}"[:120]
    else:
        out["junction"] = True
    s = ensure_settings(d, mcp_port=mcp_port, gateway_port=gateway_port)
    out["settings"] = s.get("path")
    out["settings_ok"] = bool(s.get("ok"))
    return out


# ── ③c jar 回滚（更新出问题时一键退回）──────────────────────────────────────
def rollback_jar(game_dir: Path, version: str) -> dict:
    gd = Path(game_dir)
    restored = []
    for d in _mods_dirs(gd, version):
        baks = sorted(d.glob("*unionclef*.jar.bak"), key=lambda p: p.stat().st_mtime)
        if not baks:
            continue
        bak = baks[-1]
        target = bak.with_suffix("")                     # 去掉 .bak
        cur = d / target.name
        if cur.exists():
            cur.replace(cur.with_suffix(cur.suffix + ".prev"))
        bak.replace(target)
        restored.append(str(target))
    return {"ok": bool(restored), "restored": restored,
            "why": "" if restored else "没有 .bak 可回滚"}


# ── ⑦b 诊断打包（一键上报用：一个 zip + 一段人话说明）─────────────────────────
def bundle(out_dir: Path | str, *, game_dir: Path | None = None) -> dict:
    import zipfile
    rep = diagnostics(out_dir, game_dir=game_dir)
    d = Path(out_dir)
    z = d.with_suffix(".zip")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in d.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(d))
    note = (f"诊断包：{z.name}\n"
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"请把整个 zip 发给维护者；里面有 trace 尾、客户端日志尾、端口与环境快照（不含密钥）。")
    (d / "README.txt").write_text(note, encoding="utf-8")
    return {"ok": True, "zip": str(z), "size_mb": round(z.stat().st_size / 1e6, 2),
            "items": rep.get("items"), "note": note}


# ── ③d 语音模型：有没有 / 要不要下载 ───────────────────────────────────────
def model_status(dest: Path | str = MODELS_DIR) -> dict:
    root = Path(dest)
    onnx = sorted(root.rglob("*.onnx")) if root.exists() else []
    hit = [p for p in onnx if "sense" in str(p).lower() or "model.int8" in p.name.lower()]
    if hit:
        d = hit[0].parent
        return {"ok": True, "dir": str(d), "onnx": hit[0].name,
                "has_tokens": (d / "tokens.txt").exists()}
    return {"ok": False, "why": f"{root} 里没找到 SenseVoice 模型", "dir": str(root)}


def ensure_model(dest: Path | str = MODELS_DIR, *, url: str = "",
                 sha256: str = "", repo: str = "", on_line=None) -> dict:
    """首启用：没有模型就按 url 下；没给 url 就返回"怎么手动放"的说明（不假装成功）。

    两种来源（W2-2）：
      · `url`  = **一个压缩包**（.tar.bz2/.zip）→ 交给 get_voice_model.py（已支持 sha256/断点）；
      · `repo` = **HF 仓库的 resolve 前缀**（例：`https://hf-mirror.com/<org>/<name>/resolve/main`）
        → 直接抓 `model.int8.onnx` + `tokens.txt` 两个文件（sherpa 的 SenseVoice 包就这两个）。
          ⚠️ 实测该仓库**没有**打包好的一体包，只有散文件；只写 tar 直链等于让首启必失败。

    `on_line(text)` 给了就**边下边回调**（UI 进度条用；不给则静默）。
    """
    st = model_status(dest)
    if st.get("ok"):
        return {**st, "action": "already"}
    if url:
        cmd = py_prefix() + [str(ROOT / "tools" / "get_voice_model.py"),
               "--url", url, "--dest", str(dest)]
        if sha256:
            cmd += ["--sha256", sha256]
    elif repo:
        return _fetch_repo_files(Path(dest), repo, sha256=sha256, on_line=on_line)
    else:
        return {**st, "action": "guide",
                "guide": ("python -X utf8 tools/get_voice_model.py --guide "
                          "（下好放到 %s/<模型目录>/model.int8.onnx + tokens.txt）" % dest)}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=1800)
        out = (r.stdout or "") + (r.stderr or "")
        if on_line:
            for line in out.splitlines():
                on_line(line)
        return {**model_status(dest), "action": "download", "rc": r.returncode,
                "tail": out[-400:]}
    except Exception as e:
        return {**st, "action": "download", "rc": -1,
                "err": f"{type(e).__name__}: {e}"[:160]}


MODEL_FILES = ("model.int8.onnx", "tokens.txt")


def _fetch_repo_files(dest: Path, repo: str, *, sha256: str = "", on_line=None) -> dict:
    """从 HF 风格仓库前缀抓 SenseVoice 的两个文件（带断点 + 进度 + 可选 sha256）。

    落点：`<dest>/<仓库名>/model.int8.onnx|tokens.txt` —— 正好是 `model_status()` 认的形状，
    也是 `config/voice.json` 的 `sherpa_model_dir` 能直接写的形状。
    """
    import hashlib
    name = repo.rstrip("/").split("/resolve/")[0].rstrip("/").split("/")[-1] \
        or "sherpa-onnx-sense-voice"
    target = dest / name
    target.mkdir(parents=True, exist_ok=True)
    say = on_line or (lambda _t: None)
    want = (sha256 or "").strip().lower()
    try:
        for fn in MODEL_FILES:
            out = target / fn
            if out.exists() and out.stat().st_size > 0:
                say(f"{fn}: 已存在（{out.stat().st_size / 1e6:.1f} MB），跳过")
                continue
            part = out.with_suffix(out.suffix + ".part")
            have = part.stat().st_size if part.exists() else 0
            req = urllib.request.Request(repo.rstrip("/") + "/" + fn,
                                         headers={"User-Agent": "BuddyLauncher/1"})
            if have:
                req.add_header("Range", f"bytes={have}-")
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=60) as r, \
                    part.open("ab" if have else "wb") as fh:
                total = int(r.headers.get("Content-Length") or 0) + have
                got = have
                last = 0.0
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    fh.write(b)
                    got += len(b)
                    now = time.time()
                    if now - last > 0.5 or got == total:
                        last = now
                        pct = (got * 100.0 / total) if total else 0.0
                        say(f"{fn}: {got / 1e6:.1f}/{total / 1e6:.1f} MB  "
                            f"{pct:.1f}%  {got / 1e6 / max(0.1, now - t0):.1f} MB/s")
            part.replace(out)
            say(f"{fn}: 完成 {out.stat().st_size / 1e6:.1f} MB")
        if want:
            h = hashlib.sha256()
            with (target / "model.int8.onnx").open("rb") as fh:
                for b in iter(lambda: fh.read(1 << 20), b""):
                    h.update(b)
            got = h.hexdigest()
            if got != want:
                # ⚠️ 不能只报错就把坏文件留在原地：下一轮 `model_status()` 会把它当"已装好"，
                #    于是坏模型从此永久生效（2026-10-29 实测：错误 sha 第二次调用就通过了）。
                bad = target / "model.int8.onnx"
                try:
                    bad.replace(bad.with_suffix(".onnx.bad"))
                except Exception:
                    pass
                return {"ok": False, "action": "download", "dir": str(target),
                        "why": f"sha256 不匹配（期望 {want[:12]}… 实际 {got[:12]}…）"
                               f"；坏文件已改名 .onnx.bad，重试会重下"}
            say("sha256 校验通过")
        st = model_status(dest)
        return {**st, "action": "download", "dir": str(target)}
    except Exception as e:
        return {"ok": False, "action": "download", "dir": str(target),
                "why": f"{type(e).__name__}: {e}"[:160]}


# ── ③e 更新检查（有新 jar 就提示，不自动替换）──────────────────────────────
RELEASE_DIR = ROOT / "_releases"


def release_meta(rel_dir: Path | str = RELEASE_DIR) -> dict:
    """读发布目录的 latest.json（没有就空 dict）。"""
    p = Path(rel_dir) / "latest.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def check_update(game_dir: Path, version: str, new_jar: Path | str = DEFAULT_JAR,
                 *, rel_dir: Path | str = RELEASE_DIR) -> dict:
    """有更新吗？**优先看发布目录的 latest.json**（有版本号 + sha256），否则退回老的文件名/大小比较。"""
    meta = release_meta(rel_dir)
    if meta.get("file"):
        cand = Path(rel_dir) / str(meta["file"])
        if not cand.exists():
            return {"update": False, "why": f"latest.json 指向的文件不在：{cand}",
                    "version": meta.get("version")}
        cur = []
        for d in _mods_dirs(Path(game_dir), version):
            cur += list(d.glob("*unionclef*.jar"))
        inst = cur[0] if cur else None
        if inst is None:
            return {"update": True, "installed": "", "new": cand.name,
                    "version": meta.get("version"), "sha256": meta.get("sha256"),
                    "why": "还没装"}
        same = (inst.name == cand.name and inst.stat().st_size == cand.stat().st_size)
        if same and meta.get("sha256"):
            same = file_sha256(inst) == str(meta["sha256"])
        return {"update": not same, "installed": inst.name, "new": cand.name,
                "version": meta.get("version"), "sha256": meta.get("sha256"),
                "notes": meta.get("notes") or "",
                "why": "已是最新" if same else f"本地 {inst.name} ≠ 发布 {cand.name}"}
    nj = Path(new_jar)
    if not nj.exists():
        return {"update": False, "why": f"新 jar 不存在：{nj}"}
    cur = []
    for d in _mods_dirs(Path(game_dir), version):
        cur += list(d.glob("*unionclef*.jar"))
    if not cur:
        return {"update": True, "installed": "", "new": nj.name, "why": "还没装"}
    same = [p for p in cur if p.name == nj.name and p.stat().st_size == nj.stat().st_size]
    return {"update": not same, "installed": cur[0].name, "new": nj.name,
            "why": "已是最新" if same else "版本/大小不同"}


def file_sha256(p: Path | str) -> str:
    import hashlib
    h = hashlib.sha256()
    with Path(p).open("rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def install_update(game_dir: Path, version: str, *, rel_dir: Path | str = RELEASE_DIR,
                   to_version: str = "") -> dict:
    """按发布目录装一个新版本（带备份；`to_version` 给了就从 history/ 回滚到那一版）。"""
    meta = release_meta(rel_dir)
    if to_version and to_version != meta.get("version"):
        try:
            idx = json.loads((Path(rel_dir) / "versions.json").read_text(encoding="utf-8"))
        except Exception:
            idx = {}
        info = idx.get(to_version)
        if not info:
            return {"ok": False, "why": f"发布目录里没有版本 {to_version}"}
        rel = str(info.get("file") or "")
        jar = Path(rel_dir) / rel
        if not jar.exists():                       # 老版本可能被归档到 history/
            jar = Path(rel_dir) / "history" / Path(rel).name
        if not jar.exists():
            return {"ok": False, "why": f"版本 {to_version} 的文件找不到：{rel}"}
        return {**install_mod(Path(game_dir), version, jar), "to_version": to_version}
    if not meta.get("file"):
        return {"ok": False, "why": "没有 latest.json（先跑 tools/publish_jar.py）"}
    jar = Path(rel_dir) / str(meta["file"])
    if not jar.exists():
        return {"ok": False, "why": f"发布文件不在：{jar}"}
    r = install_mod(Path(game_dir), version, jar)
    return {**r, "version": meta.get("version"), "sha256": meta.get("sha256")}


# ── ⑦c 一键准备（向导）：把散落的按钮串成一条可判定的流程 ────────────────────
def tune_client(game_dir: Path | str, version: str = "") -> dict:
    """把陪玩客户端的 options.txt 调成"一直能干活"的样子。

    为什么（2026-10-29 实测）：MCP 的每个杠杆都要 `MinecraftClient.execute()` **排到渲染线程**，
    所以一次调用的延迟 = 一帧。开着 vsync 且窗口在后台时，客户端实测落到 **10 FPS**
    ⇒ `getGameState` 稳定 **100ms**、`getBlockAt` 24ms；关掉 vsync 之后同一台机器
    ⇒ 一帧以内（个位数毫秒）。
    ⇒ 陪玩是"看不见的后台角色"，不是给人看的游戏：**vsync 必须关**，帧率给个上限防止三开烧 CPU。

    只改这四行，别的键原样保留（用户的其它设置不动）。
    """
    gd = Path(game_dir)
    path = (gd / "versions" / version / "options.txt") if version else (gd / "options.txt")
    if not path.exists():
        alt = gd / "options.txt"
        if alt.exists():
            path = alt
        else:
            return {"ok": False, "why": f"没找到 options.txt：{path}"}
    want = {"enableVsync": "false", "maxFps": "120",
            "inactivityFpsLimit": '"minimized"', "pauseOnLostFocus": "false",
            # ⛔ 2026-10-30 血证（真因）：新客户端第一次起会停在**无障碍引导页**
            # （`AccessibilityOnboardingScreen`），而我们的模组是在 `TitleScreen.init()` 里
            # 打印 "Global Init" 并**启动 MCP 服务**的 ⇒ 引导页不出来 = 主菜单不初始化 =
            # MCP 永远不监听（外面看就是"客户端卡在加载、mcp=False"，日志停在创建贴图集那行）。
            # 这一条是**必须**的：任何新装机器不写它，陪玩都起不来。
            "onboardAccessibility": "false"}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:160]}
    changed = []
    have = {}
    out = []
    for line in raw:
        k, _, v = line.partition(":")
        if k in want:
            have[k] = v
            if v != want[k]:
                out.append(f"{k}:{want[k]}")
                changed.append(f"{k}: {v} → {want[k]}")
            else:
                out.append(line)
        else:
            out.append(line)
    for k, v in want.items():
        if k not in have:
            out.append(f"{k}:{v}")
            changed.append(f"{k}: (缺) → {v}")
    if changed:
        try:
            path.write_text("\n".join(out) + "\n", encoding="utf-8")
        except Exception as e:
            return {"ok": False, "why": f"{type(e).__name__}: {e}"[:160]}
    return {"ok": True, "path": str(path), "changed": changed}


def first_run_steps(game_dir: Path | str, version: str, *, mcp_port: int = 25351,
                    model_dest: Path | str = MODELS_DIR, model_url: str = "",
                    jar: Path | str = DEFAULT_JAR, server_port: int = 25566) -> dict:
    """按顺序做一遍"装好能用"的六步，每步都给 ✅/❌ + 一句话。

    设计原则：**能自动的都自动，不能自动的明确说"要人做什么"**，
    绝不因为某步失败就整条中断（后面的步骤能跑就跑，最后统一给结论）。
    """
    gd = Path(game_dir)
    steps: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": name, "ok": bool(ok), "detail": str(detail)[:200]})

    add("① 检查游戏目录/版本", (gd / "versions" / version).exists(),
        f"{gd} / {version}")
    r = install_mod(gd, version, jar)
    add("② 安装/更新陪玩 jar", r.get("ok", False),
        f"放到 {len(r.get('placed') or [])} 处；备份 {len(r.get('backed_up') or [])} 个"
        if r.get("ok") else r.get("why", ""))
    s = ensure_settings(gd, mcp_port=mcp_port)
    tc = tune_client(gd, version)
    add("③ 写入 altoclef 设置", s.get("ok", False),
        f"{s.get('path')}（token …{s.get('token_tail')}）"
        + ("" if tc.get("ok") else f"；⚠️ options 没调成：{tc.get('why')}"))
    ms = model_status(model_dest)
    if not ms.get("ok") and model_url:
        ms = {**ensure_model(model_dest, url=model_url)}
        add("④ 语音模型（自动下载）", ms.get("ok", False), ms.get("dir", ""))
    else:
        add("④ 语音模型", ms.get("ok", False),
            ms.get("dir") if ms.get("ok") else (ms.get("why") or "") + "（可跳过：不配也能用 SAPI）")
    sv = server_status(server_port)
    add("⑤ 服务端探活", sv.get("ok", False), sv.get("line") or sv.get("why", ""))
    cmd = launch_cmd(gd, version, "Buddy", connect=f"127.0.0.1:{server_port}",
                     mcp_port=mcp_port)
    add("⑥ 启动命令成形", "--mcp-port" in cmd and ("--offscreen" in cmd or "--desktop" in cmd),
        " ".join(cmd[-4:]))
    hard = [s for s in steps if not s["ok"] and not s["step"].startswith("④")]
    return {"ok": not hard, "steps": steps,
            "next": ("可以点「启动陪玩」了" if not hard else
                     "先解决标 ❌ 的步骤（④ 语音模型可以跳过）")}


# ── ⑦d 支持闭环：诊断包上传（opt-in；默认不发）──────────────────────────────
SUPPORT_CFG = ROOT / "config" / "support.json"


def support_cfg() -> dict:
    try:
        d = json.loads(SUPPORT_CFG.read_text(encoding="utf-8"))
        return {"enabled": False, "endpoint": "", "auto_crash": True, **d}
    except Exception:
        return {"enabled": False, "endpoint": "", "auto_crash": True}


def save_support(**kw) -> dict:
    d = support_cfg()
    d.update({k: v for k, v in kw.items() if v is not None})
    SUPPORT_CFG.parent.mkdir(parents=True, exist_ok=True)
    SUPPORT_CFG.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def upload_diag(zip_path: Path | str, *, endpoint: str = "", timeout: float = 30.0) -> dict:
    """把诊断 zip POST 出去（multipart）。**没有 endpoint 就什么都不做**，绝不偷偷发。"""
    cfg = support_cfg()
    url = endpoint or str(cfg.get("endpoint") or "")
    if not cfg.get("enabled") or not url:
        return {"ok": False, "why": "未开启或没配 endpoint（本地留存）"}
    z = Path(zip_path)
    if not z.exists():
        return {"ok": False, "why": f"没有这个文件：{z}"}
    try:
        import uuid
        boundary = "----buddy" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{z.name}\"\r\nContent-Type: application/zip\r\n\r\n").encode()
        body += z.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "BuddyDiag/1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ok": 200 <= r.status < 300, "status": r.status, "bytes": len(body)}
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:160]}


# ── ⑧ 自检（离线，可进闸）──────────────────────────────────────────────────
def selftest() -> dict:
    import tempfile
    rep: dict = {"checks": []}

    def ok(name: str, cond: bool, detail: str = "") -> None:
        rep["checks"].append({"name": name, "ok": bool(cond), "detail": str(detail)[:160]})

    dirs = find_game_dirs()
    ok("找得到游戏目录", bool(dirs), f"{len(dirs)} 个")
    tmp = Path(tempfile.mkdtemp(prefix="launcher_"))
    gd = tmp / ".minecraft"
    ver = "1.21.11-Fabric 0.19.3"
    (gd / "versions" / ver).mkdir(parents=True)
    inst = list_instances(gd)
    ok("实例清单", len(inst) == 1 and inst[0]["version"] == ver, inst)
    # 用一个假 jar 验证"两个 mods 目录都放"
    fake = tmp / "fake-unionclef.jar"
    fake.write_bytes(b"PK\x03\x04fake")
    r = install_mod(gd, ver, fake)
    ok("jar 装到两个 mods 目录", r["ok"] and len(r["placed"]) == 2, r.get("placed"))
    s = ensure_settings(gd, mcp_port=25351)
    ok("设置写入且 token 非空", s["ok"] and s["token_tail"], s.get("path"))
    s2 = ensure_settings(gd, mcp_port=25351)
    ok("二次调用不改文件", s2["changed"] is False, s2)
    inst2 = list_instances(gd)
    ok("实例能认出陪玩 jar", bool(inst2[0]["buddy_jar"]), inst2[0])
    cmd = launch_cmd(gd, ver, "Buddy", connect="127.0.0.1:25566")
    ok("启动命令成形", "--mcp-port" in cmd and "--port" in cmd, " ".join(cmd[-6:]))
    # 默认不可见（屏幕外留 1px）+ debug 可见（不带隐藏参数）
    # ⚠️ W6-3 实测：`--desktop Buddy`（独立 Windows 桌面）会让**渲染主循环停** ——
    # crosshairTarget 冻住 ⇒ tungsten 的瞄准/挖掘全废 ⇒ 那台客户端干不了活。
    # 所以"看不见"改成 `--offscreen`（默认桌面 + 屏外留 1px）：看不见，但渲染照常。
    ok("默认启动=看不见但能干（offscreen）",
       "--offscreen" in cmd and "--desktop" not in cmd, " ".join(cmd[-4:]))
    vis = launch_cmd(gd, ver, "Buddy", connect="127.0.0.1:25566", hidden=False)
    ok("调试可见=不带隐藏参数",
       "--desktop" not in vis and "--offscreen" not in vis, " ".join(vis[-4:]))
    try:
        sys.path.insert(0, str(ROOT / "launcher"))
        import win32_desktop as wd
        ok("隐藏桌面助手可用", bool(wd.ensure_desktop("Buddy")), "OpenDesktop/CreateDesktop")
        ok("窗口屏外判据可用",
           wd.is_offscreen(wd.find_windows("Minecraft")[0]) in (True, False),
           "is_offscreen() 返回布尔")
    except Exception as e:
        ok("隐藏桌面助手可用", False, f"{type(e).__name__}: {e}")
    dx = diagnostics(tmp / "diag", game_dir=gd)
    ok("诊断包生成", dx["ok"] and "env.json" in dx["items"], dx.get("items"))
    bz = bundle(tmp / "diag2", game_dir=gd)
    ok("诊断打包成 zip", bz["ok"] and Path(bz["zip"]).exists(), f"{bz.get('size_mb')} MB")
    # 多实例开通：mods 用目录联接指向"真实 mods"
    real_mods = tmp / "real_mods"
    real_mods.mkdir(parents=True, exist_ok=True)
    (real_mods / "unionclef-x.jar").write_bytes(b"PKfake")
    inst3 = provision_instance(tmp / "clients", 3, real_mods=real_mods,
                               mcp_port=25353, gateway_port=25345)
    ok("多实例开通（独立端口+设置）", inst3["settings_ok"] and inst3["mcp_port"] == 25353,
       inst3.get("settings"))
    ok("实例 mods 目录可用", Path(inst3["mods"]).exists(), inst3.get("junction"))
    # jar 回滚：装两次（第二次是新 jar）→ 回滚应恢复旧的那份
    j1 = tmp / "unionclef-old.jar"; j1.write_bytes(b"PKold")
    j2 = tmp / "unionclef-new.jar"; j2.write_bytes(b"PKnew")
    install_mod(gd, ver, j1)
    install_mod(gd, ver, j2)
    rb = rollback_jar(gd, ver)
    ok("jar 一键回滚", rb["ok"] and any("old" in p for p in rb["restored"]), rb.get("restored"))
    # 更新检查 / 模型状态
    up = check_update(gd, ver, j2)
    ok("更新检查", up["update"] in (True, False), up.get("why"))
    ms = model_status(tmp / "models")
    ok("模型缺失时明确说没有", ms["ok"] is False, ms.get("why"))
    em = ensure_model(tmp / "models")
    ok("没有 url 时给指南而非假装成功", em["action"] == "guide", em.get("action"))
    fr = first_run_steps(gd, ver, model_dest=tmp / "nomodel", server_port=25566)
    ok("一键准备向导（六步齐全）", len(fr["steps"]) == 6 and "next" in fr,
       " / ".join(("✅" if s["ok"] else "❌") for s in fr["steps"]))
    rep["ok"] = all(c["ok"] for c in rep["checks"])
    return rep


if __name__ == "__main__":
    r = selftest()
    for c in r["checks"]:
        print(("✅ " if c["ok"] else "❌ ") + c["name"] + "  " + c["detail"])
    print("PASS" if r["ok"] else "FAIL")
    raise SystemExit(0 if r["ok"] else 1)
