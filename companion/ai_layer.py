#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/ai_layer.py —— **唯一交互层**：所有"要做什么"都由 AI 决定

operator 2026-10-29：「不用行为互动，全部进化为通过 ai 层进行交互」。

于是把职责切成两半（这条线很重要）：

    AI 层（本文件）      听懂人话、决定做什么、决定说什么、决定没事时干什么
    指挥层（commander）  只做三件"不能交给模型"的事：
                          ① 执行（把 AI 的工具调用落到身体上，单瞄准者）
                          ② 安全联锁（stop / 自己血低 / 一次只一件事 / 预算与超时）
                          ③ 记账（trace：谁在什么时候要了什么、结果如何）

**事件驱动**，不是每 tick 调模型（否则又慢又贵）：
    人说话 / 语音 / 面板 / 眼神 / 威胁出现 / 活干完 / 空闲超过 N 秒
每次只把一个"事件 + 现场摘要"交给模型，模型自己选工具。

三种降级（都要留，但**只作为底线**，不参与"怎么陪玩"的决策）：
    没 key       → 只回一句"我现在连不上脑子"，不动手（绝不假装做了）
    超时/报错    → 记 ai_error，事件放回队列尾部（最多重试 1 次）
    预算用完     → 记 ai_budget，等下一个事件
