#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/companion_gate.py —— 陪玩指挥层的"一条命令过闸"（离线，不连服务端）

为什么要有它（PLAN-OMNI-COMPANION M1 的最后一项，2026-10-27 晚补）：
  单独跑 caps_test / session_test 都要活体（服务端 + 客户端），改一行规则要等 5 分钟起服；
  于是"改完先过闸"这件事在指挥层等于没有 —— 而旧栈早就有四道闸（node --check / _tdz /
  _freevar / smoke_policy）。这个脚本补上同一性质的东西：**秒级、确定、失败就红**。

它检查的七类东西：
  ① 模块可导入（17 个模块，语法/导入环/删错函数立刻现形）
  ② 能力表 schema（id 唯一、状态枚举、写「通」必须有 evidence）
  ③ 规则规划器（距离 → 跟随/退让/待命/护主 四条分支）
  ④ 礼仪闸（同格/贴脸/挡视线/抢方块/挖矿退让 五条规则 + 计数）
  ⑤ 自然语言（指令化与自然语言同一路）
  ⑥ 说话礼貌（限流 + 去重）
  ⑦ 队伍规则（认领先到先得、说话权、编队分槽）
  ⑧（可选 --require-live）活体能力面：MCP 端口在时校验工具数 ≥ 80

用法：
    python -X utf8 tools/companion_gate.py
    python -X utf8 tools/companion_gate.py --require-live     # 发布前跑（端口不在 = 失败）
    python -X utf8 tools/companion_gate.py --json             # 机器可读

