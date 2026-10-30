#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/team.py —— 多陪玩协作：黑板 + 任务认领 + 站位编队 + 说话权

为什么要这一层
--------------
一个陪玩时，"谁来做"不是问题；**三个陪玩时，它会瞬间变成四个问题**：

  1. **谁说话**：三个 bot 同时回应"你好" = 刷屏，人第一反应就是关掉两个。
  2. **谁来做**：三份 `@get oak_log` 同时下发 = 三个人抢同一棵树、互相挡路。
  3. **站在哪**：三个 bot 都想站"玩家旁边 3 格" ⇒ 挤在同一格 ⇒
     准星先打到同伴（`lookingAt: ENTITY`），方块挖不到（2026-10-27 上午真踩过）。
  4. **谁还活着**：一个客户端崩了，其它两个不该跟着乱。

参考
----
· **黑板架构**（Hearsay-II 的经典 Coordination Pattern）：所有 agent 读写同一块共享状态，
  用优先级议程决定谁动手 —— 见 [blackboard pattern](https://github.com/Alpha-Park/genpark-blackboard-architecture-shared-memory-skill)；
· **合同网协议**（FIPA Contract Net：announce → bid → award）：任务先"招标"再"中标"，
  见 [contract net](https://github.com/alphaparkinc/genpark-contract-net-protocol-task-auctioneer-skill)；
· **Project Sid / PIANO**（Altera，1000+ agent 在 Minecraft 里分工）：
  每个 agent 要维持多条并发输出流（说话/移动/动手）的一致性，见
  [arXiv:2411.00114](https://arxiv.org/abs/2411.00114)。
  我们的做法更"工程"：不搞涌现，直接**确定性分工**（谁是队长、谁站哪、谁说第几句），
  因为这是陪玩产品，不是文明模拟。

这一层刻意做得很薄
------------------
**单文件黑板 + 租约（lease）**，不引入消息中间件：

    runs/team/board.json     全员共享（原子写 = 先写 .tmp 再 os.replace）
    {"v":1,
     "members": {"Buddy": {"role":"guard","pos":[..],"busy":"get:oak_log","at":ts}, ...},
     "claims":  {"get:oak_log": {"by":"Buddy","until":ts}},
     "speaker": "Buddy",            # 谁负责回应玩家（其他人心领但不吭声）
     "note":    ""}

三条硬规则（都来自血证，不是审美）：
  · **说话权**只有一个（默认按编号最小且在线的那个）；其余人**静默**但照常干活。
  · **认领过的活**别人不碰；租约到期自动放行（防"人崩了任务永远占着"）。
  · **站位**按编号分槽（环形偏移），并保证同伴间 ≥1.6 格、绝不站到玩家那格里。
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOARD = ROOT / "runs" / "team" / "board.json"

HEARTBEAT_TTL = 25.0     # 超过这么久没心跳 ⇒ 视为离线
CLAIM_TTL = 120.0        # 一个长活最多占这么久（跟 JOB_BUDGET 同量级）
MIN_SEP = 1.6            # 同伴之间的最小间距（格）


def _idx(name: str) -> int:
    """Buddy=1 / Buddy2=2 / Buddy3=3 —— 编队与说话权都按它排。"""
    m = re.search(r"(\d+)$", str(name or ""))
    return int(m.group(1)) if m else 1


class Team:
    """一个陪玩在队伍里的视角。没有队伍时（单人）所有判断都退化成"当然是我"。"""

    def __init__(self, path: Path | str | None = None, me: str = "Buddy",
                 role: str = "playmate", enabled: bool = True, log=None):
        self.path = Path(path) if path else BOARD
        self.me = me
        self.role = role
        self.enabled = enabled
        self.log = log or (lambda o: None)
        self.last: dict = {"v": 1, "members": {}, "claims": {}, "speaker": me, "note": ""}
        self._last_write = 0.0
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── 读写黑板（原子 + 容错） ─────────────────────────────────────────────
    def read(self) -> dict:
        if not self.enabled:
            return self.last
        try:
            raw = self.path.read_text(encoding="utf-8")
            d = json.loads(raw)
            if isinstance(d, dict):
                self.last = {"v": 1, "members": d.get("members") or {},
                             "claims": d.get("claims") or {},
                             "speaker": d.get("speaker") or self.me,
                             "note": d.get("note") or ""}
        except Exception:
            pass
        return self.last

    def write(self, st: dict) -> None:
        if not self.enabled:
            self.last = st
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)          # 原子替换：读的人永远看到完整文件
            self._last_write = time.time()
            self.last = st
        except Exception as e:
            self.log({"ev": "team_write_error", "err": f"{type(e).__name__}: {e}"[:120]})

    # ── 心跳 ───────────────────────────────────────────────────────────────
    def beat(self, *, pos=None, busy: str = "", role: str | None = None) -> dict:
        st = self.read()
        st["members"][self.me] = {
            "role": role or self.role,
            "pos": [round(v, 2) for v in pos] if pos else None,
            "busy": busy[:40],
            "at": round(time.time(), 2),
        }
        # 顺手清理：离线成员、过期租约
        now = time.time()
        st["members"] = {k: v for k, v in st["members"].items()
                         if now - float(v.get("at") or 0) < HEARTBEAT_TTL}
        st["claims"] = {k: v for k, v in st["claims"].items()
                        if float(v.get("until") or 0) > now}
        self.write(st)
        return st

    def peers(self) -> list[dict]:
        st = self.read()
        out = []
        for name, m in (st.get("members") or {}).items():
            if name == self.me:
                continue
            out.append({"name": name, **m})
        out.sort(key=lambda m: _idx(m["name"]))
        return out

    def alive_members(self) -> list[str]:
        st = self.read()
        names = [n for n, m in (st.get("members") or {}).items()
                 if time.time() - float(m.get("at") or 0) < HEARTBEAT_TTL]
        return sorted(names, key=_idx)

    # ── 说话权：只让一个人回应玩家 ─────────────────────────────────────────
    def set_speaker(self, name: str = "") -> None:
        st = self.read()
        st["speaker"] = name or self.me
        self.write(st)

    def may_speak(self) -> bool:
        """我是不是"对外说话"的那个人。单人时永远 True。"""
        if not self.enabled:
            return True
        st = self.read()
        alive = self.alive_members()
        if not alive:
            return True
        want = st.get("speaker") or ""
        if want in alive:
            return want == self.me
        return alive[0] == self.me          # 没有指定 ⇒ 编号最小的那个负责说话

    # ── 任务认领（合同网的"简化版"：先到先得 + 租约） ───────────────────────
    def claim(self, key: str, ttl: float = CLAIM_TTL) -> bool:
        if not self.enabled:
            return True
        st = self.read()
        now = time.time()
        cur = (st.get("claims") or {}).get(key)
        if cur and float(cur.get("until") or 0) > now and cur.get("by") != self.me:
            return False                         # 别人正干着
        st.setdefault("claims", {})[key] = {"by": self.me, "until": round(now + ttl, 1)}
        self.write(st)
        self.log({"ev": "claim", "key": key, "ok": True})
        return True

    def release(self, key: str) -> None:
        if not self.enabled:
            return
        st = self.read()
        cur = (st.get("claims") or {}).get(key)
        if cur and cur.get("by") == self.me:
            st["claims"].pop(key, None)
            self.write(st)

    def owner(self, key: str) -> str | None:
        st = self.read()
        cur = (st.get("claims") or {}).get(key)
        if cur and float(cur.get("until") or 0) > time.time():
            return str(cur.get("by") or "") or None
        return None

    # ── 编队：站位槽（环上按编号分，谁也不抢谁的格） ─────────────────────────
    def my_index(self) -> int:
        alive = self.alive_members() or [self.me]
        return alive.index(self.me) if self.me in alive else 0

    def my_radius(self, radius: float = 2.9) -> float:
        """队长站近一点（挡刀），其余人站远一点（不挡镜头、不互相挡）。"""
        return 2.2 if self.my_index() == 0 else max(radius, 3.1)

    def my_angle(self, player_pos) -> float:
        """我在编队环上的方位角（弧度）。两个陪玩都在玩家同一侧时，必须靠它分开 ——
        否则两人的"停靠点"会算到同一格上（实测 0.92 格，几乎重合）。"""
        n = max(1, len(self.alive_members() or [self.me]))
        return math.radians(-90 + 360.0 * self.my_index() / n)

    def slot(self, player_pos, *, n: int | None = None, radius: float = 2.9) -> tuple:
        """我该站玩家哪一侧。

        · 环上按编号排序分配角度 ⇒ 三个陪玩不会挤在同一格；
        · 队长（编号最小）站 2.2 格、其余 3.1 格 ⇒ 前面有人挡刀，后面的人不挡镜头；
        · **绝不返回玩家所在的那一格**（那会让准星先打到人，方块就挖不到了）。
        """
        px, py, pz = [float(v) for v in player_pos]
        alive = self.alive_members() or [self.me]
        n = n or max(1, len(alive))
        i = alive.index(self.me) if self.me in alive else 0
        r = 2.2 if i == 0 else max(radius, 3.1)
        ang = self.my_angle(player_pos) if n == len(alive) else \
            math.radians(-90 + 360.0 * i / max(1, n))
        x = px + math.cos(ang) * r
        z = pz + math.sin(ang) * r
        if abs(x - px) < 0.9 and abs(z - pz) < 0.9:
            x = px + r
        return (x, py, z)

    def min_sep_ok(self, cell, *, ignore_self: bool = True) -> bool:
        """这个格子离同伴够不够远（不够远就别站，免得互相挡）。"""
        if not self.enabled:
            return True
        try:
            cx, cy, cz = [float(v) for v in cell]
        except Exception:
            return True
        for p in self.peers():
            pos = p.get("pos")
            if not pos:
                continue
            try:
                d = math.dist((cx, cy, cz), [float(v) for v in pos])
            except Exception:
                continue
            if d < MIN_SEP:
                return False
        return True

    # ── 汇总（给面板看） ───────────────────────────────────────────────────
    def snapshot(self) -> dict:
        st = self.read()
        now = time.time()
        return {
            "me": self.me, "role": self.role, "speaker": st.get("speaker"),
            "may_speak": self.may_speak(),
            "members": [{"name": n, **m,
                         "alive": now - float(m.get("at") or 0) < HEARTBEAT_TTL}
                        for n, m in sorted((st.get("members") or {}).items(),
                                           key=lambda kv: _idx(kv[0]))],
            "claims": {k: v for k, v in (st.get("claims") or {}).items()
                       if float(v.get("until") or 0) > now},
        }
