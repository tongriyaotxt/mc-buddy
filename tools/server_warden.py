"""tools/server_warden.py —— 服务端看门狗：跑批前探活，死了就拉起来，起不来就 fail-fast

为什么需要它（2026-09-20 f10 批次血证）：
    seq_runs / frozen_run 跑批前不探活，服务端没起时整批 ECONNREFUSED 全废，
    还要等超时才发现。看门狗把"探活 → 起服 → 等待可连接 → RCON 复核"收成一个入口。

2026-09-21 加固（基建可信化批次）：
    · SERVERS 每服带 JVM profile（Xms/Xmx + Aikar 参数 + HeapDumpOnOutOfMemoryError + gc.log），
      告别裸 `-Xmx2G`——chunkgen 尖峰/OOM 从此有 GC 日志和 heap dump 可查
    · 起服前三道闸：端口没被占 / 同目录没有 Java 进程在跑 / 可用内存 ≥ 阈值（默认 6GB，可覆盖）
    · 未注册端口默认不再静默当成 ready，需 --allow-unregistered（防打错端口空烧整批）

用法：
    python tools/server_warden.py status                              # 所有服务端状态一览
    python tools/server_warden.py ensure --port 25569                 # 确保这台在跑（不在就拉起）， exit 0=就绪
    python tools/server_warden.py ensure --all                        # 全部拉起
    python tools/server_warden.py ensure --port 25570 --allow-unregistered   # 未注册端口：只探活
    python tools/server_warden.py ensure --port 25569 --min-free-gb 8 # 覆盖内存阈值

被其他工具 import：
    from server_warden import ensure_by_port
    if not ensure_by_port(25569): sys.exit(2)
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

JAVA = Path(r"%JAVA_HOME%\bin\java.exe")

DEFAULT_MIN_FREE_GB = 4.5
# ⚠️⚠️ 2026-10-17 08:00（agent-1 ✓ 夜班实测）：**6.0 在"有人陪着跑"的机器上会把整条车道饿死** ✗★
#   现场：`free` 长期只有 **5.2~5.7GB**（本机常驻 Ld9BoxHeadless 3.3G ＋ 6 个 node 共 ~3.6G ＋ 系统缓存 ✓），
#     于是 warden **每一轮都拒绝起服** ⇒ 快车道"10 秒失败一次"空转 ✓（`rc=1 且没读到 summary.json` ✗）
#   ⇒ 阈值 6.0 → **4.5** ✓（服务端 3G ＋ 余量 1.5G ⇒ 起得来且不会把机器压死 ✓）
#   ⚠️ 与上面那条"3.2 处置手册"是同一族 ✓ —— 但 3.2 是"内存被挤到 4GB 级"时的应急值，
#      4.5 是"长期中等水位"的稳态值 ✓；**机器回到 6GB 以上时请改回 6.0** ✓
# ⚠️ 2026-10-15 20:58 **已回滚** ✓（本晚 19:08 曾因内存被挤临时下调到 3.2 ✓，用法见下 ✓）：
#   症状：可用内存掉到 3.9~5.6GB（安卓模拟器 2.3GB ＋ 多个 dsh 会话 ✗ 都不是本项目的东西 ⇒ 不动它 ✗）
#        ⇒ warden 连续拒起服 ⇒ 快车道 3 次尝试 10 秒跑完 ✗
#        （签名像"策略一开局就崩"，其实是**起服闸** ✗★ —— 先看 `_s3lane00N_*.log` 再怀疑策略 ✓）
#   处置（照 25570 那条"内存被挤时的处置手册" ✓）：阈值 6.0 → 3.2 ✓ ＋ 25570 堆 → 1536M ✓
#   回滚条件（本条 ✓）：系统可用内存回到 6GB 以上 ✓ —— 20:58 实测 9.02GB ⇒ 已改回 6.0 ✓
#   ⚠️ 踩过的坑：写 `1.5G` ⇒ JVM 直接 `Invalid initial heap size` ✗（**要写 1536M** ✓）
DEFAULT_MIN_FREE_GB_NOTE = "lowered-3.2-and-restored-2026-10-15"

# Aikar 常用 G1GC 参数（MC 服务端通用，1.16~1.21 都认）
AIKAR_FLAGS = [
    "-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200",
    "-XX:+UnlockExperimentalVMOptions", "-XX:+DisableExplicitGC", "-XX:+AlwaysPreTouch",
    "-XX:G1NewSizePercent=30", "-XX:G1MaxNewSizePercent=40", "-XX:G1HeapRegionSize=8M",
    "-XX:G1ReservePercent=20", "-XX:G1HeapWastePercent=5", "-XX:G1MixedGCCountTarget=4",
    "-XX:InitiatingHeapOccupancyPercent=15", "-XX:G1MixedGCLiveThresholdPercent=90",
    "-XX:G1RSetUpdatingPauseTimePercent=5", "-XX:SurvivorRatio=32",
    "-XX:+PerfDisableSharedMem", "-XX:MaxTenuringThreshold=1",
]

# 服务端注册表：port → (目录, rcon_port, 版本, 备注, JVM profile)
# xms/xmx 按内存预算定：1.20.4 3G 起步，1.21.11 4G 起步（计划 §4）；1.16.5 在 Docker 里（CLI 不在 PATH），只探活不拉起。
SERVERS = {
    # ⚠️⚠️ 2026-10-11 【F287-b】**本机可用内存跌到 4.4GB ⇒ 3G 堆过不了 6GB 内存闸** ✗★
    #   血证（第 11 班通宵档 ✓）：`f287a` 臂 **20 局全 `infra_fail`（stage=ensure）** ✗ ——
    #     而 `ensure` 的拒绝理由只有一行 ✓：`可用内存 4.5GB < 阈值 6.0GB，拒绝起服` ✓
    #     （判据 56 ✓：**先看 warden 的拒绝理由，别先怀疑 Paper** ✓ —— 台账 F261 记过同一个病 ✓）
    #   真凶是**机器上别的程序**（Android 模拟器 1.8GB ✓ / 一堆 MCP 服务器 1GB ✓ /
    #     多个 harness 会话 1.4GB ✓ / svchost+系统 ✓ …）⇒ 残留 java 一个都没有 ✓（都杀干净了 ✓）
    #   ⇒ 处置（**不改别人的程序** ✓，只在项目内可逆地调 ✓）：**25565 的堆 3G → 2G** ✓，
    #     并把开批时的内存闸放到位（`--extra "--min-free-gb 4.0"` ✓）——
    #     ⚠️ 这与台账 11511 警告的那种用法**不是一回事** ✗：
    #       那次是"**两臂 × 3G 硬塞**"✗（GC 抖动 ⇒ 数据变差 ✗）；
    #       这次是"**单臂 × 2G**"✓（堆本身降了 ⇒ 预触内存也降了 ✓）。
    #   ⚠️⚠️ 18:25 又跌到 **3.9GB** ✗ —— 查到真凶：**18:25:25 有人起了安卓模拟器** ✗★
    #     （`Ld9BoxHeadless` **2.3GB** ＋ `dnplayer` 0.5GB ✗ —— 那不是本项目的东西 ✓，**不动它** ✗）
    #     ⇒ 本夜再把堆降到 **1.5G** ✓（`--extra "--min-free-gb 3.2"` ✓）——
    #       dev 靶场（一个 bot、末地一张图）1.5G 够用 ✓；⚠️ **代价如实记**：
    #       GC 更频繁 ⇒ 若读数出现"全局变慢"的迹象 ✗，先怀疑这一条 ✓（而不是先怀疑策略 ✗）
    #     ⇒ 📌 **内存回到 6GB 以上时，把本行改回 3G/3G** ✓（一行 ✓）
    #   ✅ **2026-10-11 19:5x 已恢复 3G/3G** ✓ —— 用户把雷电模拟器关了 ✓（可用内存 0.98 → 9.38GB ✓）
    #     （下面三段留着当"内存被挤时的处置手册" ✓ —— 下次再遇到，照它做 ✓）
    25565: dict(dir=r"%MC_SERVERS%\paper-1.20.4",   rcon=25575, version="1.20.4",  note="训练主力",
                xms="3G", xmx="3G"),
    25566: dict(dir=r"%MC_SERVERS%\paper-1.21.11",  rcon=25576, version="1.21.11", note="最终部署形态",
                xms="4G", xmx="4G"),
    25567: dict(dir=r"%MC_SERVERS%\paper-1.18.2",   rcon=25577, version="1.18.2",  note="回归门",
                xms="2G", xmx="2G"),
    25568: dict(dir=None,                            rcon=25578, version="1.16.5",  note="Docker，只探活",
                container="buddy-mc-1165"),
    25569: dict(dir=r"%MC_SERVERS%\paper-1.20.4-b", rcon=25579, version="1.20.4",  note="seq/chain 专用",
                xms="3G", xmx="3G"),
    25570: dict(dir=r"%MC_SERVERS%\paper-1.20.4-c", rcon=25580, version="1.20.4",  note="并行第三台（P1 样本量）",
                # ✅ 2026-10-15 20:58 **已回滚到 3G/3G** ✓（可用内存回到 9.02GB ✓）
                #   —— 本晚 19:08~20:45 曾按上面那段"内存被挤时的处置手册"临时降到 1536M ✓（一代班次 ✓）
                #   📌 若再遇"可用内存 < 4GB"：照手册降 1536M ✓ ＋ 把 `DEFAULT_MIN_FREE_GB` 降到 3.2 ✓
                #   ⚠️ 教训（一行 ✓）：**JVM 参数必须整数** —— `1.5G` 会被拒（`Invalid initial heap size` ✗），
                #      要写 `1536M` ✓；而且那道报错在 `warden-console.log` 里，**不在** warden 的 stdout ✗★
                xms="3G", xmx="3G"),
    # ⚠️ 2026-10-08 第 9 班注册：**跑批吞吐 +50%** ✓
    #   起因：`25569` 映射到 `paper-1.20.4-b`，**那个目录根本不存在** ✗ ⇒ 在它上面开的臂
    #     12 局**全是 `infra_fail`**（`f204b` ✓，日志逐局一行、连服务端都没起 ✗）。
    #   而 `%MC_SERVERS%\paper-1.20.4-real1` 是**现成的完整服务端** ✓
    #     （`paper.jar` ✓、`eula=true` ✓、`server-port=25571` ✓、`rcon.port=25581` ✓ 都已配好 ✓）
    #   ⇒ 注册成第 4 台 ✓（不动 25569 那条 —— 它留给 seq/chain ✓，用不用得成是它自己的事 ✓）
    25571: dict(dir=r"%MC_SERVERS%\paper-1.20.4-real1", rcon=25581, version="1.20.4", note="并行第四台（P1 样本量）",
                xms="3G", xmx="3G"),
}


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def free_mem_gb() -> float:
    """可用物理内存（GB）。优先 psutil，无则走 Win32 GlobalMemoryStatusEx，都不行返回 -1（跳过检查并告警）"""
    try:
        import psutil  # noqa: E402
        return psutil.virtual_memory().available / (1 << 30)
    except Exception:
        pass
    try:
        import ctypes  # noqa: E402

        class MEMSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MEMSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullAvailPhys / (1 << 30)
    except Exception:
        return -1.0


def java_pids_with_cmdline() -> list[tuple[int, str]]:
    """列出本机 java 进程 (pid, 命令行)。同目录双开检测用；查不到（无 PowerShell/CIM）返回 []"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like 'java%'\" | "
             "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        ).stdout
        res = []
        for line in out.splitlines():
            if "\t" in line:
                pid, cmd = line.split("\t", 1)
                if pid.strip().isdigit():
                    res.append((int(pid.strip()), cmd.strip()))
        return res
    except Exception:
        return []


