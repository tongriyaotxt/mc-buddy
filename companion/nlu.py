#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/nlu.py —— 「人话 → 意图」：指令化输入和自然输入走同一条路

operator 2026-10-26：「可指令化输入也可自然输入」。
所以这里不区分两种模式 —— **所有输入都先过这里**，只是指令化输入命中的置信度高而已：

    输入                    解析结果
    "b 挖木头"           → {kind:"get", item:"oak_log", count:3, conf:1.0}
    "帮我砍三根木头"      → {kind:"get", item:"oak_log", count:3, conf:0.9}
    "跟上我"             → {kind:"follow", conf:0.9}
    "把木镐给我"          → {kind:"supply", item:"wooden_pickaxe", conf:0.9}
    "过来一下"            → {kind:"teleport", conf:0.8}
    "看那块石头，挖了"    → {kind:"mine", use_look:true, conf:0.8}
    "别跟了"             → {kind:"stop", conf:0.9}
    "你叫什么"            → {kind:"ask", what:"name", conf:0.9}
    "记住我喜欢木头房子"   → {kind:"remember", text:"我喜欢木头房子", conf:0.9}
    "今天天气不错"        → {kind:"chat", conf:0.2}

设计取舍
--------
· **纯规则、零依赖、毫秒级**。它每秒都在跑（chat 轮询），不能是个网络调用。
· **只管"要做什么"，不管"怎么做"**。动作怎么做是 `actions.py` 的事。
· **拿不准就低置信度**（`conf` 低），由上层决定"问一句"还是"当闲聊"。
· 中文物品名走一张小词典（`ITEMS`）；查不到就原样当英文 id 传下去 —— 宁可让 AltoClef
  报"没有这个东西"，也不要自己瞎猜成另一个物品。
