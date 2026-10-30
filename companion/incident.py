#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/incident.py —— 出事时自动留证（beta 必备）

为什么单独一个模块：`commander.step()` 里的异常现在只写一行 `error` 到 trace，
**人拿不到现场**。这个模块把"崩的那一刻"打包成一个目录：

    runs/crash/<ts>/
        error.txt      异常类型 + 栈（截断）
        trace_tail.jsonl  最近一局的 trace 尾 200 行
        gate.json      最近一次回归闸结论
        env.json       python/端口/内存/进程
        README.txt     一句话：把整个目录发给维护者

调用点（已接）：commander 的 step 异常 / run 循环异常 / 语音线程异常。
不抛异常（自己出事就静默），绝不因为留证再崩一次。
"""
from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _tail(p: Path, n: int = 200) -> list[str]:
    try:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []


def capture(exc: BaseException, *, where: str = "", extra: dict | None = None) -> Path | None:
    try:
        ts = time.strftime("%Y%m%d-%H%M%S")
        d = ROOT / "runs" / "crash" / ts
        d.mkdir(parents=True, exist_ok=True)
        (d / "error.txt").write_text(
            f"where: {where}\ntype : {type(exc).__name__}\ntime : {ts}\n\n"
            + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[:8000],
            encoding="utf-8")
        runs = ROOT / "runs" / "commander"
        dirs = [p for p in runs.glob("*") if p.is_dir()] if runs.exists() else []
        if dirs:
            latest = max(dirs, key=lambda p: p.stat().st_mtime)
            tail = _tail(latest / "trace.jsonl")
            (d / "trace_tail.jsonl").write_text("\n".join(tail), encoding="utf-8")
        g = ROOT / "runs" / "gate" / "last.json"
        if g.exists():
            (d / "gate.json").write_text(g.read_text(encoding="utf-8", errors="replace"),
                                         encoding="utf-8")
        env = {"ts": ts, "cwd": str(ROOT), "pid": os.getpid(),
               "python": os.sys.version.split()[0]}
        try:
            import psutil  # 可选
            env["rss_mb"] = round(psutil.Process().memory_info().rss / 1e6, 1)
        except Exception:
            pass
        if extra:
            env["extra"] = extra
        (d / "env.json").write_text(json.dumps(env, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        (d / "README.txt").write_text(
            "把这个目录整个发给维护者（不含密钥）。\n"
            f"错误：{type(exc).__name__}: {exc}\n位置：{where}\n", encoding="utf-8")
        # 支持闭环（opt-in）：开了才打包 + 上传；没开就只留本地
        try:
            import sys as _sys
            _sys.path.insert(0, str(ROOT / "launcher"))
            import core as L
            if L.support_cfg().get("enabled") and L.support_cfg().get("auto_crash"):
                bz = L.bundle(d)
                if bz.get("ok"):
                    up = L.upload_diag(bz["zip"])
                    (d / "upload.json").write_text(json.dumps(up, ensure_ascii=False, indent=1),
                                                   encoding="utf-8")
        except Exception:
            pass
        return d
    except Exception:
        return None