def _cmd_server_dir(cmd: str) -> str | None:
    """从命令行提取 -Dbuddy.serverDir 的值（防 paper-1.20.4 前缀误判 paper-1.20.4-b）。"""
    key = "-Dbuddy.serverDir="
    i = cmd.find(key)
    if i < 0:
        return None
    val = cmd[i + len(key):]
    for sep in (" ", '"', "'"):
        k = val.find(sep)
        if k >= 0:
            val = val[:k]
    return val


def java_running_in_dir(sdir: Path) -> list[int]:
    """同目录 Java 进程检测：warden 起服时把目录写进 -Dbuddy.serverDir，靠命令行反查。
    必须精确相等：否则 %MC_SERVERS%/paper-1.20.4 会把 %MC_SERVERS%/paper-1.20.4-b（25569）误判成同目录。"""
    want = str(sdir).lower().replace("/", "\\").rstrip("\\")
    pids = []
    for pid, cmd in java_pids_with_cmdline():
        got = _cmd_server_dir(cmd)
        if got and got.lower().replace("/", "\\").rstrip("\\") == want:
            pids.append(pid)
    return pids


def build_jvm_args(info: dict, sdir: Path) -> list[str]:
    """JVM profile：Xms/Xmx + Aikar + OOM heap dump + GC 日志（落在服务端目录 gc.log）"""
    args = [f"-Xms{info.get('xms', '2G')}", f"-Xmx{info.get('xmx', '2G')}", *AIKAR_FLAGS,
            "-XX:+HeapDumpOnOutOfMemoryError", f"-XX:HeapDumpPath={sdir}",
            f"-Xlog:gc*:file={sdir / 'gc.log'}:time,uptime,level,tags:filecount=5,filesize=10M",
            f"-Dbuddy.serverDir={sdir}"]
    return args


