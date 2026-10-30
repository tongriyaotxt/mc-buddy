#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/speech.py —— 说话：**能说**之后，难的是"别烦人"

铁律 5 已取消（operator 2026-10-26），所以陪玩可以在游戏里打字说话。但一句能说话就让
聊天框变成日志流是最快的劝退方式。这一层管的是**怎么说**：

  1. **限流**：默认最小间隔 `min_gap` 秒（同一句话另有更长的去重窗口）。
  2. **不打断**：同一时刻只挂一条待发消息；新的进来先排队，超时（`ttl`）就丢掉 —— 过时的
     话不如不说（"我挖完了"在 30 秒后说出来是噪音）。
  3. **分层语气**：`tone` 由人物记忆决定（熟人轻松、生人客气、`quiet` 就只答不问）。
  4. **短**：单条上限 `max_len`，超了截断。聊天框不是文档。
  5. **可选声音**：同一条文本同时交给 `voice_io`（如果开着），文字与语音共用一套礼貌规则。

它**不判断该不该说**（那是 planner/agent 的事），只保证说出去的每一句都体面。
"""
from __future__ import annotations

import random
import threading
import time

ROOT_SAY_MAX = 100          # 单条最大字数


class Speech:
    def __init__(self, body=None, voice=None, *, enabled: bool = True,
                 min_gap: float = 6.0, dedupe_window: float = 90.0,
                 max_len: int = ROOT_SAY_MAX, tone: str = "casual", log=None,
                 gate=None):
        self.body = body
        self.voice = voice
        self.enabled = enabled
        self.min_gap = min_gap
        self.dedupe_window = dedupe_window
        self.max_len = max_len
        self.tone = tone
        self.log = log or (lambda obj: None)
        # 说话权闸门（多陪玩时只让一个人对外说话，其余静默干活）
        self.gate = gate
        self._last_at = 0.0
        self._last_text = ""
        self._recent: list[tuple[float, str]] = []
        self._lock = threading.Lock()
        self.said = 0
        self.skipped = 0

    # ── 语气包 ─────────────────────────────────────────────────────────────
    def _flavor(self, text: str) -> str:
        """加一点点口语尾音 —— 但**不能给任何句子都加**。

        2026-10-27 现场：`停了` + `，给你放这儿了` = 「停了，给你放这儿了」，
        `做不到` + 同样的尾巴也很怪。所以：短句、否定句、祈使句一律不加尾巴。
        2026-10-27 追加（operator：说话拟人一点）：尾巴改成"朋友随口的感觉"，
        不再是客服收尾词；而且**只有长句**才加，避免每句话尾巴都一样。
        """
        t = (text or "").strip()
        if self.tone == "quiet" or len(t) <= 6 or t[:1] in "停不没别勿请":
            return t[: self.max_len]
        tails = {"casual": ["", "", "啊", "吧", "，就这样"],
                 "formal": ["", "。", "，请稍等"],
                 "clingy": ["", "～", "，我盯着呢", "，你说一声我就动"],
                 }
        tail = random.choice(tails.get(self.tone, [""]))
        return (t + tail)[: self.max_len]

    # ── 主入口 ─────────────────────────────────────────────────────────────
    def say(self, text: str, *, force: bool = False, kind: str = "") -> bool:
        """说一句。返回是否真的发出去了。"""
        if not self.enabled or not (text or "").strip():
            return False
        if self.gate is not None:
            try:
                if not self.gate():
                    self.skipped += 1
                    return False          # 不是我说话的时候：静默，但不报错
            except Exception:
                pass
        t = (text or "").strip()[: self.max_len]
        now = time.time()
        with self._lock:
            self._recent = [(ts, s) for ts, s in self._recent if now - ts < self.dedupe_window]
            if not force:
                if now - self._last_at < self.min_gap:
                    self.skipped += 1
                    return False
                if t == self._last_text:
                    self.skipped += 1
                    return False
                if any(s == t for _, s in self._recent):
                    self.skipped += 1
                    return False
            self._last_at = now
            self._last_text = t
            self._recent.append((now, t))
            self.said += 1

        out = self._flavor(t)
        ok = False
        if self.body is not None:
            try:
                ok = bool(self.body.say(out))
            except Exception as e:
                self.log({"ev": "say_error", "err": f"{type(e).__name__}: {e}"[:120]})
        if self.voice is not None:
            try:
                self.voice.speak(out)          # 声音与文字共用一套礼貌规则
            except Exception:
                pass
        self.log({"ev": "say", "kind": kind, "text": out, "ok": ok})
        return ok

    # ── 常用应答（都是短句；不要在这里写长逻辑） ────────────────────────────
    # 2026-10-27（operator：说话拟人一点）：这些是**最常被听到**的几句，
    # 所以它们决定了"像人还是像客服"。统一原则：短、口语、不播报流程、不报"任务完成"。
    def ack(self, what: str = "") -> bool:
        return self.say(random.choice(
            ["好", "嗯", "成", "行啊", "收到"] + ([f"行，{what}"] if what else [])))

    def doing(self, what: str) -> bool:
        return self.say(random.choice([f"{what}去", f"{what}，等我一下", f"这就{what}"]))

    def done(self, what: str) -> bool:
        return self.say(random.choice([f"{what}好了", f"{what}搞定", f"{what}弄完了"]))

    def cant(self, why: str = "") -> bool:
        head = random.choice(["这个我真弄不了", "这个不成", "我搞不定"])
        return self.say(head + (f"，{why}" if why else ""), force=True)

    def hello(self, name: str = "") -> bool:
        who = name or ""
        return self.say(random.choice(
            [f"来啦{who}", f"我在呢{who}", f"来了来了{who}"]), force=True)

    def stats(self) -> dict:
        return {"said": self.said, "skipped": self.skipped, "tone": self.tone,
                "enabled": self.enabled}
