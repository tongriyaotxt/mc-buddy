#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/memory.py —— **人物记忆**（不是场景记忆）

operator 2026-10-26：「不要场景记忆，要人物记忆（具体你研究）」。

什么叫人物记忆
--------------
记的是**这个人**：他怎么说话、喜欢什么、怕什么、答应过他什么、一起干过什么、熟到什么程度。
**不记世界**：不存坐标、不存"哪里有树"、不存背包快照、不存当前任务。
理由（这是设计决定，不是洁癖）：

  · 场景记忆**会过期**，而且是**静默**过期 —— 你记着"东边有铁矿"，三天后再去那里已经被自己挖空了，
    陪玩就会理直气壮地带着人往空矿洞走。过期数据比没有数据更糟。
  · 场景信息**本来就能现读**（`getBlockAt`/`scanBlocks`/`getEntities` 都是一次调用的事），
    存下来只会和现实打架。
  · 人物信息**读不到**：他喜欢被叫"老板"还是被叫名字、他讨厌被跟着、他上次让你做的事做没做 ——
    这些只能记。

四条写入规则
------------
1. **事件进，结论出**：上层只调 `note_event(kind, **kw)`，特征值由这里算，不让调用方拼分数。
2. **一切都会衰减**：`traits` 带 `last`，半衰期 `HALF_LIFE_DAYS`，久不见面就退回中性。
3. **历史是环**：`history` 只留最近 `HISTORY_MAX` 条，老的扔掉（记忆要淡）。
4. **承诺不衰减**：答应过的事（`promises`）只会被"完成/取消"，不会因为时间被忘掉。

存储
----
`runs/memory/<player>.json`，单文件、可读、可手改、可 diff。没有数据库依赖 ——
一个人一份档案，几十 KB，最坏情况直接 `cat` 就能看懂。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEM_DIR = ROOT / "runs" / "memory"

HALF_LIFE_DAYS = 14.0      # 特征权重半衰期
HISTORY_MAX = 200          # 历史环长度
FORBIDDEN_KEYS = (         # 场景记忆的指纹：出现这些键就拒绝落盘（防止悄悄变成场景记忆）
    "pos", "position", "coords", "x", "y", "z", "block", "blocks",
    "inventory", "biome", "chunk", "seed", "dimension",
)

# 事件 → 会推动哪些特征
TRAIT_OF_EVENT = {
    "gave_item": "likes_gifts",
    "asked_get": "asks_for_items",
    "asked_mine": "likes_mining",
    "asked_follow": "likes_company",
    "asked_build": "likes_building",
    "asked_hero": "likes_fighting",
    "praised": "warmth",
    "scolded": "friction",
    "left_hanging": "friction",
    "thanked": "warmth",
    "died_near": "needs_protection",
    "ignored_me": "friction",
}


def _now() -> float:
    return time.time()


def _decay(last: float, now: float) -> float:
    """半衰期衰减系数 0..1。"""
    if not last:
        return 0.0
    days = max(0.0, (now - last) / 86400.0)
    return 0.5 ** (days / HALF_LIFE_DAYS)


@dataclass
class Trait:
    """一个"关于他"的判断：值 -1..1、证据条数、最后一次时间。"""
    v: float = 0.0
    n: int = 0
    last: float = 0.0

    def bump(self, delta: float, now: float) -> None:
        d = self.v * _decay(self.last, now) if self.last else 0.0
        self.n += 1
        self.v = max(-1.0, min(1.0, d + delta / max(1.0, 1.0 + self.n / 8.0)))
        self.last = now

    def read(self, now: float) -> float:
        return self.v * _decay(self.last, now)


