#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/guards.py —— 礼仪与安全闸：把"像不像人"写成可检查的规则

为什么单独一层（而不是散在 planner 里）：
  将来的 AI 会把任意意图丢进来（包括 `raw` 直通任何能力）。护栏必须**在意图和执行之间**，
  而不是在规则里 —— 否则换个 planner 就全漏了。

四条闸（每条都能独立解释"为什么刚才没做"）：
  NoChat     铁律 5：任何发聊天输出的意图一律拦掉
  Etiquette  不失礼：别贴脸、别抢玩家正在挖的那块、别站在玩家正前方挡视线
  Safety     不出事：自己血低时不许做高风险动作（会自动降级为跟随/撤退）
  Cooldown   不抽风：同一意图有最小间隔，防止 4Hz 刷指令把寻路打崩
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .registry import COMMANDS

CHAT_TOOLS = {"ChatMessage"}


@dataclass
class Verdict:
    allow: bool
    reason: str = ""

    def __bool__(self):
        return self.allow


ALLOW = Verdict(True)


class Guards:
    # 阈值都是显式常量，校准的时候一眼能改
    MIN_DIST = 1.6          # 比这更近就算贴脸（guard 意图除外）
    LOW_HP = 6.0            # 自己血低于这个，不许做高风险动作
    AHEAD_DEG = 35.0        # 玩家正前方这个扇形内，不要站进去
    AHEAD_DIST = 3.2
    SAME_CELL = 0.9         # 水平距离小于这个 = 站在同一格（2026-10-27 卡死血证）
    BACKOFF_DIST = 2.6      # 玩家自己在挖矿时，我至少要退到这么远

    def __init__(self):
        self._cd: dict[str, float] = {}
        # 礼仪计数（终局判据"挡路 0 / 抢块 0"的唯一数据源；null 里没有就等于没测）
        self.stats: dict[str, int] = {k: 0 for k in
                                      ("block_view", "too_close", "same_cell",
                                       "steal_block", "backoff_mining",
                                       "low_hp", "cooldown")}

    def _bump(self, key: str) -> None:
        self.stats[key] = int(self.stats.get(key, 0)) + 1

    def snapshot(self) -> dict:
        """给 trace / 验收台读的一行计数（累计值，不清零）。"""
        return {"guard_veto": dict(self.stats),
                "guard_veto_total": sum(int(v) for v in self.stats.values())}

    # ── 总入口 ──────────────────────────────────────────────────────────────
    def check(self, intent, obs, now: float) -> Verdict:
        for fn in (self._no_chat, self._cooldown, self._safety, self._etiquette):
            v = fn(intent, obs, now)
            if not v.allow:
                return v
        return ALLOW

    def stamp(self, intent, now: float) -> None:
        self._cd[intent.name] = now

    # ── 四道闸 ──────────────────────────────────────────────────────────────
    def _no_chat(self, intent, obs, now) -> Verdict:
        if intent.action in CHAT_TOOLS:
            return Verdict(False, "铁律5: 不许发聊天输出")
        if intent.action == "run_command":
            cmd = str((intent.args or {}).get("cmd") or "")
            if not cmd.startswith("@"):
                return Verdict(False, "铁律5: run_command 只接 AltoClef 命令（@ 开头）")
        return ALLOW

    def _cooldown(self, intent, obs, now) -> Verdict:
        cd = intent.cooldown
        if cd <= 0:
            return ALLOW
        last = self._cd.get(intent.name, 0.0)
        if now - last < cd:
            self._bump("cooldown")
            return Verdict(False, f"cooldown {cd:.0f}s（还剩 {cd - (now - last):.1f}s）")
        return ALLOW

    def _safety(self, intent, obs, now) -> Verdict:
        hp = (obs.get("me") or {}).get("hp")
        if hp is None or float(hp) >= self.LOW_HP:
            return ALLOW
        # 自己快死了：只允许"跑/跟/停/清怪"这类，别去挖矿盖房
        if intent.name in ("guard", "follow", "stay", "ack", "stop"):
            return ALLOW
        if intent.action == "run_command":
            cmd = str((intent.args or {}).get("cmd") or "")
            if any(k in cmd for k in ("@hero", "@stop", "@idle", "@unpause")):
                return ALLOW
        self._bump("low_hp")
        return Verdict(False, f"我自己血只有 {hp}，先保命")

    def _etiquette(self, intent, obs, now) -> Verdict:
        d = obs.get("dist")
        if intent.name == "guard":
            return ALLOW                      # 护主时贴脸是对的
        mp, pp = obs.get("self_pos"), obs.get("player_pos")
        det = obs.get("detail") or {}

        # ① 站在你那一格（2026-10-27 血证：两人同格 ⇒ 准星打到同伴 ⇒ 挖不动 ⇒ 卡死）
        #    必须排在"贴脸"之前：同格必然也贴脸，但修法不同（让开 vs 退开）。
        if (intent.name in ("follow", "approach") and mp and pp
                and math.hypot(mp[0] - pp[0], mp[2] - pp[2]) < self.SAME_CELL):
            self._bump("same_cell")
            return Verdict(False, "我站在你脚下了，先让开")

        # ② 贴脸
        if d is not None and d < self.MIN_DIST and intent.name in ("follow", "approach"):
            self._bump("too_close")
            return Verdict(False, f"太近（{d:.1f} 格），先退开")

        # ③ 正前方挡视线（用玩家 yaw 算一个扇形）
        if intent.name in ("follow", "approach") and det:
            yaw = det.get("yaw")
            if yaw is not None and pp and mp:
                dx, dz = mp[0] - pp[0], mp[2] - pp[2]
                dist = math.hypot(dx, dz)
                if dist < self.AHEAD_DIST:
                    ang = math.degrees(math.atan2(-dx, dz))
                    diff = abs((ang - float(yaw) + 180.0) % 360.0 - 180.0)
                    if diff < self.AHEAD_DEG:
                        self._bump("block_view")
                        return Verdict(False, f"我在你正前方 {dist:.1f} 格挡视线")

        # ④ 不抢你正对着的那块（"看哪挖哪"本身就是照着你指的那块挖，不算抢）
        aim = obs.get("player_aim")
        tgt = (intent.args or {}).get("target")
        look_driven = str(getattr(intent, "why", "") or "").startswith("你在看")
        if (aim and tgt and intent.action == "macro.mine" and not look_driven):
            try:
                ax, ay, az = int(aim[0]), int(aim[1]), int(aim[2])
                tx, ty, tz = int(tgt[0]), int(tgt[1]), int(tgt[2])
            except Exception:
                ax = ay = az = None
            if ax is not None and max(abs(ax - tx), abs(ay - ty), abs(az - tz)) <= 1:
                self._bump("steal_block")
                return Verdict(False, "这块你正对着，我不抢")

        # ⑤ 你在挖矿时别贴上来（player_state.mining 由玩家状态机填；没填就跳过）
        st = obs.get("player_state") or {}
        if (st.get("mining") and intent.name in ("follow", "approach", "build_place")
                and d is not None and d < self.BACKOFF_DIST):
            self._bump("backoff_mining")
            return Verdict(False, "你在挖，我先退开")

        return ALLOW

    # ── 给日志用 ────────────────────────────────────────────────────────────
    @staticmethod
    def risk_of(command: str) -> str:
        name = command.lstrip("@").split()[0] if command else ""
        return (COMMANDS.get(name) or {}).get("risk", "unknown")