"""
from __future__ import annotations

import re

# ── 中文/口语物品名 → minecraft id ──────────────────────────────────────────
ITEMS = {
    "木头": "oak_log", "原木": "oak_log", "木材": "oak_log", "树干": "oak_log",
    "木板": "oak_planks", "木板子": "oak_planks",
    "木棍": "stick", "棍子": "stick",
    "木镐": "wooden_pickaxe", "木稿": "wooden_pickaxe",
    "石镐": "stone_pickaxe", "铁镐": "iron_pickaxe", "钻石镐": "diamond_pickaxe",
    "木剑": "wooden_sword", "石剑": "stone_sword", "铁剑": "iron_sword",
    "镐子": "pickaxe", "镐": "pickaxe", "剑": "sword", "斧头": "axe", "斧子": "axe",
    "石头": "stone", "圆石": "cobblestone", "石料": "cobblestone",
    "泥土": "dirt", "沙子": "sand", "沙": "sand",
    "煤": "coal", "煤炭": "coal", "铁": "iron_ingot", "铁锭": "iron_ingot",
    "金子": "gold_ingot", "金锭": "gold_ingot", "钻石": "diamond",
    "吃的": "cooked_mutton", "食物": "cooked_mutton", "肉": "cooked_mutton",
    "熟肉": "cooked_mutton", "面包": "bread", "苹果": "apple",
    "火把": "torch", "工作台": "crafting_table", "台子": "crafting_table",
    "箱子": "chest", "床": "bed", "桶": "bucket", "水桶": "water_bucket",
    "铁锹": "iron_shovel", "锹": "shovel", "铲子": "shovel",
    "弓": "bow", "箭": "arrow", "盾": "shield",
    "羊毛": "white_wool", "玻璃": "glass",
}

# 数量词
NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
       "八": 8, "九": 9, "十": 10, "半": 1, "几": 3}
NUM_RE = r"(\d+|[一二两三四五六七八九十半几])"

# 意图关键词（顺序 = 优先级，先命中先返回）
RULES: list[tuple[str, str]] = [
    ("stop",      r"停下|别动|不要动|等着|待着|别跟|不用了|算了|回来吧|站住"),
    ("teleport",  r"传送|过来一下|过来吧|来我这|到我身边|召唤|过来|来一下"),
    ("remember",  r"记住|记一下|别忘了|记得我"),
    ("ask",       r"你叫什么|你叫啥|你是谁|你在哪|你几岁|你会什么|你能做什么"),
    ("supply",    r"给我|递我|丢给我|扔给我|送你|拿着|帮我拿|把.*(给|递)"),
    ("food",      r"吃饭|吃点|饿了|给我点吃的|弄点吃的"),
    ("hero",      r"打怪|清怪|有怪|保护我|救我|怪物|杀掉|揍它|打死"),
    ("build",     r"建|盖|搭|墙上|放个|铺|做面墙|填"),
    ("follow",    r"跟上|跟着我|跟我走|跟着|过来跟"),
    ("mine",      r"挖这块|挖掉这个|挖它|把这块挖|这块挖了|盯着的那块"),
    ("get",       r"挖|砍|采集|弄点|搞点|要.*个|找.*个|来点|弄|搞"),
    ("look",      r"看那块|看这个|看那边"),
    ("chat",      r".*"),
]


def _strip(text: str) -> str:
    t = re.sub(r"§.", "", text or "")
    t = re.sub(r"^<[^>]+>\s*", "", t)          # <Xiaorun> ...
    t = re.sub(r"^\[[^\]]+\]\s*", "", t)       # [CHAT] ...
    return t.strip()


def find_item(text: str) -> str | None:
    """先查长词（"木镐"优先于"镐"），再查英文 id，最后查任意 a-z_ 词。"""
    for zh in sorted(ITEMS, key=len, reverse=True):
        if zh in text:
            return ITEMS[zh]
    m = re.search(r"\b([a-z_]{3,})\b", text.lower())
    if m and m.group(1) not in ("the", "and", "you", "for", "with", "please", "buddy"):
        return m.group(1)
    return None


def find_count(text: str, default: int = 1) -> int:
    m = re.search(NUM_RE + r"\s*(个|根|块|把|份|桶|组|只|条|张|支)", text)
    if m:
        g = m.group(1)
        return int(g) if g.isdigit() else NUM.get(g, default)
    m = re.search(NUM_RE, text)
    if m:
        g = m.group(1)
        return int(g) if g.isdigit() else NUM.get(g, default)
    return default


def is_addressed(text: str, me: str = "buddy", player: str = "") -> bool:
    """这句话是不是对我说的。

    零聊天取消后，玩家聊天里什么都有；全当指令会把闲聊也执行了。
    判据（任一即可）：有前缀 / 提到我的名字 / 有祈使词 / 直接叫玩家名字的方式。
    """
    t = (text or "").lower()
    if not t.strip():
        return False
    if re.match(r"^(b|buddy|byd|小跟班|跟班|助手)\b", t):
        return True
    if me.lower() in t:
        return True
    return bool(re.search(r"帮我|给我|你|跟着|过来|挖|砍|建|盖|打|传送|停下|记住|扔", t))


def parse(raw: str, *, me: str = "buddy", player: str = "") -> dict:
    """人话 → {kind, conf, ...args}。永不抛异常，永不返回 None。"""
    text = _strip(raw)
    if not text:
        return {"kind": "chat", "conf": 0.0, "text": ""}

    conf = 0.6
    # 显式前缀 = 高置信
    m = re.match(r"^(?:b|buddy|byd|小跟班|跟班|助手)\s*[,，:：]?\s*(.*)$", text, re.I)
    if m:
        text = m.group(1).strip()
        conf = 1.0
        if not text:
            return {"kind": "ask", "what": "help", "conf": 1.0, "text": raw}

    low = text.lower()
    for kind, pat in RULES:
        if re.search(pat, text if kind != "chat" else low):
            if kind == "chat":
                return {"kind": "chat", "conf": 0.2, "text": text}
            out: dict = {"kind": kind, "conf": conf, "text": text}
            if kind in ("get", "supply", "mine"):
                item = find_item(text)
                if item:
                    out["item"] = item
                out["count"] = find_count(text, 1 if kind == "mine" else 3)
            if kind == "mine":
                out["use_look"] = True
            if kind == "build":
                item = find_item(text)
                if item:
                    out["block"] = item
            if kind == "remember":
                out["text"] = re.sub(r"^(记住|记一下|别忘了)[,，:：]?\s*", "", text).strip()
            if kind == "ask":
                out["what"] = ("name" if re.search(r"叫什么|叫啥", text)
                               else "where" if "在哪" in text else "help")
            if kind == "hero":
                out["watch"] = 30
            return out
    return {"kind": "chat", "conf": conf * 0.4, "text": text}


if __name__ == "__main__":                       # 自测：python -m companion.nlu
    samples = ["b 挖木头", "帮我砍三根木头", "跟上我", "把木镐给我", "过来一下",
               "挖掉这块", "别跟了", "你叫什么", "记住我喜欢木头房子", "今天天气不错"]
    for s in samples:
        print(f"{s:16s} -> {parse(s)}")