@dataclass
class Persona:
    """一个人的档案。字段顺序就是落盘顺序（方便人读）。"""
    player: str
    first_met: float = field(default_factory=_now)
    last_seen: float = field(default_factory=_now)
    sessions: int = 1
    minutes_together: float = 0.0
    address: str = ""                       # 他希望被怎么称呼（"老板"/名字/外号）
    style: str = "casual"                   # casual / formal / quiet（从他的话里推断）
    traits: dict = field(default_factory=dict)     # name -> Trait
    facts: list = field(default_factory=list)      # 他说过的关于自己/世界的话（短句，带时间）
    promises: list = field(default_factory=list)   # {"what","made_at","done","due"}
    history: list = field(default_factory=list)    # [{"t","kind",...}] 环
    counters: dict = field(default_factory=dict)   # gifts_given / mined_for_him / ...

    # ── 序列化 ─────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "player": self.player, "first_met": self.first_met, "last_seen": self.last_seen,
            "sessions": self.sessions, "minutes_together": round(self.minutes_together, 1),
            "address": self.address, "style": self.style,
            "traits": {k: {"v": round(t.v, 3), "n": t.n, "last": t.last}
                       for k, t in self.traits.items()},
            "facts": self.facts[-40:],
            "promises": self.promises[-30:],
            "history": self.history[-HISTORY_MAX:],
            "counters": self.counters,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Persona":
        p = cls(player=str(d.get("player") or ""))
        for k in ("first_met", "last_seen", "sessions", "minutes_together", "address", "style"):
            if k in d:
                setattr(p, k, d[k])
        p.traits = {k: Trait(v=float(x.get("v", 0)), n=int(x.get("n", 0)),
                             last=float(x.get("last", 0)))
                    for k, x in (d.get("traits") or {}).items()}
        p.facts = list(d.get("facts") or [])
        p.promises = list(d.get("promises") or [])
        p.history = list(d.get("history") or [])
        p.counters = dict(d.get("counters") or {})
        return p

    # ── 读 ─────────────────────────────────────────────────────────────────
    def warmth(self, now: float | None = None) -> float:
        """热度 -1..1：warmth 高、friction 低 ⇒ 熟人。"""
        now = now or _now()
        w = self.traits.get("warmth")
        f = self.traits.get("friction")
        return round((w.read(now) if w else 0.0) - (f.read(now) if f else 0.0), 3)

    def how_to_call(self) -> str:
        return self.address or self.player

    def brief(self, now: float | None = None, max_facts: int = 6) -> str:
        """给 AI / 给决策层看的一小段"关于他"。别超过一屏 —— 这是提示词不是档案。"""
        now = now or _now()
        bits = [f"玩家 {self.how_to_call()}（第 {self.sessions} 次一起玩，"
                f"累计 {self.minutes_together:.0f} 分钟，交情 {self.warmth(now):+.2f}）"]
        hot = sorted(((t.read(now), k) for k, t in self.traits.items()),
                     key=lambda x: -abs(x[0]))[:4]
        if hot:
            bits.append("特点：" + "、".join(f"{k}={v:+.2f}" for v, k in hot))
        open_promises = [p for p in self.promises if not p.get("done")]
        if open_promises:
            bits.append("还欠着：" + "；".join(str(p.get("what")) for p in open_promises[-3:]))
        if self.facts:
            bits.append("他说过：" + "；".join(str(f.get("text")) for f in self.facts[-max_facts:]))
        return " | ".join(bits)


