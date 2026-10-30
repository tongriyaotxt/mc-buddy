#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/long_run.py —— 长跑取证（30 分钟稳定性的"仪器"）

为什么要有它：`session_test` 是"发请求看它做不做"，这个是**只跑不自扰**——
陪玩正常跟着玩家跑，我们每 30 秒采一次样（活着/内存/端口/事件数），跑完自动算：

    · 存活率（进程一直在？）
    · 内存曲线（有没有泄漏：末值 / 峰值 / 斜率）
    · 事故数（trace 里的 error / incident / body_restart）
    · 终局判据五数（调 interaction_score 复算）
    · 任务空档（调 job_gap 复算）

用法：
    python -X utf8 tools/long_run.py --minutes 30
    python -X utf8 tools/long_run.py --minutes 30 --server 127.0.0.1:25566 --voice
    python -X utf8 tools/long_run.py --minutes 2 --no-ai      # 冒烟（不花 token）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = r"%BUDDY_CLIENT%"


def rss_of(pid: int) -> float | None:
    try:
        import psutil  # type: ignore
        return round(psutil.Process(pid).memory_info().rss / 1e6, 1)
    except Exception:
        pass
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10).stdout
        parts = [x.strip('"') for x in out.strip().split('","')]
        if len(parts) >= 5:
            return round(int(parts[4].replace(",", "").replace(" K", "")) / 1024.0, 1)
    except Exception:
        return None


def count_events(path: Path) -> dict:
    n = {"total": 0, "error": 0, "job_done": 0, "job_cancel": 0, "body_restart": 0,
         "incident": 0, "death": 0}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("{"):
                continue
            n["total"] += 1
            for k in ("error", "job_done", "job_cancel", "body_restart", "death"):
                if f'"ev": "{k}"' in line:
                    n[k] += 1
            if '"ev": "incident_saved"' in line or '"ev": "body_restart"' in line:
                n["incident"] += 1
    except Exception:
        pass
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--port", type=int, default=25351)
    ap.add_argument("--player", default="Xiaorun")
    ap.add_argument("--server", default="127.0.0.1:25566")
    ap.add_argument("--voice", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--sample", type=float, default=30.0)
    a = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "runs" / "longrun" / ts
    out.mkdir(parents=True, exist_ok=True)
    log = out / "commander.log"
    cmd = [sys.executable, "-X", "utf8", "-m", "companion.commander",
           "--port", str(a.port), "--settings", SETTINGS, "--player", a.player,
           "--seconds", str(int(a.minutes * 60)), "--hz", "6",
           "--server", a.server]
    if a.voice:
        cmd.append("--voice")
    if a.no_ai:
        cmd.append("--no-ai")
    print(f"[long] 起指挥层：{' '.join(cmd[2:])}")
    fh = log.open("w", encoding="utf-8")
    p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT)

    samples, t0 = [], time.time()
    try:
        while p.poll() is None and time.time() - t0 < a.minutes * 60 + 120:
            samples.append({"t": round(time.time() - t0, 1), "rss_mb": rss_of(p.pid)})
            time.sleep(a.sample)
    except KeyboardInterrupt:
        p.terminate()
    finally:
        try:
            p.wait(timeout=30)
        except Exception:
            p.kill()
        fh.close()

    # 找这一局的 trace
    dirs = [d for d in (ROOT / "runs" / "commander").glob("*") if d.is_dir()]
    latest = max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None
    ev = count_events(latest / "trace.jsonl") if latest else {}

    rss = [s["rss_mb"] for s in samples if s.get("rss_mb")]
    rep = {"ts": ts, "minutes": a.minutes, "rc": p.returncode,
           "alive_ratio": round(sum(1 for s in samples if s.get("rss_mb")) / max(1, len(samples)), 3),
           "rss_mb": {"first": rss[0] if rss else None, "last": rss[-1] if rss else None,
                      "max": max(rss) if rss else None,
                      "growth_per_min": (round((rss[-1] - rss[0]) / a.minutes, 2)
                                         if len(rss) > 1 and a.minutes else None)},
           "events": ev, "trace": str(latest / "trace.jsonl") if latest else "",
           "samples": samples}

    # 收尾复算：终局判据 + 任务空档
    for tool, args in (("interaction_score.py", ["--run", str(latest)] if latest else []),
                       ("job_gap.py", ["--run", str(latest)] if latest else [])):
        try:
            r = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "tools" / tool)] + args,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120)
            rep[tool] = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:]
        except Exception as e:
            rep[tool] = [f"{type(e).__name__}: {e}"]

    (out / "summary.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print(f"[long] 时长 {a.minutes} 分  存活率 {rep['alive_ratio']}  "
          f"内存 {rep['rss_mb']}")
    print(f"[long] 事件 {ev}")
    for k in ("interaction_score.py", "job_gap.py"):
        for line in rep.get(k, []):
            print(f"[long] {k}: {line}")
    print(f"[long] 产物：{out}")
    return 0 if p.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
