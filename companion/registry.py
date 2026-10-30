#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/registry.py —— 能力注册表：把 AltoClef 的**全部**能力编成可被指挥的名词

设计原则（operator 2026-10-26：「保留 altoclef 的所有能力」）：
  · **不裁剪**。66 个 MCP 工具 + 全部 `@命令` 都在册，`Action` 只是给它们套一层元数据，
    不改变它们的用法。想用任何一个，都可以：
        registry.invoke("invoke_tool", {"tool": "<任意工具名>", "args": {...}})
        registry.invoke("run_command", {"cmd": "@get oak_log 3"})
  · **元数据只用于"选"和"防护"**，不用于"禁用"：risk 高不代表不能做，只代表需要更高优先级
    或更强的前置检查。
  · `@命令` 表来自 AltoClef 自己的 `@help`（2026-10-26 实测抓取），不是猜的。

分三类：
  tool      —— MCP 工具（66 个，运行时自动发现，schema 原样保留）
  command   —— AltoClef 的聊天命令（40 条，`@xxx`，通过 ExecuteCommand 下发）
  internal  —— 指挥层自己的动作（observe / wait / stop），不发给身体
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .body import Body, ToolError, truthy

# ── AltoClef 原生命令表（@help 实测抓取，2026-10-26） ────────────────────────
# risk: none/low/mid/high  —— high = 会长时间占用身体或改变世界
COMMANDS: dict[str, dict[str, Any]] = {
    "goto":        {"args": "x y z",            "risk": "mid",  "desc": "走到坐标（AltoClef 自己的任务链，能破障搭桥）"},
    "follow":      {"args": "[nick]",           "risk": "mid",  "desc": "跟着某人"},
    "alive":       {"args": "watch|near|roam|idle <nicks> [sec]", "risk": "high", "desc": "像玩家一样活着（会自己找事做，可能跑远）"},
    "idle":        {"args": "",                 "risk": "none", "desc": "站着别动"},
    "stop":        {"args": "",                 "risk": "none", "desc": "停掉当前任务"},
    "unpause":     {"args": "",                 "risk": "none", "desc": "解除暂停"},
    "status":      {"args": "",                 "risk": "none", "desc": "当前任务状态"},
    "coords":      {"args": "",                 "risk": "none", "desc": "报坐标"},

    "mineblock":   {"args": "",                 "risk": "low",  "desc": "破准星下那块（先 lookAt）"},
    "scan":        {"args": "<block>",          "risk": "low",  "desc": "找最近的某方块"},
    "check_block": {"args": "<block>",          "risk": "none", "desc": "视野里有没有某方块"},
    "get":         {"args": "<item> [n]",       "risk": "high", "desc": "去弄到某物品（会自己规划采集/合成）"},
    "food":        {"args": "[n]",              "risk": "high", "desc": "去弄吃的"},
    "meat":        {"args": "[n]",              "risk": "high", "desc": "去弄肉"},
    "selfcare":    {"args": "",                 "risk": "high", "desc": "自己搞装备（可能跑很远）"},
    "equip":       {"args": "[item]",           "risk": "low",  "desc": "穿/拿装备"},
    "inventory":   {"args": "[item]",           "risk": "none", "desc": "看背包/数某物"},
    "stash":       {"args": "[items...]",       "risk": "mid",  "desc": "把东西存进箱子"},

    "place":       {"args": "<block>",          "risk": "mid",  "desc": "在附近放一个方块"},
    "build":       {"args": "grave|sign|golem|bed|portal", "risk": "high", "desc": "盖结构"},
    "grave":       {"args": "<任意文字>",        "risk": "low",  "desc": "立一块带字的墓碑（唯一能把文字写到世界里的方式）"},
    "coverwithblocks": {"args": "",             "risk": "mid",  "desc": "用方块盖住岩浆"},

    "hero":        {"args": "",                 "risk": "mid",  "desc": "清掉所有敌对生物"},
    "combat":      {"args": "",                 "risk": "high", "desc": "战斗模式"},
    "pursue":      {"args": "<nick>",           "risk": "high", "desc": "追某人"},
    "avoid":       {"args": "<nick>",           "risk": "low",  "desc": "躲某人"},

    "gesture":     {"args": "",                 "risk": "none", "desc": "做手势"},
    "mirror":      {"args": "<nick> [rec|sec]", "risk": "mid",  "desc": "录/放某个玩家的动作"},
    "locate_structure": {"args": "<structure>", "risk": "low",  "desc": "找结构"},
    "set":         {"args": "<name> <value>",   "risk": "mid",  "desc": "改设置"},
    "reload_settings": {"args": "",             "risk": "low",  "desc": "重载设置"},
    "timeout":     {"args": "<sec> <cmd...>",   "risk": "low",  "desc": "带超时地跑一条命令"},
    "t":           {"args": "<cmd...>",         "risk": "low",  "desc": "同上，默认 60 秒"},
    "custom2":     {"args": "",                 "risk": "high", "desc": "跑配置里的自定义任务序列"},
    "connect":     {"args": "<ip>[:port]",      "risk": "high", "desc": "换服务器"},
    "help":        {"args": "",                 "risk": "none", "desc": "列命令"},
}