def rcon_cmd(port: int, cmd: str) -> str:
    sys.path.insert(0, str(ROOT / "orchestrator"))
    import rcon  # noqa: E402
    rc = rcon.Rcon("127.0.0.1", port, "buddy").connect()
    out = rc.command(cmd)
    rc.close()
    return out


def rcon_ok(port: int) -> bool:
    """端口开了不代表服务端就绪（启动中也会监听）——发一条 list 验证"""
    try:
        return "players online" in rcon_cmd(port, "list")
    except Exception:
        return False


def ensure_buddy_op(port: int) -> None:
    """幂等给 Buddy 上 OP（场景 setup 走 bot.chat 注入，没 OP 会被当聊天刷屏踢——disconnect.spam 血证）"""
    try:
        rcon_cmd(port, "op Buddy")
    except Exception:
        pass


def start_server(port: int, min_free_gb: float = DEFAULT_MIN_FREE_GB) -> bool:
    """拉起 :port 的服务端。起服前三道闸：端口 / 同目录 Java 进程 / 可用内存"""
    info = SERVERS[port]
    if not info["dir"]:
        print(f"[warden] :{port} 是 Docker 服务端，请手动拉起（Docker Desktop）")
        return False
    sdir = Path(info["dir"])
    if not sdir.exists():
        print(f"[warden] 服务端目录不存在: {sdir}")
        return False
    if not JAVA.exists():
        print(f"[warden] Java 不存在: {JAVA}")
        return False
    # 闸 1：端口已被占（可能是别人/僵尸服）——不抢端口，fail-fast
    if port_open("127.0.0.1", port):
        print(f"[warden] :{port} 端口已被占用但 RCON 不就绪——疑似僵尸进程，先人工处理")
        return False
    # 闸 2：同目录已有 Java 进程（双开会把世界玩坏）
    pids = java_running_in_dir(sdir)
    if pids:
        print(f"[warden] {sdir} 已有 Java 进程在跑（pid={pids}），拒绝双开")
        return False
    # 闸 3：可用内存预算（chunkgen + 多服并行把机器打到交换分区的血证）
    free = free_mem_gb()
    if 0 <= free < min_free_gb:
        print(f"[warden] 可用内存 {free:.1f}GB < 阈值 {min_free_gb}GB，拒绝起服（--min-free-gb 可覆盖）")
        return False
    jvm_args = build_jvm_args(info, sdir)
    logf = (sdir / "warden-console.log").open("a", encoding="utf-8", errors="replace")
    logf.write(f"\n===== warden 起服 {time.strftime('%F %T')} | {' '.join(jvm_args)} =====\n")
    logf.flush()
    print(f"[warden] 启动 {info['version']} @ :{port}（{sdir}，{info.get('xmx', '2G')}，"
          f"可用 {free:.1f}GB，日志 {sdir/'warden-console.log'}，GC 日志 {sdir/'gc.log'}）")
    subprocess.Popen(
        [str(JAVA), *jvm_args, "-jar", "paper.jar", "nogui"],
        cwd=str(sdir), stdout=logf, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
    )
    return True