"""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field

# 事件 → 给模型的一句话（**只说事实，不教它怎么做**）
TEMPLATES = {
    "say_text": "玩家说：{text}",
    "voice": "玩家（语音）说：{text}",
    "panel": "玩家在面板上点了：{text}",
    "gaze": "玩家正看着方块 {block}@{pos}，离他 {dist} 格。",
    "threat": "玩家附近有 {n} 只敌对生物，最近的是 {kind}，离玩家 {dist} 格。",
    "hurt": "玩家血量降到 {hp}，附近敌对 {n} 只。",
    "job_done": "你刚才那件事结束了：{text}",
    "idle": "现在没有别的事，玩家在你附近 {dist} 格。判断一件对他有用、且这个世界做得到的事；"
            "做不到就直接待着，别硬来。",
}


@dataclass
class AiEvent:
    kind: str
    text: str = ""
    data: dict = field(default_factory=dict)
    at: float = field(default_factory=time.time)
    tries: int = 0

    def goal(self) -> str:
        tpl = TEMPLATES.get(self.kind)
        if tpl is None:
            return self.text or ""
        try:
            return tpl.format(text=self.text, **self.data)
        except Exception:
            return self.text or tpl


class AiLayer:
    """把"事件"喂给 AI，拿回工具调用结果。指挥层只负责调度它。"""

    MAX_QUEUE = 8
    MAX_PER_MIN = 10          # 一分钟最多问模型几次（花钱 + 延迟都在这里兜住）

    def __init__(self, agent, *, log=None, budget_s: float = 40.0, planner=None,
                 daily_token_budget: int = 0):
        self.agent = agent
        self.log = log or (lambda o: None)
        self.budget_s = budget_s
        # 自动前置：收到人话时先算"它会不会做/它自己怎么拆"（companion/goal_graph.auto_plan）
        self.planner = planner
        self.q: deque[AiEvent] = deque(maxlen=self.MAX_QUEUE)
        self.calls: deque[float] = deque(maxlen=64)
        self.dropped = 0
        self.calls_total = 0
        # ── 成本护栏（2026-10-29）──────────────────────────────────────────
        # 没有上限的模型调用 = 账单失控。默认每天 40 万 token（约 100~150 次对话），
        # 超了就**降级成确定性执行器**（仍会动手、仍会回一句"今天额度用完了"）。
        self.budget = int(daily_token_budget or self._budget_from_cfg())
        self.tokens_used = 0
        self._day = time.strftime("%Y-%m-%d")
        self.exhausted = False
        self._notified = False

    @staticmethod
    def _budget_from_cfg() -> int:
        try:
            import json
            from pathlib import Path
            p = Path(__file__).resolve().parent.parent / "config" / "ai.json"
            d = json.loads(p.read_text(encoding="utf-8"))
            return int(d.get("daily_token_budget") or 400_000)
        except Exception:
            return 400_000

    def _roll_day(self, now: float | None = None) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self.tokens_used, self.exhausted, self._notified = today, 0, False, False
            self.log({"ev": "ai_budget_reset", "day": today, "budget": self.budget})

    def add_tokens(self, usage: dict | None) -> int:
        """记账（Agent 的 usage 形状：prompt_tokens/completion_tokens/total_tokens，允许缺）。"""
        u = usage or {}
        n = int(u.get("total_tokens") or 0) or (int(u.get("prompt_tokens") or 0)
                                                + int(u.get("completion_tokens") or 0))
        self.tokens_used += max(0, n)
        over = self.budget > 0 and self.tokens_used >= self.budget
        if over and not self.exhausted:
            self.exhausted = True
            self.log({"ev": "ai_budget_exhausted", "used": self.tokens_used,
                      "budget": self.budget})
        return n

    # ── 能力 ────────────────────────────────────────────────────────────────
    def enabled(self) -> bool:
        return bool(getattr(self.agent, "has_model", False))

    def why_disabled(self) -> str:
        return "没有可用的模型（config/ai.json 里没有 api_key）"

    def _budget_ok(self, now: float) -> bool:
        self.calls = deque((t for t in self.calls if now - t < 60.0), maxlen=64)
        return len(self.calls) < self.MAX_PER_MIN

    # ── 入队 ────────────────────────────────────────────────────────────────
    def push(self, kind: str, text: str = "", **data) -> AiEvent:
        """同一类事件在队列里只留最新一条（例如连续三次"玩家在看我"只处理一次）。"""
        ev = AiEvent(kind=kind, text=text, data=data)
        for old in list(self.q):
            if old.kind == kind:
                self.q.remove(old)
        self.q.append(ev)
        self.log({"ev": "ai_event", "kind": kind, "text": text[:80], "q": len(self.q)})
        return ev

    def pending(self) -> bool:
        return bool(self.q)

    def next_event(self, now: float | None = None) -> AiEvent | None:
        now = now or time.time()
        self._roll_day(now)
        if not self.enabled() or not self.q:
            return None
        if self.exhausted:
            ev = self.q.popleft()
            self.dropped += 1
            self.log({"ev": "ai_budget_exhausted_skip", "kind": ev.kind,
                      "used": self.tokens_used, "budget": self.budget})
            return None
        if not self._budget_ok(now):
            ev = self.q.popleft()
            self.dropped += 1
            self.log({"ev": "ai_budget", "kind": ev.kind, "q": len(self.q)})
            return None
        # 派发即占额度：否则"取出来还没执行"的这段时间里可以无限取
        self.calls.append(now)
        return self.q.popleft()

    # ── 真正去问模型 ────────────────────────────────────────────────────────
    def handle(self, ev: AiEvent) -> dict:
        goal = ev.goal()
        # ── 自动化前置：先算分解图/可行性，再交给模型（不用它自己想起来调 plan_item）──
        if self.planner is not None and ev.kind in ("say_text", "voice", "panel"):
            try:
                plan = self.planner(ev.text)
                if plan:
                    from companion.goal_graph import prompt_block
                    block = prompt_block(plan)
                    goal = f"{goal}\n\n{block}"
                    self.log({"ev": "ai_preplan", "item": plan.get("item"),
                              "mode": plan.get("mode"), "ok": plan.get("ok")})
            except Exception as e:
                self.log({"ev": "ai_preplan_err", "err": f"{type(e).__name__}: {e}"[:120]})
        t0 = time.time()
        self.calls_total += 1
        self.log({"ev": "ai_call", "kind": ev.kind, "goal": goal[:160],
                  "tries": ev.tries})
        try:
            res = self.agent.run(goal, budget_s=self.budget_s)
        except TypeError:
            res = self.agent.run(goal)
        except Exception as e:
            res = {"ok": False, "err": f"{type(e).__name__}: {e}"[:200]}
        out = {"ok": bool(res.get("ok")), "how": res.get("how"),
               "say": res.get("say", ""), "steps": res.get("steps") or [],
               "sec": round(time.time() - t0, 1), "event": ev.kind}
        # 成本记账 + 降级
        used = self.add_tokens(res.get("tokens") if isinstance(res, dict) else None)
        out["tokens"] = used
        if self.exhausted and not self._notified:
            self._notified = True
            try:
                if getattr(self.agent, "speech", None) is not None:
                    self.agent.speech.say("今天的额度用完了，我先只动手不动脑", force=True)
            except Exception:
                pass
        self.log({"ev": "ai_done", **{k: out[k] for k in ("ok", "how", "say", "sec", "event")},
                  "n_steps": len(out["steps"]), "tokens": used,
                  "used_total": self.tokens_used, "budget": self.budget})
        return out

    # ── 观测：给面板/诊断用 ────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {"enabled": self.enabled(), "pending": len(self.q),
                "calls_total": self.calls_total, "dropped": self.dropped,
                "tokens_used": self.tokens_used, "budget": self.budget,
                "exhausted": self.exhausted,
                "queue": [e.kind for e in self.q]}