# 指挥层内部动作（不发给身体）
INTERNAL = {
    "observe": {"desc": "读一帧世界（自己 + 玩家 + 朝向 + 导航状态）"},
    "wait":    {"desc": "什么都不做，等下一 tick"},
}


@dataclass
class ActionResult:
    ok: bool
    data: Any = None
    err: str = ""
    kind: str = ""
    name: str = ""
    args: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {"ok": self.ok, "kind": self.kind, "name": self.name}
        if self.args:
            d["args"] = self.args
        if self.err:
            d["err"] = self.err[:200]
        return d


class Registry:
    """所有能力的总入口。上层（规则 / 将来的 AI）只通过 `invoke()` 说话。"""

    def __init__(self, body: Body, dry: bool = False):
        self.body = body
        self.dry = dry
        self.tools: dict[str, dict] = {}
        try:
            for t in body.tools():
                self.tools[t["name"]] = t
        except Exception:
            pass                      # 身体还没起也没关系，命令仍可用

    # ── 目录 ────────────────────────────────────────────────────────────────
    def catalog(self) -> dict:
        """给 AI/人看的全量能力清单（这一层就是"保留所有能力"的证据）。"""
        return {
            "tools": {n: {"desc": (t.get("description") or "").split("\n")[0][:110],
                          "schema": t.get("inputSchema")}
                      for n, t in sorted(self.tools.items())},
            "commands": {k: {"args": v["args"], "risk": v["risk"], "desc": v["desc"]}
                         for k, v in sorted(COMMANDS.items())},
            "internal": INTERNAL,
        }

    def has(self, name: str) -> bool:
        return name in self.tools or name in COMMANDS

    # ── 执行 ────────────────────────────────────────────────────────────────
    def invoke(self, action: str, args: dict | None = None) -> ActionResult:
        """唯一的执行口。action 是：
             · 任意 MCP 工具名          （66 个，原样透传）
             · "run_command" + {"cmd": "@xxx"}  （原生命令，风险档位见 COMMANDS）
             · "wait" / "observe"
        """
        args = args or {}
        if action == "wait":
            return ActionResult(True, kind="internal", name="wait")
        if action == "observe":
            return ActionResult(True, kind="internal", name="observe")

        if action == "run_command":
            cmd = str(args.get("cmd") or "")
            if not cmd.startswith("@"):
                return ActionResult(False, err="run_command 只接 @ 开头的 AltoClef 命令",
                                    kind="command", name=cmd)
            if self.dry:
                return ActionResult(True, {"dry": True}, kind="command", name=cmd, args=args)
            try:
                return ActionResult(True, self.body.call("ExecuteCommand", {"cmd": cmd}, 20),
                                    kind="command", name=cmd, args=args)
            except Exception as e:
                return ActionResult(False, err=f"{type(e).__name__}: {e}",
                                    kind="command", name=cmd, args=args)

        # 默认当作 MCP 工具
        if self.tools and action not in self.tools:
            return ActionResult(False, err=f"未知能力 {action}", kind="tool", name=action)
        if self.dry:
            return ActionResult(True, {"dry": True}, kind="tool", name=action, args=args)
        try:
            return ActionResult(True, self.body.call(action, args, 25),
                                kind="tool", name=action, args=args)
        except ToolError as e:
            return ActionResult(False, err=str(e), kind="tool", name=action, args=args)
        except Exception as e:
            return ActionResult(False, err=f"{type(e).__name__}: {e}",
                                kind="tool", name=action, args=args)

    # ── 便利：读世界（不属于"动作"，单独拎出来） ─────────────────────────────
    def pos(self, o: dict):
        try:
            a = [float(x) for x in str(o.get("pos")).split(",")]
            return (a[0], a[1], a[2]) if len(a) == 3 else None
        except Exception:
            return None

    def block(self, x, y, z) -> dict:
        r = self.invoke("getBlockAt", {"x": int(x), "y": int(y), "z": int(z)})
        return r.data if r.ok and isinstance(r.data, dict) else {}

    def is_air(self, x, y, z) -> bool:
        return truthy(self.block(x, y, z).get("is_air"))