退出码：0 全绿；1 有失败。
"""
from __future__ import annotations

import argparse
import importlib
import json
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULT: list[dict] = []


def rec(kind: str, name: str, ok: bool | None, detail: str = "") -> None:
    RESULT.append({"kind": kind, "name": name, "ok": ok,
                   "detail": str(detail)[:400]})
    tag = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    print(f"  [{tag}] {name:34s} {detail}"[:200], flush=True)


def check(name: str, fn, *, skip: str = "") -> None:
    if skip:
        rec("check", name, None, skip)
        return
    try:
        detail = fn()
        rec("check", name, True, detail or "")
    except AssertionError as e:
        rec("check", name, False, f"断言失败: {e}")
    except Exception as e:
        rec("check", name, False, f"{type(e).__name__}: {e}")
        if "-v" in sys.argv:
            traceback.print_exc()


# ── ① 模块导入 ──────────────────────────────────────────────────────────────
MODULES = ["body", "body_ctl", "registry", "guards", "planner", "nlu", "speech",
           "team", "memory", "tasks", "bus", "hook", "voice_io", "chat_in",
           "actions", "agent", "commander"]


def check_imports() -> str:
    bad = []
    for m in MODULES:
        try:
            importlib.import_module(f"companion.{m}")
        except Exception as e:
            bad.append(f"{m}: {type(e).__name__}: {e}")
    assert not bad, "; ".join(bad)
    return f"{len(MODULES)} 个模块"


# ── ② 能力表 schema ─────────────────────────────────────────────────────────
STATUS = {"通", "半通", "不通", "未做"}
REQUIRED = ("id", "wave", "prio", "name", "trigger", "lever", "accept", "status", "evidence")


def check_capabilities() -> str:
    import yaml
    p = ROOT / "companion" / "capabilities.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    ids, n, ng = [], 0, 0
    problems = []
    for section in ("caps", "caps_extra", "caps_team", "caps_hook"):
        for item in doc.get(section) or []:
            n += 1
            for k in REQUIRED:
                if k not in item:
                    problems.append(f"{item.get('id', '?')} 缺 {k}")
            cid = str(item.get("id") or "")
            if cid in ids:
                problems.append(f"id 重复: {cid}")
            ids.append(cid)
            st = str(item.get("status"))
            if st not in STATUS:
                problems.append(f"{cid} 状态非法: {st}")
            if st == "通":
                ng += 1
                if len(str(item.get("evidence") or "")) < 12:
                    problems.append(f"{cid} 记「通」但 evidence 太短（没血证）")
    assert not problems, "; ".join(problems[:6])
    assert n >= 30, f"能力表只剩 {n} 项？"
    return f"{n} 项（通 {ng} / 其余 {n - ng}）"


# ── ③ 规则规划器 ────────────────────────────────────────────────────────────
def _obs(dist=3.0, mp=(0.0, 64.0, 0.0), pp=(0.0, 64.0, 0.0), *, hp=20, php=20,
         near=False, park=(3.0, 64.0, 3.0), yaw=0.0, pitch=0.0):
    return {"me": {"hp": hp}, "pl": {"hp": php, "name": "Xiaorun"},
            "detail": {"yaw": yaw, "pitch": pitch, "held": "minecraft:air",
                       "sneaking": False, "hp": 20, "pos": list(pp)},
            "self_pos": list(mp), "player_pos": list(pp), "dist": dist,
            "nav": {}, "park_point": list(park),
            "threat": {"count": 1 if near else 0, "near_player": near,
                       "type": "zombie" if near else None, "dist": 5.0 if near else None},
            "probe": lambda x, y, z: {"id": "minecraft:air", "_air": True}}


def check_planner() -> str:
    from companion.planner import RulePlanner
    p = RulePlanner(requests=None)
    m: dict = {}
    far = p.plan(_obs(dist=12.0, mp=(0, 64, 12)), m)
    assert far and far.name == "follow", f"12 格外应跟随，得到 {far and far.name}"
    # 近距用例把玩家朝向设成背对（yaw=180），否则会先命中「你在看我 → 点头」那条分支
    near = p.plan(_obs(dist=1.2, mp=(0, 64, 1.2), yaw=180.0), {})
    assert near and near.name == "approach", f"1.2 格应退开，得到 {near and near.name}"
    stay = p.plan(_obs(dist=2.6, mp=(0, 64, 2.6), yaw=180.0), {})
    assert stay and stay.name == "stay", f"2.6 格应待命，得到 {stay and stay.name}"
    threat = p.plan(_obs(dist=2.6, mp=(0, 64, 2.6), yaw=180.0, near=True),
                    {"guard_at": 0.0})
    assert threat and threat.name == "guard", f"怪来了应护主，得到 {threat and threat.name}"
    lowhp = p.plan(_obs(dist=6.0, mp=(0, 64, 6), php=5, yaw=180.0), {"guard_at": 0.0})
    assert lowhp and lowhp.name == "guard", f"玩家血低应护主，得到 {lowhp and lowhp.name}"
    return "follow/approach/stay/guard 四分支正确"


# ── ④ 礼仪闸 ────────────────────────────────────────────────────────────────
def check_guards() -> str:
    from companion.guards import Guards
    from companion.planner import Intent

    def run(intent, obs):
        return Guards().check(intent, obs, time.time())

    # 同格
    v = run(Intent("follow", "gotoXYZ"), _obs(dist=0.4, mp=(0, 64, 0.4)))
    assert not v.allow and "脚下" in v.reason, f"同格未拦: {v.reason}"
    # 贴脸
    v = run(Intent("follow", "gotoXYZ"), _obs(dist=1.2, mp=(0, 64, 1.2)))
    assert not v.allow and "太近" in v.reason, f"贴脸未拦: {v.reason}"
    # 挡视线：玩家 yaw=0，我站在 +z 2 格
    v = run(Intent("follow", "gotoXYZ"), _obs(dist=2.0, mp=(0, 64, 2.0), yaw=0.0))
    assert not v.allow and "挡" in v.reason, f"挡视线未拦: {v.reason}"
    # 抢方块
    o = _obs(dist=2.0, mp=(3, 64, 3))
    o["player_aim"] = (10, 64, 10, "minecraft:stone")
    v = run(Intent("look_mine", "macro.mine", {"target": [10, 64, 10]}, why="挖这块"), o)
    assert not v.allow and "不抢" in v.reason, f"抢方块未拦: {v.reason}"
    # 看哪挖哪不算抢
    v = run(Intent("look_mine", "macro.mine", {"target": [10, 64, 10]},
                   why="你在看 minecraft:stone"), o)
    assert v.allow, f"看哪挖哪被误拦: {v.reason}"
    # 你在挖矿 → 退开
    o2 = _obs(dist=2.0, mp=(0, 64, 2.0), yaw=180.0)   # 站到背侧，避开挡视线
    o2["player_state"] = {"mining": True}
    v = run(Intent("follow", "gotoXYZ"), o2)
    assert not v.allow and "挖" in v.reason, f"挖矿时未退让: {v.reason}"
    # 保命：自己血低不许去采集
    v = run(Intent("get_item", "macro.get"), _obs(dist=3.0, hp=5))
    assert not v.allow and "保命" in v.reason, f"低血未拦: {v.reason}"
    # 计数
    g = Guards()
    for _ in range(3):
        g.check(Intent("follow", "gotoXYZ"), _obs(dist=1.0, mp=(0, 64, 1.0)), time.time())
    snap = g.snapshot()
    assert snap["guard_veto"]["too_close"] == 3, f"计数不对: {snap}"
    return "5 条规则 + 计数正确"


# ── ⑤ 自然语言 ──────────────────────────────────────────────────────────────
def check_nlu() -> str:
    from companion.nlu import parse
    a = parse("b 挖木头")
    assert a["kind"] == "get" and a["conf"] == 1.0, f"指令化解析错: {a}"
    b = parse("帮我砍三根木头")
    assert b["kind"] == "get" and b.get("item") == "oak_log" and b.get("count") == 3, \
        f"自然语言解析错: {b}"
    c = parse("跟上我")
    assert c["kind"] == "follow", f"跟随解析错: {c}"
    return "指令化 / 自然语言 / 跟随 三条"


# ── ⑥ 说话礼貌 ──────────────────────────────────────────────────────────────
def check_speech() -> str:
    from companion.speech import Speech

    class FakeBody:
        def __init__(self):
            self.said = []

        def say(self, text):
            self.said.append(text)
            return True

    b = FakeBody()
    s = Speech(b, enabled=True, min_gap=6.0)
    assert s.say("我来了") is True, "第一句应发出"
    assert s.say("我来了") is False, "6 秒内第二句应被限流"
    s2 = Speech(b, enabled=True, min_gap=0.0, dedupe_window=90.0)
    assert s2.say("搞定了") is True and s2.say("搞定了") is False, "去重失效"
    return "限流 + 去重"


# ── ⑦ 队伍规则 ──────────────────────────────────────────────────────────────
def check_team() -> str:
    from companion.team import Team
    tmp = Path(tempfile.mkdtemp(prefix="gate_team_")) / "board.json"
    a = Team(tmp, me="Buddy", role="playmate", enabled=True)
    b = Team(tmp, me="Buddy2", role="guard", enabled=True)
    a.beat(pos=(0, 64, 0), busy="")
    b.beat(pos=(3, 64, 0), busy="")
    assert a.claim("get:oak_log") is True, "先到应先得"
    assert b.claim("get:oak_log") is False, "后来者不该拿到同一件活"
    assert a.owner("get:oak_log") == "Buddy"
    a.release("get:oak_log")
    assert b.claim("get:oak_log") is True, "释放后应放行"
    sp = a.may_speak()
    assert sp in (True, False)
    pa, pb = a.slot((0, 64, 0)), b.slot((0, 64, 0))
    import math
    sep = math.dist(pa, pb)
    assert sep >= 1.6, f"编队间距 {sep:.2f} < 1.6"
    return f"认领/释放/说话权/编队间距 {sep:.2f} 格"


# ── ⑧ 角色闸（2026-10-27 晚新增） ──────────────────────────────────────────
def check_role() -> str:
    from companion.commander import ROLE_ALLOW
    assert ROLE_ALLOW["guard"] is not None and "get" not in ROLE_ALLOW["guard"], \
        "guard 角色不应接采集活"
    assert ROLE_ALLOW["playmate"] is None, "playmate 应不限制"
    # 真跑一遍 role_allows（不连身体）
    from companion.commander import Commander
    fn = Commander.role_allows
    class Fake:
        role = "guard"
        bot_name = "Buddy"
    assert fn(Fake(), {"kind": "get"}) is False, "guard 接了 get"
    assert fn(Fake(), {"kind": "get", "to": "Buddy"}) is True, "点名找我的活应放行"
    assert fn(Fake(), {"kind": "get", "to": "Buddy2"}) is False, "点名给别人的活不该抢"
    assert fn(Fake(), {"kind": "hero"}) is True, "guard 应接护主"
    return "guard 拒采集 / 点名放行 / 护主可接"


# ── ⑨ 任务层（TaskSpec + 验收谓词 + 配装腿） ────────────────────────────────
def check_tasks() -> str:
    from companion import tasks
    for kind, args in (("get", {"item": "oak_log", "count": 2}),
                       ("supply", {"item": "bread", "count": 1}),
                       ("hero", {}), ("follow", {}),
                       ("mine", {"target": [1, 2, 3]})):
        spec = tasks.spec_for(kind, args)
        assert spec is not None, f"{kind} 没有 TaskSpec"
        assert spec.accept in tasks.ACCEPT, f"{kind} 的验收键 {spec.accept} 不在 ACCEPT 里"
        assert spec.budget_s > 0, f"{kind} budget 非法"
    assert callable(getattr(tasks, "_ensure_gear", None)), "护主配装腿不见了"
    return f"{len(tasks.ACCEPT)} 个验收谓词 / 5 个杠杆"


# ── ⑩ 终局判据统计（interaction_score） ─────────────────────────────────────
def check_scoring() -> str:
    import json as _json
    sys.path.insert(0, str(ROOT / "tools"))
    import importlib
    iscore = importlib.import_module("interaction_score")
    tmp = Path(tempfile.mkdtemp(prefix="gate_score_"))
    evs = [
        {"ev": "heard", "t": 0.0},
        {"ev": "say", "t": 1.0},          # 1 秒内 → 去抖，不算
        {"ev": "say", "t": 6.0, "kind": "narrate"},   # 播报不算互动
        {"ev": "job_done", "t": 10.0, "result": {"ok": True}},
        {"ev": "job_done", "t": 20.0, "result": {"ok": False}},   # 失败不算
        {"ev": "task_done", "t": 30.0, "ok": True},
        {"ev": "gesture", "t": 40.0},
        {"ev": "etiquette", "guard_veto": {"block_view": 2, "steal_block": 1}},
        {"ev": "etiquette", "guard_veto": {"block_view": 2, "steal_block": 1}},
        {"ev": "death"},
    ]
    (tmp / "trace.jsonl").write_text(
        "\n".join(_json.dumps(e) for e in evs), encoding="utf-8")
    s = iscore.score(iscore.read_events(tmp))
    assert s["interactions"] == 4, f"有效互动应为 4（去抖+失败过滤），得到 {s['interactions']}"
    assert s["etiquette"]["block_view"] == 2, f"挡路计数错: {s['etiquette']}"
    assert s["etiquette"]["steal_block"] == 1, f"抢块计数错: {s['etiquette']}"
    assert s["deaths"] == 1, f"死亡计数错: {s['deaths']}"
    return f"互动 {s['interactions']} / 挡路 {s['etiquette']['block_view']} / 死 {s['deaths']}"


# ── ⑪ 反射弧（2026-10-29：operator「反射弧太长」） ──────────────────────────
def check_reflex() -> str:
    import inspect
    from companion.commander import Commander
    sig = inspect.signature(Commander.observe)
    assert "fast" in sig.parameters, "observe 没有 fast 参数（两级观察没接上）"
    assert hasattr(Commander, "reflex"), "reflex 不存在"
    src = inspect.getsource(Commander.step)
    assert "observe(fast=" in src, "step 没有用两级观察"
    assert "self.reflex(obs, now)" in src, "step 没有每 tick 调 reflex"

    # 用一个最小 stub 真跑一遍 reflex：护主必须抢占，应答不许打断
    class Bus:
        def __init__(self):
            self.items = []

        def push(self, kind, **kw):
            self.items.append((kind, kw))

    calls = []
    c = Commander.__new__(Commander)
    c.dry = False
    c.job = None
    c._guard_at = 0.0
    c._ack_at = 0.0
    c._hurt_at = 0.0
    c._look_at = 0.0
    c.ai = None                      # 这一段测的是"没有 AI 时的规则反射"（AI 分支另有单测）
    c.bus = Bus()
    c.log = lambda o: None
    c.job_running = lambda: False
    c.cancel_job = lambda why: calls.append(("cancel", why))
    c.execute = lambda intent, obs: calls.append(("exec", intent.name)) or True

    obs = {"threat": {"near_player": True, "dist": 4.0, "type": "zombie"},
           "detail": {}, "player_pos": [0, 64, 0], "self_pos": [3, 64, 0], "dist": 3.0}
    hit = c.reflex(obs, 100.0)
    assert hit is True and ("exec", "guard") in calls, f"护主反射没触发: {calls}"

    calls.clear()
    obs2 = {"threat": {}, "detail": {"yaw": 0.0},
            "player_pos": [0, 64, 0], "self_pos": [0, 64, 3], "dist": 3.0,
            "player_aim": (10, 64, 10, "minecraft:stone")}
    hit2 = c.reflex(obs2, 200.0)
    assert hit2 is False, "应答不该抢占"
    assert ("exec", "ack") in calls, f"应答反射没触发: {calls}"
    assert c.bus.items and c.bus.items[0][0] == "mine", f"眼神没排队: {c.bus.items}"
    return "护主抢占 / 应答不抢占 / 眼神排队"


# ── ⑫ 变通（2026-10-29 operator：「没牛就做不出皮甲，选做别的」） ────────────
def check_adaptivity() -> str:
    import inspect
    from companion import actions, tasks
    from companion.commander import Commander
    assert "idle_next" in inspect.getsource(Commander.step), "空闲决策没接进主循环"
    assert hasattr(Commander, "idle_next")

    class FakeReg:
        def invoke(self, action, args=None):
            class R:
                ok = True
                data = []
                err = ""
            return R()

    orig_count = actions.count_item
    orig_ents = actions.nearby_entities
    try:
        # ① 没牛、没皮、没别的原料：皮甲不可行 ⇒ 阶梯一路否决
        actions.count_item = lambda reg, item: 0
        actions.nearby_entities = lambda reg, **kw: []
        item, trace = tasks.pick_alternative(FakeReg(), "leather_chestplate")
        assert item is None, f"没有牛却选了 {item}"
        assert len(trace) >= 2 and all(not t["ok"] for t in trace), f"阶梯判定不对: {trace}"
        # ② 有牛：皮甲可行 ⇒ 直接选它
        actions.nearby_entities = lambda reg, **kw: [
            {"type": "cow", "hostile": False, "pos": [0, 64, 0]}]
        item2, trace2 = tasks.pick_alternative(FakeReg(), "leather_chestplate")
        assert item2 == "leather_chestplate", f"有牛却没选皮甲: {item2} {trace2}"
        # ③ 铁不在、木也没有：木剑这级也要否决（不是"闷头做"）
        actions.nearby_entities = lambda reg, **kw: []
        item3, trace3 = tasks.pick_alternative(FakeReg(), "wooden_sword")
        assert item3 is None, f"没树没石却选了 {item3}"
    finally:
        actions.count_item = orig_count
        actions.nearby_entities = orig_ents
    return "没牛→否决皮甲 / 有牛→选皮甲 / 没树→否决木剑"


# ── ⑭ AI 交互层（2026-10-29 operator：全部交互走 AI） ──────────────────────
def check_ai_layer() -> str:
    from companion.ai_layer import AiLayer
    from companion.ai_layer import TEMPLATES

    class FakeAgent:
        has_model = True

        def __init__(self):
            self.goals = []

        def run(self, goal, **kw):
            self.goals.append(goal)
            return {"ok": True, "how": "fake", "say": "好的", "steps": []}

    # ① 每种事件都要能拼出一句给模型的话
    for kind in TEMPLATES:
        ev_kind = kind
        ev = AiEventProbe(ev_kind)
        assert ev.goal(), f"{kind} 拼不出 goal"

    # ② 同类事件去重（连续三次"在看我"只处理一次）
    lay = AiLayer(FakeAgent())
    for _ in range(3):
        lay.push("gaze", pos=[1, 2, 3], block="stone", dist=3.0)
    assert len(lay.q) == 1, f"去重失效: {len(lay.q)}"

    # ③ 预算：一分钟最多 MAX_PER_MIN 次
    lay2 = AiLayer(FakeAgent())
    got = 0
    for i in range(AiLayer.MAX_PER_MIN + 5):
        lay2.push("say_text", text=f"第{i}句")
        if lay2.next_event() is not None:
            got += 1
    assert got <= AiLayer.MAX_PER_MIN, f"预算失效: {got}"

    # ④ 没有模型时必须"明确不可用"，且不发请求
    class NoModel:
        has_model = False

        def run(self, *a, **k):
            raise AssertionError("没有模型却调用了")

    lay3 = AiLayer(NoModel())
    lay3.push("say_text", text="hi")
    assert lay3.enabled() is False and lay3.next_event() is None, "无模型时不应取事件"
    # ⑤ 成本护栏：超预算 ⇒ 只降级，不再调模型；并记账
    class BudgetAgent:
        has_model = True

        class speech:
            @staticmethod
            def say(*a, **k):
                return True

        def run(self, goal, **kw):
            return {"ok": True, "say": "好", "steps": [],
                    "tokens": {"total_tokens": 20}}

    lay_b = AiLayer(BudgetAgent(), daily_token_budget=30)
    ev1 = lay_b.push("say_text", text="第一句")
    lay_b.handle(ev1)
    assert lay_b.tokens_used >= 20, f"没记账: {lay_b.tokens_used}"
    ev2 = lay_b.push("say_text", text="第二句")
    got = lay_b.next_event()
    assert got is not None
    lay_b.handle(got)
    lay_b.push("say_text", text="第三句")
    assert lay_b.exhausted and lay_b.next_event() is None, "超预算后仍在调模型"
    assert lay_b.snapshot()["budget"] == 30
    return "模板/去重/预算/无模型降级/成本护栏 五项"


def AiEventProbe(kind: str):
    from companion.ai_layer import AiEvent
    return AiEvent(kind=kind, text="测试", data={"pos": [0, 64, 0], "block": "stone",
                                                 "dist": 3.0, "n": 2, "kind": "zombie",
                                                 "hp": 10})


# ── ⑮ 目标分解（operator：AI 不知道怎么弄时，用它自己的分解图） ──────────────
def check_goal_graph() -> str:
    from companion import goal_graph, hook

    class FakeReg:
        def invoke(self, action, args=None):
            class R:
                ok = True
                data = []
                err = ""
            return R()

    o_res, o_obt, o_task, o_chain = hook.resources, hook.obtain, hook.user_task, None
    try:
        hook.resources = lambda reg: ["oak_log", "wooden_sword", "stick"]
        # ① 它知道怎么弄到的 → 可行
        ok, why = goal_graph.can_obtain(FakeReg(), "oak_log")
        assert ok and "清单" in why, f"oak_log 应可行: {ok} {why}"
        # ② 它不知道怎么弄的 → 不可行（并说明原因，不许瞎试）
        ok2, why2 = goal_graph.can_obtain(FakeReg(), "diamond_sword")
        assert not ok2 and "清单" in why2, f"钻石剑应不可行: {ok2} {why2}"
        # ③ 分解：开始跑 + 读回任务树
        hook.obtain = lambda reg, item, count=1, **kw: {"ok": True}
        hook.user_task = lambda reg: "<Get 3 oak_log> <Chop 3 oak_log> <Mine block oak_log>"
        plan = goal_graph.decompose(FakeReg(), "oak_log", 3, wait_s=0.5)
        assert plan["ok"] and any("Chop 3 oak_log" in t for t in plan["tree"]), f"树没读到: {plan}"
        # ④ 不可行的目标：不开始、树为空、给出理由
        plan2 = goal_graph.decompose(FakeReg(), "diamond_sword", 1, wait_s=0.5)
        assert (not plan2["ok"]) and not plan2["tree"] and plan2.get("reason"), \
            f"不可行目标处理不对: {plan2}"
        blk = goal_graph.prompt_block(plan2)
        assert "不会做" in blk, f"prompt_block 没告诉模型做不到: {blk}"
        # ⑤ 自动降级：不可行但有替代 ⇒ 换掉，并且**必须在提示里要求告知**
        import companion.tasks as _tasks
        o_alt = _tasks.pick_alternative
        _tasks.pick_alternative = lambda reg, want: ("wooden_sword", [
            {"item": want, "ok": False, "why": "附近没有牛"},
            {"item": "wooden_sword", "ok": True, "why": "附近有 oak_log"}])
        try:
            plan3 = goal_graph.decompose(FakeReg(), "leather_chestplate", 1, wait_s=0.5)
            assert plan3.get("substituted") and plan3["item"] == "wooden_sword", \
                f"没有自动替换: {plan3}"
            blk3 = goal_graph.prompt_block(plan3)
            assert "替换" in blk3 and "必须" in blk3, f"替换没要求告知: {blk3}"
        finally:
            _tasks.pick_alternative = o_alt
    finally:
        hook.resources = o_res
        hook.obtain = o_obt
        hook.user_task = o_task
    return "可行/不可行/树解析/提示块/自动替换必告知 五项"


# ── ⑯ 自动前置（operator：这部分可以自动化） ───────────────────────────────
def check_auto_plan() -> str:
    import companion.goal_graph as gg

    class FakeReg:
        def invoke(self, action, args=None):
            class R:
                ok = True
                data = []
                err = ""
            return R()

    calls = {"obtain": 0, "resources": 0}
    o_res, o_obt, o_task = gg.hook.resources, gg.hook.obtain, gg.hook.user_task
    gg._CACHE.clear()
    try:
        gg.hook.resources = lambda reg: (calls.__setitem__("resources", calls["resources"] + 1)
                                         or ["oak_log", "wooden_sword"])
        def _obt(reg, item, count=1, **kw):
            calls["obtain"] += 1
            return {"ok": True}
        gg.hook.obtain = _obt
        gg.hook.user_task = lambda reg: "<Get 1 oak_log> <Chop 1 oak_log>"
        # ① 明确要去做 → 真的开始 + 读回分解图
        p1 = gg.auto_plan(FakeReg(), "帮我弄点木头")
        assert p1 and p1["mode"] == "plan" and calls["obtain"] == 1, f"没自动开始: {p1}"
        assert p1.get("tree"), f"没拿到分解图: {p1}"
        # ② 只是提到（不是指令）→ 只查可行性，**不许动身体**
        before = calls["obtain"]
        p2 = gg.auto_plan(FakeReg(), "木头好砍吗")
        assert p2 and p2["mode"] == "feasible" and calls["obtain"] == before, \
            f"聊天里提一句就动手了: {p2}"
        # ③ 完全无关的话 → 不管
        assert gg.auto_plan(FakeReg(), "今天天气不错") is None
        # ④ 缓存：同一句话第二次不再扫清单
        r1 = calls["resources"]
        gg.auto_plan(FakeReg(), "帮我弄点木头")
        assert calls["resources"] == r1, "缓存失效（每次都扫清单）"
        # ⑤ AI 层真的把它塞进 prompt
        from companion.ai_layer import AiLayer

        class FakeAgent:
            has_model = True
            goals: list = []

            def run(self, goal, **kw):
                FakeAgent.goals.append(goal)
                return {"ok": True, "say": "好", "steps": []}

        lay = AiLayer(FakeAgent(), planner=lambda text: gg.auto_plan(FakeReg(), text))
        ev = lay.push("say_text", text="帮我弄点木头")
        lay.handle(ev)
        assert any("它自己的分解" in g for g in FakeAgent.goals), "分解图没进 prompt"

        # ⑥ 里程碑播报：长活 >30s、玩家 ≤8 格、全程 ≤2 句
        from companion.commander import Commander
        said = []

        class Sp:
            def say(self, text, **kw):
                said.append((text, kw.get("kind")))
                return True

        c = Commander.__new__(Commander)
        c.dry = False
        c.job = ("get:get_item", None)
        c.job_started_at = 0.0
        c.memory = {}
        c.speech = Sp()
        c.log = lambda o: None
        c.job_running = lambda: True
        obs = {"dist": 3.0}
        for t in (10.0, 35.0, 50.0, 95.0, 140.0):
            c.narrate(obs, t)
        assert len(said) == 2, f"播报次数不对: {said}"
        assert all(k == "narrate" for _, k in said), f"播报没打标记: {said}"
        # 玩家太远 ⇒ 不播
        said.clear()
        c.memory = {}
        c.narrate({"dist": 20.0}, 100.0)
        assert not said, "玩家在 20 格外还播报"
    finally:
        gg.hook.resources, gg.hook.obtain, gg.hook.user_task = o_res, o_obt, o_task
        gg._CACHE.clear()
    return "自动开始/只查不动手/无关跳过/缓存/进 prompt/播报限次 七项"


# ── ⑰ 启动器内核（类 PCL：实例/模组/设置/诊断）─────────────────────────────
def check_launcher_core() -> str:
    sys.path.insert(0, str(ROOT / "launcher"))
    import importlib
    core = importlib.import_module("core")
    r = core.selftest()
    bad = [c for c in r["checks"] if not c["ok"]]
    assert not bad, "; ".join(f"{c['name']}: {c['detail']}" for c in bad)
    return f"{len(r['checks'])} 项（实例/双 mods 目录/设置/启动命令/诊断包）"


# ── ⑰b 启动器 UI（Qt）：能建窗口、能认目录与版本 ────────────────────────────
def check_launcher_ui() -> str:
    import subprocess
    ui = ROOT / "launcher" / "ui" / "main.py"
    assert ui.exists(), "launcher/ui/main.py 不存在"
    r = subprocess.run([sys.executable, "-X", "utf8", str(ui), "--selftest"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0 and "selftest OK" in out, out[-300:]
    return out.strip().splitlines()[-1][:120]


# ── ⑲ 语音选型层（本地优先、永远兜底）──────────────────────────────────────
def check_voice_engine() -> str:
    from companion import voice_engine as VE, voice_io as V
    cfg = VE.load_cfg()
    tts, why_t = VE.pick_tts({**cfg, "tts": "auto", "cosyvoice_dir": ""})
    assert tts == "sapi" and "SAPI" in why_t or "sapi" in why_t.lower(), f"TTS 兜底不对: {tts} {why_t}"
    stt, why_s = VE.pick_stt({**cfg, "stt": "auto", "sherpa_model_dir": ""})
    assert stt in ("sapi", "sherpa"), f"STT 选型异常: {stt}"
    # 强制要 sherpa 但没装/没模型 ⇒ 必须明确退回 sapi 并给出原因（不许静默）
    stt2, why2 = VE.pick_stt({**cfg, "stt": "sherpa", "sherpa_model_dir": ""})
    assert stt2 == "sapi" and why2, f"缺模型时应兜底并说明: {stt2} {why2}"
    # 适配器存在；没有模型时构造必须**抛错**（由上层兜底），不能假装在听
    assert hasattr(V, "SherpaSTT") and hasattr(V, "CosyVoiceTTS")
    try:
        V.SherpaSTT("", log=None)
        raise AssertionError("没有模型却构造成功了")
    except Exception as e:
        assert "模型" in str(e) or "sherpa" in str(e).lower() or "依赖" in str(e), str(e)
    # 重采样（设备 44.1k → 模型 16k）
    import numpy as _np
    y = V._resample_linear(_np.zeros(44100, dtype="float32"), 44100, 16000)
    assert len(y) == 16000, f"重采样长度不对: {len(y)}"
    # 设备解析：索引 / 名字片段 / 不存在
    assert V.resolve_input_device(1) == 1
    assert V.resolve_input_device("不存在的设备") is None
    cfg2 = VE.load_cfg()
    for k in ("input_device", "silence_chunks", "vad_threshold"):
        assert k in cfg2, f"config/voice.json 缺 {k}"
    return f"TTS={tts} STT={stt}（兜底/重采样/设备 均通过）"


# ── ⑳b 诚实度（离线管道：不可行必须直说、可行必须给分解、失败必须带原因）──
def check_honesty() -> str:
    import subprocess
    p = ROOT / "tools" / "honesty_probe.py"
    r = subprocess.run([sys.executable, "-X", "utf8", str(p)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0 and "PASS" in out, out[-400:]
    return out.strip().splitlines()[-1][:80]


# ── ⑳c 崩溃留证（自己出事也要留现场）───────────────────────────────────────
def check_incident() -> str:
    from companion import incident
    try:
        raise RuntimeError("gate 故意抛的异常")
    except RuntimeError as e:
        d = incident.capture(e, where="gate_selftest", extra={"k": 1})
    assert d and (d / "error.txt").exists() and (d / "env.json").exists(), f"没留证: {d}"
    txt = (d / "error.txt").read_text(encoding="utf-8")
    assert "RuntimeError" in txt and "gate_selftest" in txt
    return f"{d.name}（error/env/README 齐）"


# ── ⑰c 更新流水线（W2-3）＋ ⑰d 取模型（W2-2）──────────────────────────────────
def _run_tool_json(rel: str, timeout: float = 300.0, extra: list[str] | None = None) -> dict:
    import subprocess as sp
    r = sp.run([sys.executable, "-X", "utf8", str(ROOT / rel), "--json"] + (extra or []),
               capture_output=True, text=True, encoding="utf-8", errors="replace",
               timeout=timeout)
    out = (r.stdout or "").strip()
    # ⚠️ 2026-10-30 血证：不能只看**最后一行**。有些探针会先把指挥层的事件 JSON 也打到 stdout
    # （`{"ev": "say", ...}`），于是"最后一行"可能是条事件而不是结论 ⇒ 闸读到空结论判 FAIL。
    # 反过来扫：谁同时有 ts/ok/verdict/problems 之类的结论字段，就用谁。
    keys = ("verdict", "ok", "problems", "steps", "checks", "w6_4")
    for line in reversed(out.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and "ev" not in obj and any(k in obj for k in keys):
            return obj
    raise AssertionError(f"{rel} 没给出结论：{out[-200:]}{(r.stderr or '')[-300:]}")


def check_update_flow() -> str:
    rep = _run_tool_json("tools/_update_flow_test.py")
    assert rep.get("ok"), f"更新/回滚闭环失败：{rep.get('problems')}"
    return "发布/检查/更新/sha 判据/回滚 五项通过（同名同大小不同内容也会报更新）"


def check_model_fetch() -> str:
    rep = _run_tool_json("tools/_model_fetch_test.py")
    assert rep.get("ok"), f"取模型链路失败：{rep.get('problems')}"
    meta = _read_release_meta()
    return ("下载/续传/sha 正例/sha 反例 四项通过"
            + (f"；已发布 v{meta.get('version')}" if meta.get("version") else "；发布目录为空"))


def _read_release_meta() -> dict:
    try:
        return json.loads((ROOT / "_releases" / "latest.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── ⑲b 自听闸（W1-1：它不许听见自己说话）──────────────────────────────────────
def check_self_listen() -> str:
    """跑真探针（不是复述逻辑）：静音期内必须有 stt_muted、零泄漏；拆掉闸必须变红。"""
    import importlib.util
    p = ROOT / "tools" / "_self_listen_probe.py"
    spec = importlib.util.spec_from_file_location("_self_listen_probe", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    d = ROOT / "runs" / "gate"
    d.mkdir(parents=True, exist_ok=True)
    rep = mod.run("sim", 5.0, d / "selflisten.jsonl")
    assert rep["verdict"] == "PASS", f"自听闸没合上：{rep['checks']} 泄漏={rep['leaked']}"
    assert rep["muted_events"] >= 1, "说话期间一条 stt_muted 都没有"
    assert not rep["leaked"], f"说话期间被识别回来：{rep['leaked']}"
    assert rep["checks"]["mic_reopened"], "说完后麦克风没放开（永久静音也是哑）"
    # 反向对照：把闸拆掉，探针**必须**变红 —— 否则它只是个永远绿的灯
    rep2 = mod.run("sim", 5.0, d / "selflisten-broken.jsonl", break_mute=True)
    assert rep2["verdict"] == "FAIL", "拆掉静音闸后探针仍然 PASS（探针没牙）"
    return (f"静音 {rep['muted_events']} 次 / 泄漏 0；"
            f"拆闸对照 FAIL（泄漏 {len(rep2['leaked'])} 条）")


# ── ⑲c 语音共享（W3-2：3 个陪玩共享一路麦克风/喇叭）────────────────────────────
def check_voice_hub() -> str:
    from companion import voice_hub as H
    r = H.selftest()
    assert r["ok"], f"共享失败：{r['problems']}"
    assert r["hub_heard"] == 3, f"hub 只收到 {r['hub_heard']} 句"
    assert r["hub_spoken"] >= 1, "TTS 共享没生效（hub 没读 outbox）"
    per = r["per_client"]
    assert all(v == 3 for v in per.values()), f"客户端收数不对：{per}"
    return f"1 路麦克风 → {len(per)} 个客户端各 3 句；点名/截断自愈 均通过"


# ── ⑳d 支持闭环（W4-1：开关→打包→真 POST→服务端真收到）──────────────────────
def check_support_loop() -> str:
    import subprocess as sp
    r = sp.run([sys.executable, "-X", "utf8", str(ROOT / "tools" / "_support_loop_test.py"),
                "--json"], capture_output=True, text=True, encoding="utf-8",
               errors="replace", timeout=180)
    line = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else "{}"
    try:
        rep = json.loads(line)
    except Exception:
        raise AssertionError(f"闭环回归没给出结论：{(r.stdout or '')[-200:]}{(r.stderr or '')[-300:]}")
    assert rep.get("ok"), f"闭环失败：{rep.get('problems')}"
    assert not rep.get("support_enabled_after"), "跑完没把开关还原成关（产品承诺是不默认上报）"
    return f"打包→POST→落盘 sha256 一致；开关已还原（enabled={rep['support_enabled_after']}）"


# ── ㉑ 活体能力面（可选） ────────────────────────────────────────────────────
def port_open(port: int = 25351, host: str = "127.0.0.1") -> bool:
    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def check_live() -> str:
    from companion.body import Body
    # ⚠️ settings 不能传空串：Body 会把它当 Path('.') 然后 PermissionError（2026-10-29 踩过）
    cand = [Path(r"%BUDDY_CLIENT%"),
            ROOT / "runs" / "gate" / "altoclef_settings.json"]
    settings = next((str(p) for p in cand if p.exists()), None)
    assert settings, "找不到任何 altoclef_settings.json（无法取 Bearer token）"
    b = Body(25351, settings)
    tools = b.tools(refresh=True)
    names = [t.get("name") for t in tools if isinstance(t, dict)]
    assert names, "读不到工具清单"
    assert len(names) >= 80, f"工具只有 {len(names)} 个（应 ≥80）"
    return f"{len(names)} 个工具（含 {sum(1 for n in names if n in ('obtainItem','listCommands','runCommandText','takeControl'))}/4 个 hook）"


# ── ㉒ RPC 批量化（W6-4）：一个全量 tick 花几次往返、多少毫秒 ──────────────────
def check_rpc_batch() -> str:
    """不是复述设计，而是**真的量**：stateDigest 三个工具在不在 + 全量观察 p95/RPC 数。"""
    from companion.body import Body
    settings = str(Path(r"%BUDDY_CLIENT%"))
    names = set(Body(25351, settings).tool_names())
    need = {"stateDigest", "getBlocksBatch", "raycastFromPlayer"}
    assert need <= names, f"缺批量化工具：{sorted(need - names)}"
    rep = _run_tool_json("tools/reflex_latency.py", timeout=240)
    w = rep.get("w6_4") or {}
    assert w.get("ok"), (f"全量观察没达标：p95={w.get('full_p95_ms')}ms"
                         f"（折算预算 {w.get('budget_ms')}，单次往返 {w.get('base_rpc_ms')}ms）"
                         f"RPC={w.get('rpc_max')} 次（预算 {w.get('budget_rpc')}）")
    r2a = rep.get("request_to_action_ms") or {}
    return (f"全量观察 p95 {w['full_p95_ms']}ms / **{w['rpc_max']} 次 RPC**"
            f"（单次往返 {w['base_rpc_ms']}ms）；请求→动作 p95 {r2a.get('p95')}ms；"
            f"工具 88 个含 3 个批量化")


# ── ㉓ 看不见但能干活的客户端（W6-3）：屏外窗口 + 渲染帧仍在跑 ──────────────
# 背景（实测结论，别改回独立桌面）：`--desktop Buddy` 的客户端**渲染主循环会停** ——
# 准星 `crosshairTarget` 冻住 ⇒ tungsten 的瞄准/挖掘全废。正解是 `--offscreen`
# （默认桌面 + 屏幕外留 1px）：看不见，但渲染照常。这条判据就守这个。
def check_hidden_desktop() -> str:
    rep = _run_tool_json("tools/_hidden_desktop_check.py", timeout=180,
                         extra=["--port", "25353",
                                "--settings", r"%BUDDY_CLIENT%",
                                "--desktop", "Default", "--expect-offscreen"])
    assert rep.get("verdict") == "PASS", f"屏外客户端没达标：{rep.get('checks')}"
    win = rep["checks"].get("①b窗口在屏外（留 1px）") or {}
    aim = rep["checks"].get("③渲染帧在跑（准星随瞄准变化）") or {}
    if aim.get("ok") is None:
        aim_txt = f"渲染判定 SKIP（{aim.get('skip_reason')}；读数 {aim.get('reads')}）"
    else:
        aim_txt = f"准星随瞄准变化（{aim.get('distinct')}）⇒ 渲染帧在跑"
    return f"窗口在屏外 {win.get('rect')}；{aim_txt}"


# ── ㉔ 打包（W2-1）：dist\\Buddy.exe 能起界面、能跑"一键准备"六步 ──────────────
def check_packaged_exe() -> str:
    import subprocess as sp
    exe = ROOT / "dist" / "Buddy.exe"
    assert exe.exists(), "dist\\Buddy.exe 不存在（先跑 pyinstaller 那条命令）"
    r = sp.run([str(exe), "--selftest"], capture_output=True, text=True, encoding="utf-8",
               errors="replace", timeout=240)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0 and "selftest OK" in out, f"exe 起不来：{out[-300:]}"
    size_mb = round(exe.stat().st_size / 1e6, 1)
    return f"{exe.name} {size_mb}MB；--selftest OK（打包态 ROOT/解包/--run-py 通道可用）"


# ── ㉕ 三陪玩同时在线（W3-1）：读最近一次 _three_live 的判据 ──────────────────
def check_three_live() -> str:
    d = ROOT / "runs" / "three_live"
    reps = sorted(d.glob("*/report.json")) if d.exists() else []
    assert reps, "还没有任何三实例报告（跑 tools/_three_live.py）"
    rep = json.loads(reps[-1].read_text(encoding="utf-8"))
    assert rep.get("verdict") == "PASS", f"三实例判据没过：{rep.get('failed')}"
    c = rep.get("checks") or {}
    return (" / ".join(f"{k}✓" for k, v in c.items() if v.get("ok")) +
            f"  （{reps[-1].parent.name}）")


def check_place_retry() -> str:
    """㉖ 放置的 **4-tick 限速兜底**（2026-10-30 的坑，用假 registry 单测，不碰真身体）。

    Java 侧 `BlockPlaceHelper.tryPlace` 每 4 tick 只允许放一次；被限速时回 `sent:false`，
    而上一层的 `placeBlockAt` 已经把"瞄准成功"当成 ok ⇒ 一次限速就等于"这一拍没放"。
    实测症状：连着放两格，第二格 `ok:false, sent:false, cooldown:true`，目标格一直是 air。

    这条判据守三件事：
      ① 限速（没 placed 且无永久原因）必须**重试**，重试成功要返回 ok=True；
      ② 几何/背包/保护类失败必须**立刻返回**（tries==1），不许拿 8 次去撞墙；
      ③ 重试用尽要如实 ok=False 且带上原因。
    """
    from companion import actions

    class _R:
        def __init__(self, ok, data=None, err=""):
            self.ok, self.data, self.err = ok, data or {}, err

    class _Reg:
        """按脚本回话的假 registry：第 1 次限速、第 2 次成功。"""
        def __init__(self, script):
            self.script, self.calls = list(script), 0

        def invoke(self, name, args=None):
            self.calls += 1
            step = self.script[min(self.calls - 1, len(self.script) - 1)]
            return _R(True, step)

    # ① 限速 → 重试 → 成功
    reg = _Reg([{"ok": False, "sent": False, "cooldown": True, "placed": False},
                {"ok": True, "sent": True, "placed": True, "support": "-1,63,0"}])
    r = actions.place_block(reg, 0, 64, 0, tries=4, gap=0.0)
    assert r.get("ok") and r.get("tries") == 2, f"限速没重试：{r}"
    assert reg.calls == 2, f"调用次数不对：{reg.calls}"

    # ② 永久失败 → 一次就收手
    reg2 = _Reg([{"ok": False, "reason": "no reachable supporting face"}])
    r2 = actions.place_block(reg2, 0, 64, 0, tries=8, gap=0.0)
    assert (not r2.get("ok")) and r2.get("tries") == 1, f"永久失败却重试了：{r2}"
    assert reg2.calls == 1, f"永久失败调用了 {reg2.calls} 次"

    # ③ 一直限速 → 用尽后如实 False
    reg3 = _Reg([{"ok": False, "sent": False, "cooldown": True}])
    r3 = actions.place_block(reg3, 0, 64, 0, tries=3, gap=0.0)
    assert (not r3.get("ok")) and r3.get("tries") == 3 and r3.get("why"), f"用尽后没如实报：{r3}"

    # ④ place_looking 走同一套分流（空手必须立刻收手，不重试）
    reg4 = _Reg([{"ok": False, "reason": "held item is not a block (minecraft:air)"}])
    r4 = actions.place_looking(reg4, tries=5, gap=0.0)
    assert (not r4.get("ok")) and reg4.calls == 1, f"空手却重试了：{r4} / {reg4.calls} 次"

    return (f"限速重试→{r['tries']} 次成功；永久失败 1 次收手；用尽后如实报；"
            f"空手不重试（{reg4.calls} 次）")


def check_place_body_blocked() -> str:
    """㉗ 「自己的身体压着目标格」必须被认出来，而且**能自救**（2026-10-30 第二个坑）。

    现场血证（buddy14→15，格子 (65,66,-75)、身体 (65.0,66.0,-74.0)）：连打四次全是
    `sent:false`，当时的分类把这种"client 自己拒绝"写成 `cooldown`（限速），于是外面看到的是
    "放置限速八次也不动"。真因是玩家碰撞箱 x[64.7,65.3] z[-74.3,-73.7] 与目标格
    x[65,66] z[-75,-74] 有 0.3 宽重叠 —— 原版不许把实心方块放进玩家身体里。挪开一格
    （64.7,67.8,-71.3）同一格第一次就成。这正是老 mineflayer 栈 `recenter()` 治的那条。

    这条判据守四件事：
      ① `body_blocked` ≠ `permanent`：它是"站位问题"，不是"这格不能放"；
      ② 退半步之后必须真的重试并放上（不能把站位问题当死局）；
      ③ 一个方向退不动要**换反方向**再试（实测一侧常有墙/水）；
      ④ 两个方向都退不动 ⇒ 立刻老实收手，不许拿 8 次去撞同一堵墙。
    """
    from companion import actions

    class _R:
        def __init__(self, ok, data=None, err=""):
            self.ok, self.data, self.err = ok, data or {}, err

    class _Reg:
        """像真的：位置只在 gotoXYZ 之后才变；`blocked_n` 次之后才放得上。"""
        def __init__(self, blocked_n: int, can_move: bool = True):
            self.blocked_n, self.n, self.can_move = blocked_n, 0, can_move
            self.x, self.z, self.calls = 10.5, 10.5, []

        def invoke(self, name, args=None):
            self.calls.append(name)
            if name == "placeBlockAt":
                self.n += 1
                if self.n <= self.blocked_n:
                    return _R(True, {"ok": False, "body_blocked": True, "permanent": False,
                                     "reason": "my own hitbox covers that cell — step aside"})
                return _R(True, {"ok": True, "sent": True, "placed": True})
            if name == "getGameState":
                return _R(True, {"self": {"pos": "%.3f,64.0,%.3f" % (self.x, self.z)}})
            if name == "gotoXYZ":
                if self.can_move:
                    self.x, self.z = args["x"], args["z"]
                return _R(True, {"ok": True})
            return _R(True, {})

    # ① 压一次 → 退开 → 放成（tries==2，正好"退一次 + 放一次"）
    g = _Reg(1)
    r = actions.place_block(g, 11, 64, 10, tries=6, gap=0.0)
    assert r.get("ok") and r.get("tries") == 2, f"退开后没放上：{r}"

    # ② 退一次还不够 → 反方向再退 → 放成
    g2 = _Reg(2)
    r2 = actions.place_block(g2, 11, 64, 10, tries=6, gap=0.0)
    assert r2.get("ok") and r2.get("tries") == 3, f"两次退开没放上：{r2}"
    assert g2.calls.count("gotoXYZ") == 2, f"只退了一次：{g2.calls.count('gotoXYZ')}"

    # ③ 一直压着 → 退两次后认输，且**真实原因**要留下
    g3 = _Reg(99)
    r3 = actions.place_block(g3, 11, 64, 10, tries=6, gap=0.0)
    assert (not r3.get("ok")) and r3.get("body_blocked"), f"认输时没标 body_blocked：{r3}"
    assert g3.calls.count("placeBlockAt") == 3, f"烧了 {g3.calls.count('placeBlockAt')} 次"

    # ④ 退不动（寻路失败/被卡住）→ 立刻收手，别假装重试
    g4 = _Reg(99, can_move=False)
    r4 = actions.place_block(g4, 11, 64, 10, tries=6, gap=0.0)
    assert (not r4.get("ok")) and r4.get("body_blocked"), f"退不动时没标 body_blocked：{r4}"
    assert g4.calls.count("placeBlockAt") <= 2, f"退不动还硬试 {g4.calls.count('placeBlockAt')} 次"

    return (f"压住→退开→{r['tries']} 次放成；反方向退开→{r2['tries']} 次放成；"
            f"一直压着 3 次收手；退不动 ≤2 次收手")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--require-live", action="store_true",
                    help="MCP 25351 不在时判失败（发布前用）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    t0 = time.time()
    print("[gate] 陪玩指挥层离线回归闸", flush=True)
    check("① 模块可导入", check_imports)
    check("② 能力表 schema", check_capabilities)
    check("③ 规则规划器", check_planner)
    check("④ 礼仪闸", check_guards)
    check("⑤ 自然语言", check_nlu)
    check("⑥ 说话礼貌", check_speech)
    check("⑦ 队伍规则", check_team)
    check("⑧ 角色闸", check_role)
    check("⑨ 任务层", check_tasks)
    check("⑩ 终局判据统计", check_scoring)
    check("⑪ 反射弧", check_reflex)
    check("⑫ 变通（可行性阶梯）", check_adaptivity)
    check("⑭ AI 交互层", check_ai_layer)
    check("⑮ 目标分解", check_goal_graph)
    check("⑯ 自动前置", check_auto_plan)
    check("⑰ 启动器内核", check_launcher_core)
    check("⑰b 启动器 UI(Qt)", check_launcher_ui)
    check("⑰c 更新流水线", check_update_flow)
    check("⑰d 取模型", check_model_fetch)
    check("⑲ 语音选型", check_voice_engine)
    check("⑲b 自听闸", check_self_listen)
    check("⑲c 语音共享", check_voice_hub)
    check("⑳b 诚实度", check_honesty)
    check("⑳c 崩溃留证", check_incident)
    check("⑳d 支持闭环", check_support_loop)
    live = port_open()
    check("㉑ 活体能力面（MCP 25351）", check_live,
          skip="" if live else ("端口不在，跳过" if not a.require_live else ""))
    check("㉒ RPC 批量化（W6-4）", check_rpc_batch,
          skip="" if live else ("身体不在，跳过" if not a.require_live else ""))
    live2 = port_open(25353)
    check("㉓ 屏外客户端仍渲染（W6-3）", check_hidden_desktop,
          skip="" if live2 else ("Buddy3 不在，跳过" if not a.require_live else ""))
    check("㉔ 打包 exe（W2-1）", check_packaged_exe,
          skip="" if (ROOT / "dist" / "Buddy.exe").exists() else "dist\\Buddy.exe 还没打")
    check("㉖ 放置限速兜底", check_place_retry)
    check("㉗ 放置被自己身体挡住", check_place_body_blocked)
    check("㉕ 三陪玩在线（W3-1）", check_three_live,
          skip="" if (ROOT / "runs" / "three_live").exists() else "还没跑过 _three_live")

    fails = [r for r in RESULT if r["ok"] is False]
    skips = [r for r in RESULT if r["ok"] is None]
    verdict = "PASS" if not fails and not (a.require_live and skips) else "FAIL"
    out = {"ts": time.strftime("%Y%m%d-%H%M%S"), "verdict": verdict,
           "sec": round(time.time() - t0, 2), "results": RESULT,
           "fails": len(fails), "skips": len(skips)}
    d = ROOT / "runs" / "gate"
    d.mkdir(parents=True, exist_ok=True)
    (d / "last.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    print(f"\n[gate] {verdict}  通过 {len(RESULT) - len(fails) - len(skips)}"
          f" / 失败 {len(fails)} / 跳过 {len(skips)}   {out['sec']}s", flush=True)
    print(f"[gate] 记录: {d / 'last.json'}", flush=True)
    if a.json:
        print(json.dumps(out, ensure_ascii=False))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
