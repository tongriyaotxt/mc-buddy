#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/install_jar_now.py —— 等客户端把旧 jar 放开，再把新的换上去（可挂后台）

为什么要有它：`apply_unionclef_jar.py` 一次性替换会撞上 **WinError 32**（旧 jar 正被运行中的
客户端 mmap 住）。今天要装的这版把 MCP 服务器补到了规范版（annotations / resources /
协议协商 / 不再双重编码），**必须重启客户端**才生效，而"真人客户端"还在跑。

用法：
  python -X utf8 tools/install_jar_now.py --jar <新 jar> [--wait-min 30]
每 5 秒试一次，装好就写 runs/jar_install.json 并退出；超时就以 1 退出。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TARGETS = [
    Path(r"%MC_HOME%"),
    Path(r"%MC_HOME%"),
]
PATTERN = "unionclef-*.jar"


def try_install(jar: Path) -> tuple[bool, str]:
    for d in TARGETS:
        if not d.exists():
            continue
        for old in d.glob(PATTERN):
            try:
                old.unlink()
            except PermissionError:
                return False, f"locked: {old.name} @ {d}"
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"
        try:
            shutil.copy2(jar, d / jar.name)
        except Exception as e:
            return False, f"copy: {type(e).__name__}: {e}"
    return True, "installed"


def default_jar() -> str:
    """默认装**最新发布**的那个，而不是写死某个版本号。

    2026-10-30 血证：这里原先是 `...buddy3.jar` 写死。发布 buddy15 之后照原样跑
    `install_jar_now.py`，它把 **buddy3** 拷进了两个 mods 目录 —— 客户端起的还是四天前的
    代码，而发布流水线、latest.json、校验全说 buddy15。默认值必须跟着发布指针走。
    """
    ptr = ROOT / "_releases" / "latest.json"
    if ptr.exists():
        try:
            info = json.loads(ptr.read_text(encoding="utf-8"))
            name = info.get("file") or ""
            cand = ROOT / "_releases" / name
            if cand.exists():
                return str(cand)
        except Exception:
            pass
    return str(ROOT / "_refs" / "unionclef-0.95.63" / "versions" / "1.21.11"
               / "build" / "libs" / "unionclef-1.21.11-0.95.63-local.jar")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jar", default=default_jar(),
                    help="默认 = _releases/latest.json 指向的那一个")
    ap.add_argument("--wait-min", type=float, default=30.0)
    a = ap.parse_args()
    jar = Path(a.jar)
    if not jar.exists():
        print(f"jar 不存在: {jar}")
        return 2
    out = ROOT / "runs" / "jar_install.json"
    t0 = time.time()
    last = ""
    while time.time() - t0 < a.wait_min * 60:
        ok, msg = try_install(jar)
        if ok:
            info = {"ok": True, "jar": str(jar), "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "waited_s": round(time.time() - t0, 1), "targets": [str(t) for t in TARGETS]}
            out.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
            print(json.dumps(info, ensure_ascii=False))
            return 0
        if msg != last:
            print(f"[{time.strftime('%H:%M:%S')}] {msg} —— 等客户端关掉（每 5 秒重试）", flush=True)
            last = msg
        time.sleep(5)
    out.write_text(json.dumps({"ok": False, "why": last,
                               "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print("超时，没装上：" + last)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