def health_snapshot(port: int) -> dict:
    """跑批前的服务端健康快照（seq/parallel 可写到日志旁做 infra 归因）"""
    info = SERVERS.get(port, {})
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "port": port,
        "registered": port in SERVERS,
        "port_open": port_open("127.0.0.1", port),
        "rcon_ok": rcon_ok(info["rcon"]) if info.get("rcon") else None,
        "free_mem_gb": round(free_mem_gb(), 2),
        "version": info.get("version"),
        "dir": info.get("dir"),
    }


def ensure_by_port(port: int, wait_sec: int = 300, quiet: bool = False,
                   allow_unregistered: bool = False,
                   min_free_gb: float = DEFAULT_MIN_FREE_GB,
                   settle_sec: int = 75) -> bool:
    """确保 :port 的服务端可用（探活 → 起服 → 等 RCON 就绪）。True=就绪"""
    if port not in SERVERS:
        if not allow_unregistered:
            print(f"[warden] :{port} 不在注册表（{sorted(SERVERS)}）——默认不当 ready，"
                  f"确认无误请加 --allow-unregistered / allow_unregistered=True")
            return False
        print(f"[warden] :{port} 不在注册表，--allow-unregistered 生效：只做端口探活")
        return port_open("127.0.0.1", port)
    info = SERVERS[port]
    t0 = time.time()
    port_is_open = port_open("127.0.0.1", port)
    if port_is_open and rcon_ok(info["rcon"]):
        ensure_buddy_op(info["rcon"])
        if not quiet:
            print(f"[warden] :{port} ({info['version']}) 已在运行 ✅")
        return True
    # 端口已开但 RCON 未就绪 = 服务端正在启动/卡住：不要 start_server（会因端口/同目录 Java 闸拒绝双开），
    # 进入等待循环；只有端口完全 closed 才允许拉起新 JVM。
    started_here = False
    if not port_is_open:
        if not start_server(port, min_free_gb=min_free_gb):
            return False
        started_here = True
    # 等端口 → 等 RCON。若是本函数拉起的 fresh server，RCON list 通了还要再沉淀 settle_sec：
    # Paper 1.20.4 fresh world 的 placeNewPlayer/fudgeSpawnLocation 会在首个客户端入服时卡 20s+
    # （实测 nr11/nr12：能执行聊天命令但 physicsTick=0）。沉淀后再放 bot 进服。
    ready_at = None
    while time.time() - t0 < wait_sec:
        if port_open("127.0.0.1", port) and rcon_ok(info["rcon"]):
            if started_here:
                if ready_at is None:
                    ready_at = time.time()
                if time.time() - ready_at < settle_sec:
                    time.sleep(5)
                    continue
            ensure_buddy_op(info["rcon"])
            print(f"[warden] :{port} 就绪 ✅（{time.time()-t0:.0f}s）")
            return True
        time.sleep(5)
    print(f"[warden] :{port} 等待 {wait_sec}s 仍未就绪 ❌（查 {info['dir']}/warden-console.log）")
    return False