class Memory:
    """一个人一份档案的读写口。指挥层/agent 只跟这个类说话。"""

    def __init__(self, player: str, root: Path = MEM_DIR):
        self.player = player
        self.dir = Path(root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{_safe(player)}.json"
        self.p = self._load()

    # ── 落盘 ───────────────────────────────────────────────────────────────
    def _load(self) -> Persona:
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                p = Persona.from_dict(d)
                p.sessions += 1
                p.last_seen = _now()
                return p
            except Exception:
                pass
        return Persona(player=self.player)

    @staticmethod
    def _guard(payload: dict) -> None:
        """拒绝把场景状态写进人物档案（这就是"不要场景记忆"的机械保证）。"""
        bad = [k for k in payload if str(k).lower() in FORBIDDEN_KEYS]
        if bad:
            raise ValueError(f"人物记忆不许存场景字段：{bad}")

    def save(self) -> Path:
        self.p.last_seen = _now()
        data = self.p.to_dict()
        self._guard({k: v for k, v in data.items() if k not in ("traits", "counters")})
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return self.path

    # ── 写 ─────────────────────────────────────────────────────────────────
    def note_event(self, kind: str, **kw) -> None:
        """记一件事。`kind` 见 TRAIT_OF_EVENT / 自由扩展。"""
        now = _now()
        ev = {"t": now, "kind": kind, **{k: v for k, v in kw.items()
                                         if str(k).lower() not in FORBIDDEN_KEYS}}
        self.p.history.append(ev)
        if len(self.p.history) > HISTORY_MAX:
            self.p.history = self.p.history[-HISTORY_MAX:]

        trait = TRAIT_OF_EVENT.get(kind)
        if trait:
            delta = float(kw.get("delta", 0.35))
            self.p.traits.setdefault(trait, Trait()).bump(delta, now)
        if kind in ("gave_item", "mined_for_him", "built"):
            key = {"gave_item": "gifts_given", "mined_for_him": "mined_for_him",
                   "built": "built"}[kind]
            self.p.counters[key] = self.p.counters.get(key, 0) + 1
        if kind == "session_tick":
            self.p.minutes_together += float(kw.get("minutes", 0.0))

    def note_fact(self, text: str, tag: str = "") -> None:
        """他自己说的一句"关于我/关于他"的话。这是最贵的信息，原样留着。"""
        t = (text or "").strip()
        if not t:
            return
        self.p.facts.append({"t": _now(), "text": t[:200], "tag": tag[:24]})
        self.p.facts = self.p.facts[-40:]
        if tag == "address":
            self.p.address = _extract_address(t)
        if tag == "style":
            self.p.style = t[:16]

    def promise(self, what: str) -> None:
        """我答应他的事。承诺不衰减，只被完成。"""
        self.p.promises = [x for x in self.p.promises if not x.get("done")]
        self.p.promises.append({"what": str(what)[:120], "made_at": _now(), "done": False})

    def fulfil(self, keyword: str) -> bool:
        for p in self.p.promises:
            if not p.get("done") and keyword in str(p.get("what", "")):
                p["done"] = True
                p["done_at"] = _now()
                return True
        return False

    # ── 读 ─────────────────────────────────────────────────────────────────
    def brief(self) -> str:
        return self.p.brief()

    def trait(self, name: str) -> float:
        t = self.p.traits.get(name)
        return round(t.read(_now()), 3) if t else 0.0

    def recent(self, kind: str | None = None, n: int = 10) -> list[dict]:
        rows = [e for e in self.p.history if kind is None or e.get("kind") == kind]
        return rows[-n:]


def _safe(name: str) -> str:
    return "".join(c for c in (name or "player") if c.isalnum() or c in "-_") or "player"


def _extract_address(text: str) -> str:
    """从"叫我老板就行"里把"老板"抠出来。

    不抠的话会把人家的整句话当称呼（实测：打招呼变成"我来了，叫我老板就行"）。
    """
    import re
    t = (text or "").strip()
    m = re.search(r"(?:叫我|喊我|称呼我|叫我做|叫我一声)\s*([^\s，。,.！!？?的]{1,8}?)(?:就行|就好|吧|就可以了|$)", t)
    if m:
        return m.group(1)
    m = re.search(r"([^\s，。,.！!？?]{1,8}?)(?:就行|就好|吧)$", t)
    if m:
        return m.group(1)
    return t[:12]


if __name__ == "__main__":                       # 自测：python -m companion.memory
    m = Memory("Xiaorun")
    m.note_fact("叫我老板就行", tag="address")
    m.note_event("asked_get", item="oak_log")
    m.note_event("gave_item", item="wooden_pickaxe")
    m.note_event("praised", delta=0.5)
    m.promise("给他做一把铁镐")
    print(m.brief())
    print("saved:", m.save())
