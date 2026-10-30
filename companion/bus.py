#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/bus.py —— 请求总线：把"人说的话"变成"陪玩要做的事"

为什么不是文件队列
------------------
第一版用 `runs/requests.jsonl`（一行一个 JSON）。它能跑，但有两个毛病：
  · **延迟**：写文件再轮询，天生慢一拍；
  · **没有回执**：请求发出去，人不知道陪玩收没收到。

所以改成进程内总线：输入层（chat / 语音 / EXE 面板 / 未来的手势）往 `push()` 里塞一条，
指挥层 `pop()` 取走；每条请求带 `id` 和 `status`，执行完写回 `done`，人可以在面板里看到。

请求形状（就是 Intent 的原材料）：
    {"kind": "get|mine|supply|build|hero|selfcare|follow|stop|say|teleport|ask|free",
     ...参数}
`free` 是自然语言原文（交给 agent 解析），`ask` 是"回答我一个问题"。
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque

PRIO = {              # 越小越先做
    "stop": 0, "teleport": 1, "hero": 2, "supply": 4, "get": 5, "mine": 5,
    "build": 6, "selfcare": 6, "follow": 8, "ask": 20, "free": 20, "say": 30,
}


class Bus:
    def __init__(self, maxlen: int = 200):
        self._q: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.log: deque[dict] = deque(maxlen=400)

    def push(self, kind: str, **kw) -> dict:
        req = {"id": next(self._ids), "kind": kind, "t": time.time(),
               "status": "queued", **kw}
        with self._lock:
            self._q.append(req)
        return req

    def pop(self) -> dict | None:
        """取最紧急的一条（PRIO 小的先做；同优先级按时间先后）。"""
        with self._lock:
            if not self._q:
                return None
            best = min(self._q, key=lambda r: (PRIO.get(r["kind"], 50), r["t"]))
            self._q.remove(best)
        best["status"] = "taken"
        return best

    def pop_only(self, kinds: set[str] | frozenset[str]) -> dict | None:
        """只从**指定几类**里取一条，别的 request 原样留在队里。

        为什么需要它（2026-10-27 现场血证）：长活（@get / @hero）跑在单线程作业池里
        要 40~110 秒，而指挥层在作业期间 `return` —— 这期间人问"你叫什么"，
        请求就**一直躺着**，30 秒后才被回答，面板上看起来就是"叫它不理"。
        说话/应答/急停不碰准星、不抢寻路，完全可以插队执行 ⇒ 给它们一条快车道。
        """
        with self._lock:
            cand = [r for r in self._q if r["kind"] in kinds]
            if not cand:
                return None
            best = min(cand, key=lambda r: (PRIO.get(r["kind"], 50), r["t"]))
            self._q.remove(best)
        best["status"] = "taken"
        return best

    def done(self, req: dict | None, ok: bool, note: str = "") -> None:
        if not req:
            return
        req.update({"status": "done" if ok else "failed", "ok": bool(ok),
                    "note": str(note)[:200], "done_at": time.time()})
        self.log.append(req)

    def pending(self) -> list[dict]:
        with self._lock:
            return list(self._q)

    def __len__(self) -> int:
        return len(self._q)


BUS = Bus()
