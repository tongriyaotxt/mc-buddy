#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/body_ctl.py —— 身体进程控制（重启 / 状态），给指挥层用的那半边

`tools/buddy_body.py` 是同一套逻辑的 CLI 形态；这里只是把它包成可 import 的函数，
免得指挥层去 spawn 一个 python 进程。两边共用同一份实现。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TOOLS = _ROOT / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import buddy_body as _bb  # noqa: E402


def status() -> dict:
    return _bb.status()


def restart(log=None) -> dict:
    """杀掉 Buddy 客户端 → --no-connect 重拉 → 等 MCP → 进服。

    实测：MCP 恢复约 40~60 秒；进服后位置与死亡前一致（playerdata 未重置）。

    ⚠️ 2026-10-29：重启前必须把目标服地址同步进 buddy_body（`BUDDY_SERVER`）。
    以前它写死 25565，换到 25566 之后健康阶梯每重启一次就连错一次 —— 客户端永远进不去，
    从外面看就是"卡死、不出活"。
    """
    import os
    _bb.SERVER = os.environ.get("BUDDY_SERVER", _bb.SERVER)
    if log:
        log({"ev": "restart_begin", "pid": _bb.find_pid(), "server": _bb.SERVER})
    snap = _bb.restart(verbose=False)
    if log:
        log({"ev": "restart_done", **{k: snap.get(k) for k in
                                      ("restart_s", "mcp", "in_game", "pos", "pid")}})
    return snap


def kill() -> bool:
    pid = _bb.find_pid()
    if pid:
        _bb.kill(pid)
        return True
    return False


def ensure(log=None) -> dict:
    """能不动就不动：MCP 不通 → 重启；在游戏外 → 进服。"""
    s = status()
    if s.get("mcp") and s.get("in_game"):
        return s
    if s.get("mcp"):
        _bb.ensure_ingame(_bb.body(), 90)
        return status()
    return restart(log=log)
