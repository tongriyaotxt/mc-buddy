#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/chat_in.py —— 听人说话：游戏聊天 → 意图 → 请求总线

零聊天取消之后，**玩家的聊天框就是最自然的下令方式**（他本来就在那儿打字）。
但"能读"和"该听"是两件事：

  · 聊天里什么都有：系统提示、别的玩家的闲聊、我们自己的 mod 日志（`[Alto Clef] …`）。
  · 所以这里做三道过滤：**是不是人说的 → 是不是对我说的 → 说出来的是不是一件可执行的事**。
  · 三道都过不了的，**不执行也不追问**（追问会把聊天框变成审讯室）。

实现要点
--------
1. **增量**：`getRecentChat(n)` 每次都返回"最近 n 条"，这里用**最长公共后缀**和上一帧对齐，
   只取新行。用文本对齐而不是行号，是因为 mod 的 ring buffer 会滚动。
2. **去重**：同一句原文在 `dedupe_window` 内只执行一次（玩家会连发两遍）。
3. **可回溯**：每条被采纳的输入都带原文，进 `bus` 后一路带到 `trace.jsonl`。
"""
from __future__ import annotations

import re
import time

from . import nlu

SELF_NAMES = ("buddy", "小跟班", "跟班", "助手")
NOISE = ("[Alto Clef]", "[Tungsten]", "[System]", "加入了游戏", "退出了游戏",
         "joined the game", "left the game")


def _plain(line: str) -> str:
    t = re.sub(r"§.", "", str(line or ""))
    return t.replace("\u00a7", "").strip()


def _speaker(line: str) -> tuple[str, str]:
    """解析 `<名字> 内容` / `[名字] 内容` / `名字 » 内容`，返回 (名字, 内容)。"""
    t = _plain(line)
    m = re.match(r"^<([^>]{1,24})>\s*(.*)$", t)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^\[([^\]]{1,24})\]\s*(.*)$", t)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^([A-Za-z0-9_]{2,16})\s*[»>:]\s*(.*)$", t)
    if m:
        return m.group(1), m.group(2)
    return "", t


def _common_suffix(a: list[str], b: list[str]) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


class ChatIn:
    """轮询玩家聊天，把"对我说的人话"变成总线请求。"""

    def __init__(self, reg, bus, *, player: str = "Xiaorun", me: str = "Buddy",
                 min_conf: float = 0.5, ask_conf: float = 0.3,
                 dedupe_window: float = 20.0, log=None, gate=None):
        self.reg = reg
        self.bus = bus
        self.player = player
        self.me = me
        self.min_conf = min_conf
        self.ask_conf = ask_conf
        self.dedupe_window = dedupe_window
        self.log = log or (lambda obj: None)
        # 多陪玩：听见了不等于该动手（只让"说话权持有者"执行，其余人静默旁听）
        self.gate = gate
        self.seen: list[str] = []
        self.recent: dict[str, float] = {}
        self.heard = 0
        self.accepted = 0
        self.last_poll = 0.0

    # ── 读 ─────────────────────────────────────────────────────────────────
    def _fetch(self) -> list[str]:
        r = self.reg.invoke("getRecentChat", {"n": 40})
        d = r.data
        if isinstance(d, list):
            return [str(x) for x in d]
        if isinstance(d, dict):
            for k in ("_raw", "lines", "chat"):
                if isinstance(d.get(k), list):
                    return [str(x) for x in d[k]]
        return []

    # ── 主入口 ─────────────────────────────────────────────────────────────
    def poll(self, *, force: bool = False, now: float | None = None) -> list[dict]:
        now = now or time.time()
        if not force and now - self.last_poll < 0.4:      # 2.5 Hz 足够，别再快
            return []
        self.last_poll = now
        cur = self._fetch()
        if not cur:
            return []
        n = _common_suffix(self.seen, cur)
        new_lines = cur[: len(cur) - n]          # 只取"上一帧之后新增的那些行"
        self.seen = cur[-60:]

        out: list[dict] = []
        for line in new_lines:
            text = _plain(line)
            if not text or any(x in text for x in NOISE):
                continue
            who, said = _speaker(line)
            if not who:
                continue
            # 自己的话不执行（否则会自问自答）
            if who.lower() in SELF_NAMES or who.lower() == self.me.lower():
                continue
            if self.player and who.lower() != self.player.lower():
                continue
            said = said.strip()
            if not said:
                continue
            self.heard += 1
            if not nlu.is_addressed(said, me=self.me, player=self.player):
                continue
            key = said.lower()[:60]
            if now - self.recent.get(key, 0.0) < self.dedupe_window:
                continue
            self.recent[key] = now

            if self.gate is not None:
                try:
                    if not self.gate():
                        self.log({"ev": "heard_defer", "who": who, "said": said})
                        continue
                except Exception:
                    pass

            parsed = nlu.parse(said, me=self.me, player=self.player)
            conf = float(parsed.get("conf", 0.0))
            if conf < self.ask_conf:
                continue                      # 听着像闲聊，不接
            parsed["raw"] = said
            parsed["from"] = who
            if conf < self.min_conf:
                parsed["kind"] = "ask"
                parsed["what"] = "clarify"
            req = self.bus.push(parsed.pop("kind"), **parsed)
            self.accepted += 1
            self.log({"ev": "heard", "who": who, "said": said, "req": req["id"],
                      "kind": req["kind"], "conf": conf})
            out.append(req)
        return out

    def stats(self) -> dict:
        return {"heard": self.heard, "accepted": self.accepted,
                "queued": len(self.bus)}