def status() -> None:
    free = free_mem_gb()
    print(f"可用内存: {free:.1f}GB" if free >= 0 else "可用内存: 查询失败")
    print(f"{'port':>6}  {'version':<9}  {'game':<6}  {'rcon':<5}  {'heap':<7}  note")
    for port, info in sorted(SERVERS.items()):
        game = "UP" if port_open("127.0.0.1", port) else "down"
        rc = rcon_ok(info["rcon"]) if game == "UP" else False
        heap = info.get("xmx", "-")
        print(f"{port:>6}  {info['version']:<9}  {game:<6}  {'ok' if rc else '-':<5}  {heap:<7}  {info['note']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "ensure"])
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--allow-unregistered", action="store_true",
                    help="未注册端口只探活（默认拒绝，防打错端口空烧整批）")
    ap.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB,
                    help=f"起服内存阈值（GB，默认 {DEFAULT_MIN_FREE_GB}）")
    ap.add_argument("--wait-sec", type=int, default=300, help="ensure 等 RCON 就绪的秒数")
    ap.add_argument("--settle-sec", type=int, default=75,
                    help="本函数拉起 fresh server 后，RCON 通了再沉淀多少秒才放 bot 进服")
    a = ap.parse_args()
    if a.cmd == "status":
        status()
        return 0
    if a.all:
        ok = True
        for port in sorted(SERVERS):
            ok = ensure_by_port(port, wait_sec=a.wait_sec, min_free_gb=a.min_free_gb,
                                settle_sec=a.settle_sec) and ok
        return 0 if ok else 2
    if a.port is None:
        ap.error("ensure 需要 --port 或 --all")
    return 0 if ensure_by_port(a.port, wait_sec=a.wait_sec, allow_unregistered=a.allow_unregistered,
                               min_free_gb=a.min_free_gb, settle_sec=a.settle_sec) else 2


if __name__ == "__main__":
    raise SystemExit(main())
