#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/agent.py —— 会自己用工具的 agent（**DeepSeek 原生 tool calling**）

它是什么
--------
一句话进来 → 一串工具调用出去 → 一句人话收尾。和 `planner.AIPlanner` 的区别：
`AIPlanner` 每帧只做**一次**决策；agent 做的是**把一件事办完**（看 → 动 → 核验 → 报告）。

接谁
----
DeepSeek（operator 2026-10-27 定：模型用 DS）。端点/模型/密钥三处可配，优先级：

  1. 构造参数（launcher 面板填的）
  2. 环境变量 `BUDDY_AI_KEY` / `DEEPSEEK_API_KEY`、`BUDDY_AI_BASE`、`BUDDY_AI_MODEL`
  3. `config/ai.json`  →  `{"api_key": "sk-…", "base_url": "https://api.deepseek.com",
                            "model": "deepseek-flash"}`
  4. 都没有 ⇒ **不是报错，是降级**：NLU + 规则，仍然做动作、仍然回一句人话

AP 参考（2026-10-27 抓的官方文档）：
  · base_url `https://api.deepseek.com`，`POST /chat/completions`
  · 模型 `deepseek-flash` / `deepseek-v4-pro`（老名字 `deepseek-chat` 仍被兼容）
  · tools 是 OpenAI 形状；回复在 `choices[0].message.tool_calls[].function.{name,arguments}`
  · 思考模式 `thinking:{type:"enabled"}` + `reasoning_effort`；**思考模式下 tool_choice 不能用
    required/具名**，所以这里只用 `auto`
  · `usage` 带 prompt/completion/total 与 cache 命中数 ⇒ 每轮都记账

两条硬约束（都是本项目血证）
----------------------------
1. **单瞄准者**（tungsten `PathExecutor:853`）：长活（挖/取/盖/清怪/贴人）必须交给指挥层的
   作业池串行执行，agent **不许**自己并发下发两个会抢准星的动作。所以长活走 `runner` 回调。
2. **说话要体面**：agent 的收尾话一律经 `speech.py`（限流/去重/截断），它不直接调 ChatMessage。

自测（不依赖网络/模型）
-----------------------
    python -X utf8 tools/_fake_llm.py --selftest     # 脚本化端点 + 一整轮工具循环
    python -X utf8 -m companion.agent --show-config   # 只看配置解析结果
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import actions, nlu
from .body import as_obj, truthy
from .registry import Registry

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "ai.json"

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

# 会长时间占用身体 / 改变世界的动作：**只能**经指挥层作业池（runner）串行执行
MACRO_KINDS = ("mine", "get", "supply", "build", "hero", "selfcare", "follow_player",
               "teleport")

# 绝对不给模型的能力：换服务器、跑自定义任务序列（会把陪玩带到别的服务器/不可预期状态）
NEVER = {"connect", "custom2"}

# 首发给模型的工具（**顺序就是优先级**）。2026-10-27 实测：把 48 个工具的 schema 全塞进去，
# 一次对话光 prompt 就 5.2k tokens；而模型真正用得上的其实就这十几个，
# 剩下的（选择框/建造队列/选区填充/录制回放…）留个 `run_command` 就够。
PREFERRED = (
    # HOOK 层优先：这些是"让 AltoClef 自己干"的入口（别自己重造合成/容器/交付）
    "listCommands", "obtainableItems", "obtainItem", "runCommandText", "takeControl",
    "userTask",
    "getGameState", "playerDetail", "getEntities", "scanBlocks", "getBlockAt",
    "getBlocksAround", "inventorySpace", "getOpenScreen", "pathStatus", "taskChain",
    "canReach", "lookAt", "lookAtPlayer", "gotoXYZ", "mineStatus", "mineBlock",
    "placeBlockAt", "dropItem", "gesture", "getRecentChat", "ExecuteCommand",
    "inGame", "closeScreen",
)

SYSTEM = """你是 Minecraft 里的陪玩，名字叫 {bot}，跟着 {player} 一起玩。你不是助手，是**搭子**。

## 怎么说话（比干什么更重要）
- 一两句，**聊天框不是文档**：不分点、不加标题、不做总结。
- 像朋友随口说话：短句、口语，可以有语气词（啊/诶/哈/吧），偶尔吐槽一句。
- 用称呼（记忆里怎么叫就怎么叫），偶尔叫一次就够，别每句都带。
- **绝不要客服腔**：不说"您好""请问""我可以帮您""作为一个…""建议您""希望对你有帮助"。
- **不要解释自己怎么工作**：不说"我正在调用工具""根据我的分析""我将执行"。
- **也不要暴露内部结构**：不说"指挥层""接口""系统""端点""模型"这类词 —— 你就是个人。
- 不要复述玩家的问题，不要播报流程，不要说"任务完成"。
- 做不到就直接说，别拐弯；能带一句原因更好（没材料 / 够不着 / 没看见）。
- 可以有点小得意、小情绪，但别演；不要堆感叹号和波浪号。

好例子：
  玩家：帮我砍三根木头 → 「行，砍完就回来」/「去了，等我一会儿」
  玩家：你在哪 → 「在你左边那棵树下」
  玩家：把这块挖了 → 「挖了，这块是石头」
  玩家：我饿了 → 「我这儿没吃的，得先去弄点」
  玩家：你叫什么 → 「{bot}啊，你的陪玩」
坏例子（不要这样）：
  「好的，我将为您砍伐三根树木，请稍候。」
  「根据当前状态分析，我建议您先…」
  「任务已完成。成功采集 oak_log x3。」

## 怎么干活
0. **先问它会不会做**：凡是"弄到/做/造/建/弄一套 X""帮我搞点 X"，**第一步调 `plan_item`** ——
   它给的是这座身体自己的资源依赖图（木头→木板→棍→台子→镐→挖铁→烧…）。
   如果 plan_item 说"不在可获取清单里"，就**直接告诉玩家做不到**（带一句原因），不要用底层工具瞎试。
1. 先看清（getGameState / playerDetail / getEntities / scanBlocks / getBlockAt），再动手。
2. 一次只做一件事。**不要**同时下发两个会动手的动作（会互相抢准星，两个都做不成）。
3. 长活（挖一块 / 要某样东西 / 盖东西 / 清怪 / 跟过去 / 递东西）用 `macro_*` 工具，
   它们会跑到底并替你核验。不要用底层的 mineBlock/gotoXYZ 去重复它们。
4. 动完**必须核验**（方块变没变、背包多没多、距离近没近），核验失败就当没做成，换法子或认输。
5. 记忆里的称呼、喜好、你答应过的事要用上，但别生硬复述。
6. 玩家说「这块 / 面前这块 / 那个方块」时，说的就是他正对着的东西 ——
   状态里的 `player_looking_at.pos` 就是它，**直接拿来用，不要猜坐标**。

收尾：办完（或确认办不成）就回一句 {bot} 会说的话，然后不要再调工具。
"""


# ── 配置 ───────────────────────────────────────────────────────────────────
def load_config(path: Path | str | None = None) -> dict:
    """端点/模型/密钥。空字符串一律视为"没配"。"""
    cfg = {"base_url": "", "model": "", "api_key": ""}
    p = Path(path) if path else CONFIG
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update({k: str(raw.get(k) or "") for k in cfg})
    except Exception:
        pass
    env = os.environ
    cfg["api_key"] = (cfg["api_key"] or env.get("BUDDY_AI_KEY")
                      or env.get("DEEPSEEK_API_KEY") or "")
    cfg["base_url"] = (cfg["base_url"] or env.get("BUDDY_AI_BASE") or DEFAULT_BASE)
    cfg["model"] = (cfg["model"] or env.get("BUDDY_AI_MODEL") or DEFAULT_MODEL)
    cfg["enabled"] = bool(cfg["api_key"])
    return cfg


def save_config(api_key: str = "", base_url: str = "", model: str = "",
                path: Path | str | None = None) -> Path:
    """把密钥写进 config/ai.json（面板"保存"按钮用它）。文件权限最小化。"""
    p = Path(path) if path else CONFIG
    p.parent.mkdir(parents=True, exist_ok=True)
    old = {}
    try:
        if p.exists():
            old = json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        old = {}
    old.update({k: v for k, v in (("api_key", api_key), ("base_url", base_url),
                                  ("model", model)) if v})
    p.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return p


# ── HTTP 通道（OpenAI 兼容 / DeepSeek 原生 tools） ───────────────────────────
class LLM:
    def __init__(self, base_url: str = DEFAULT_BASE, model: str = DEFAULT_MODEL,
                 api_key: str = "", *, timeout: float = 45.0, retries: int = 2,
                 thinking: bool = False, effort: str = "low", log=None):
        b = (base_url or DEFAULT_BASE).rstrip("/")
        self.url = b if b.endswith("/chat/completions") else b + "/chat/completions"
        self.model = model or DEFAULT_MODEL
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self.thinking = thinking
        self.effort = effort
        self.log = log or (lambda o: None)
        self.usage = {"prompt": 0, "completion": 0, "total": 0, "calls": 0, "cached": 0}

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             *, temperature: float = 0.4, max_tokens: int = 900) -> dict:
        payload: dict = {"model": self.model, "messages": messages,
                         "temperature": temperature, "max_tokens": max_tokens,
                         "stream": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"       # 思考模式下只允许 auto
        # ⚠️ 2026-10-27 现场血证（第一次联通就栽在这）：
        #    DeepSeek 的 `thinking` **默认是 enabled**。开了思考模式后，
        #    `max_tokens` 会先被 reasoning 吃掉 —— 请求返回 200、`usage` 有数，
        #    但 `message.content` 是**空字符串**，我们看起来就是"模型没有可用输出"。
        #    陪玩要的是 0.5~2 秒内一句人话，不需要长篇推理，所以默认**显式关掉**。
        if self.thinking:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.effort
        else:
            payload["thinking"] = {"type": "disabled"}

        last = ""
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(
                self.url, data=json.dumps(payload).encode(), method="POST",
                headers={"Content-Type": "application/json",
                         **({"Authorization": "Bearer " + self.api_key} if self.api_key else {})})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    resp = json.loads(r.read().decode("utf-8", "replace"))
                u = resp.get("usage") or {}
                self.usage["prompt"] += int(u.get("prompt_tokens") or 0)
                self.usage["completion"] += int(u.get("completion_tokens") or 0)
                self.usage["total"] += int(u.get("total_tokens") or 0)
                self.usage["cached"] += int((u.get("prompt_tokens_details") or {})
                                            .get("cached_tokens") or 0)
                self.usage["calls"] += 1
                return resp
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                last = f"HTTP {e.code} {body}"
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            except Exception as e:
                last = f"{type(e).__name__}: {e}"[:200]
                if attempt < self.retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                break
        self.log({"ev": "agent_llm_error", "err": last[:200], "model": self.model})
        return {}

    # ── 流式（SSE）：实时语音要用它 ──────────────────────────────────────────
    def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                    *, temperature: float = 0.5, max_tokens: int = 400,
                    first_token_timeout: float = 8.0):
        """边生成边吐：yield {type: delta|tool_calls|usage|done|error}。

        DeepSeek 的流式是标准 SSE（`data: {json}` … `data: [DONE]`），
        `delta.content` 是增量文本，`delta.tool_calls[i].function.arguments` 是**分片**的
        JSON 字符串（要按 index 拼接）。⚠️ 流式 + 思考模式时 `delta.reasoning_content`
        会先来一大段 —— 我们默认关掉思考（见 `chat()` 里的注释），否则首句会被推理拖死。
        """
        payload: dict = {"model": self.model, "messages": messages,
                         "temperature": temperature, "max_tokens": max_tokens,
                         "stream": True, "thinking": {"type": "disabled"}}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream",
                     **({"Authorization": "Bearer " + self.api_key} if self.api_key else {})})
        calls: dict[int, dict] = {}
        self.usage["calls"] += 1
        try:
            with urllib.request.urlopen(req, timeout=first_token_timeout) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    u = chunk.get("usage")
                    if u:
                        self.usage["prompt"] += int(u.get("prompt_tokens") or 0)
                        self.usage["completion"] += int(u.get("completion_tokens") or 0)
                        self.usage["total"] += int(u.get("total_tokens") or 0)
                    for ch in (chunk.get("choices") or []):
                        d = ch.get("delta") or {}
                        if d.get("content"):
                            yield {"type": "delta", "text": d["content"]}
                        for tc in (d.get("tool_calls") or []):
                            i = int(tc.get("index") or 0)
                            slot = calls.setdefault(i, {"id": "", "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
        except Exception as e:
            self.log({"ev": "agent_stream_error", "err": f"{type(e).__name__}: {e}"[:160]})
            yield {"type": "error", "err": f"{type(e).__name__}: {e}"[:160]}
            return
        if calls:
            yield {"type": "tool_calls",
                   "calls": [{"id": v["id"] or f"call_{i}", "name": v["name"],
                              "args": v["args"]} for i, v in sorted(calls.items())]}
        yield {"type": "usage", "usage": dict(self.usage)}
        yield {"type": "done"}


def _message_of(resp: dict) -> dict:
    try:
        return ((resp.get("choices") or [{}])[0].get("message") or {})
    except Exception:
        return {}


def _content_of(resp: dict) -> tuple[str, str]:
    """(正文, 思考内容)。思考模式下 `content` 可能为空而 `reasoning_content` 有字 ——
    这种时候**不能当成失败**，那只是把预算花在推理上了。"""
    m = _message_of(resp)
    return str(m.get("content") or ""), str(m.get("reasoning_content") or "")


def _finish_of(resp: dict) -> str:
    try:
        return str(((resp.get("choices") or [{}])[0] or {}).get("finish_reason") or "")
    except Exception:
        return ""


def _loose_json(text: str) -> dict | None:
    t = (text or "").strip()
    if not t:
        return None
    t = t.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(t[i:j + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


# ── 流式（实时语音的第一步） ─────────────────────────────────────────────────
SENT_END = "。！？!?；;\n"
SENT_SOFT = "，,、：:"


class SentenceSplitter:
    """把"边生成边吐字"的 token 流切成**能立刻念**的句子。

    实时语音的关键不是"模型多快"，而是**第一句多快**：等整段生成完再 TTS，
    首字延迟 = 全文生成时间；边生成边念，首字延迟 = 第一句生成时间。
    所以这里：遇到句末标点立刻切；没有标点但已经憋了 `soft_len` 个字，就在最近的
    逗号处切（读起来仍然自然）；再不行硬切，绝不无限等。
    """

    def __init__(self, soft_len: int = 18, hard_len: int = 42):
        self.buf = ""
        self.soft_len = soft_len
        self.hard_len = hard_len

    def feed(self, delta: str) -> list[str]:
        self.buf += delta or ""
        out: list[str] = []
        while True:
            idx = min([i for i in (self.buf.find(c) for c in SENT_END) if i >= 0],
                      default=-1)
            if idx >= 0:
                out.append(self.buf[: idx + 1].strip())
                self.buf = self.buf[idx + 1:]
                continue
            if len(self.buf) >= self.soft_len:
                soft = [i for i in (self.buf.find(c) for c in SENT_SOFT) if i >= 0]
                if soft:
                    cut = min(soft)
                    out.append(self.buf[: cut + 1].strip())
                    self.buf = self.buf[cut + 1:]
                    continue
            if len(self.buf) >= self.hard_len:
                out.append(self.buf[: self.hard_len].strip())
                self.buf = self.buf[self.hard_len:]
                continue
            break
        return [s for s in out if s]

    def flush(self) -> str:
        s, self.buf = self.buf.strip(), ""
        return s


# ── 一步 ───────────────────────────────────────────────────────────────────
@dataclass
class Step:
    n: int
    tool: str = ""
    args: dict = field(default_factory=dict)
    ok: bool = False
    data: str = ""
    say: str = ""

    def brief(self) -> dict:
        return {"n": self.n, "tool": self.tool, "ok": self.ok, "data": self.data[:200]}


class Agent:
    """一句话 → 一串工具调用 → 一句回报。"""

    MAX_STEPS = 10

    def __init__(self, reg: Registry, *, base_url: str = "", model: str = "",
                 api_key: str = "", endpoint: str = "", memory=None, speech=None,
                 log=None, timeout: float = 45.0, max_steps: int = MAX_STEPS,
                 budget_s: float = 110.0, runner=None, obs_fn=None,
                 allow: set[str] | None = None, bot_name: str = "Buddy",
                 outdir: Path | str | None = None, thinking: bool = False):
        cfg = load_config()
        self.reg = reg
        self.memory = memory
        self.speech = speech
        self.log = log or (lambda obj: None)
        self.runner = runner                    # (kind, args) -> dict  —— 走指挥层作业池
        self.obs_fn = obs_fn                    # () -> obs（runner 需要玩家坐标时用）
        self.allow = allow
        self.bot_name = bot_name
        self.max_steps = max_steps
        self.budget_s = budget_s
        self.outdir = Path(outdir) if outdir else (ROOT / "runs" / "agent")
        self.trace: list[Step] = []
        self.last_usage: dict = {}
        base = endpoint or base_url          # 兼容老写法 endpoint=<完整 url>
        self.llm = LLM(base or cfg["base_url"], model or cfg["model"],
                       api_key or cfg["api_key"], timeout=timeout, log=self.log,
                       thinking=thinking)
        self.has_model = bool(self.llm.api_key)

    # ── 能力面：给模型看的工具清单（原生的用 schema，长活用 macro_*） ────────
    def tools_schema(self, *, native: bool = True, limit: int = 24) -> list[dict]:
        out: list[dict] = []
        # ①b 目标分解（2026-10-29 operator）：先问"它会不会做"，再拉它自己的任务树
        out.append({"type": "function",
                    "function": {
                        "name": "plan_item",
                        "description": ("查一座身体的资源依赖图：这个目标它会不会做、它自己会怎么拆步骤。"
                                        "凡是『弄到/做/造/建/弄一套 X』这类目标，**先调它**，"
                                        "拿到分解再决定（不要用底层工具瞎试）。"),
                        "parameters": {"type": "object",
                                       "properties": {"item": {"type": "string"},
                                                     "count": {"type": "integer"}},
                                       "required": ["item"]}}})
        # ① 长活（串行、自带核验）
        macro_desc = {
            "mine": ("挖掉一个坐标上的方块（自己走近、够得着再挖，会核验方块变空气）",
                     {"target": {"type": "array", "items": {"type": "number"},
                                 "description": "[x,y,z]"}}),
            "get": ("去弄到某样东西（总量口径：背包里有 N 个就算完成）",
                    {"item": {"type": "string"}, "count": {"type": "integer"}}),
            "build": ("在坐标处放一个方块（需要背包里有这种方块）",
                      {"target": {"type": "array", "items": {"type": "number"}},
                       "block": {"type": "string"}}),
            "hero": ("清掉附近的敌对生物（护主用）", {}),
            "selfcare": ("自己补给：@food 找吃的 / 需要什么命令就传 cmd",
                         {"cmd": {"type": "string"}}),
            "follow_player": ("走到玩家身边（保持 3 格左右）", {}),
            "supply": ("把背包里的东西丢给玩家（必须贴身）",
                       {"item": {"type": "string"}, "count": {"type": "integer"}}),
            "teleport": ("直接传送到玩家身边（服务器允许时）", {}),
        }
        for kind, (desc, props) in macro_desc.items():
            if self.runner is None and kind in ("follow_player", "supply", "teleport"):
                continue
            out.append({"type": "function",
                        "function": {"name": f"macro_{kind}",
                                     "description": desc + "（串行执行，自带核验）",
                                     "parameters": {"type": "object", "properties": props,
                                                    "required": list(props)}}})
        # ② 原生的读/手脚工具：**先给常用的那批**，再按字母序补到上限
        if native:
            names = [n for n in PREFERRED if n in (self.reg.tools or {}) and n not in NEVER]
            rest = [n for n in sorted(self.reg.tools or {})
                    if n not in names and n not in NEVER]
            for name in (names + rest):
                t = self.reg.tools[name]
                schema = t.get("inputSchema") or {}
                props = schema.get("properties") or {}
                out.append({"type": "function",
                            "function": {"name": name,
                                         "description": (t.get("description") or name).split("\n")[0][:180],
                                         "parameters": {"type": "object", "properties": props,
                                                        "required": schema.get("required") or []}}})
                if len(out) >= limit:
                    break
        return out

    def context(self, player: str = "Xiaorun") -> dict:
        st = self.reg.invoke("getGameState", {})
        me = (st.data or {}).get("self") if (st.ok and isinstance(st.data, dict)) else {}
        det = as_obj(self.reg.invoke("playerDetail", {"player": player}).data or {})
        ents = self.reg.invoke("getEntities", {"limit": 8})
        d = ents.data
        ents_list = (d.get("_raw") if isinstance(d, dict) else d) or []
        inv = actions.inventory(self.reg)
        ctx = {
            "me": me,
            "player": det,
            "nearby_entities": ents_list[:8] if isinstance(ents_list, list) else ents_list,
            "my_items": dict(list(inv.items())[:14]),
            "persona": (self.memory.brief() if self.memory is not None else ""),
        }
        # 2026-10-27：模型最容易犯的错是"猜坐标"。『把面前这块挖掉』的"这块"必须由我们算出来：
        # 用玩家自己的 yaw/pitch 打一条射线，给出**他正对着的那个方块**的坐标。
        try:
            from .planner import ray_block
            pp = None
            if isinstance(det, dict):
                pp = self.reg.pos(det)
            if pp:
                eye = (pp[0], pp[1] + actions.EYE_H, pp[2])
                hit = ray_block(eye, float(det.get("yaw") or 0.0), float(det.get("pitch") or 0.0),
                                lambda x, y, z: {**self.reg.block(x, y, z),
                                                 "_air": self.reg.is_air(x, y, z)})
                if hit:
                    ctx["player_looking_at"] = {"pos": list(hit[:3]), "block": hit[3]}
        except Exception:
            pass
        return ctx

    def _exec(self, name: str, args: dict) -> dict:
        """执行一个工具调用。长活走 runner（指挥层作业池，单瞄准者）。"""
        # 目标分解（不是 MCP 工具，走 companion/goal_graph.py）
        if name == "plan_item":
            from companion import goal_graph
            plan = goal_graph.decompose(self.reg, str(args.get("item") or ""),
                                        int(args.get("count") or 1), log=self.log)
            return {"ok": bool(plan.get("ok")), "data": goal_graph.prompt_block(plan),
                    "tree": plan.get("tree"), "reason": plan.get("reason", "")}
        if name.startswith("macro_"):
            kind = name[len("macro_"):]
            if kind not in MACRO_KINDS:
                return {"ok": False, "why": f"未知宏 {kind}"}
            if self.runner is None:
                return {"ok": False, "why": "手边没工具，做不了这个"}
            try:
                return self.runner(kind, args) or {"ok": False, "why": "没有返回"}
            except Exception as e:
                return {"ok": False, "err": f"{type(e).__name__}: {e}"[:200]}
        if name in NEVER:
            return {"ok": False, "why": "这个能力不给模型用"}
        if name in ("ChatMessage", "chatSay"):
            text = str(args.get("msg") or args.get("text") or "")
            ok = self.speech.say(text) if self.speech else False
            return {"ok": bool(ok), "via": "speech"}
        r = self.reg.invoke(name, args)
        data = r.data
        if isinstance(data, (dict, list)):
            data = json.dumps(as_obj(data) if isinstance(data, str) else data,
                              ensure_ascii=False)[:900]
        return {"ok": bool(r.ok), "err": r.err[:200], "data": str(data)[:900]}

    # ── 规则回落（没有模型时：NLU + 规则，仍然动手、仍然回话） ───────────────
    def _rule_fallback(self, goal: str, player: str) -> dict:
        parsed = nlu.parse(goal, me=self.bot_name.lower(), player=player)
        k = parsed.get("kind")
        say = ""
        ok = False
        if k == "get" and parsed.get("item"):
            if self.runner:
                r = self.runner("get", {"item": parsed["item"],
                                        "count": int(parsed.get("count") or 3)})
                ok = bool(r.get("ok"))
                say = (f"{parsed['item']} 弄到了" if ok else f"{parsed['item']} 没弄到")
            else:
                say = "现在拿不到东西"
        elif k == "follow" and self.runner:
            r = self.runner("follow_player", {})
            ok, say = bool(r.get("ok")), ("来了" if r.get("ok") else "过不去")
        elif k == "teleport" and self.runner:
            r = self.runner("teleport", {})
            ok, say = bool(r.get("ok")), ("到了" if r.get("ok") else "过不去")
        elif k == "stop":
            actions.stop_all(self.reg)
            ok, say = True, "停了"
        elif k == "ask":
            ok, say = True, "我能挖、能砍、能盖、能打怪、能跟着你；你直接说"
        else:
            say = "这句我没听懂，换个说法？"
        if self.speech and say:
            self.speech.say(say, force=True)
        self.log({"ev": "agent_rule", "goal": goal, "parsed": parsed, "say": say, "ok": ok})
        return {"ok": ok, "how": "rule", "say": say, "steps": [],
                "sec": 0.0, "tokens": {}, "parsed": parsed}

    # ── 主循环 ─────────────────────────────────────────────────────────────
    def run(self, goal: str, *, player: str = "Xiaorun",
            max_steps: int | None = None) -> dict:
        t0 = time.time()
        self.trace = []
        steps_budget = max_steps or self.max_steps
        mode = "llm" if self.has_model else "rule"

        if not self.has_model:
            res = self._rule_fallback(goal, player)
            res["sec"] = round(time.time() - t0, 1)
            res["tokens"] = dict(self.llm.usage)
            self._dump(goal, res)
            return res

        tools = self.tools_schema(native=True)
        sysmsg = SYSTEM.format(bot=self.bot_name, player=player)
        ctx = self.context(player)
        messages: list[dict] = [
            {"role": "system", "content": sysmsg},
            {"role": "user", "content": "当前状态：" + json.dumps(ctx, ensure_ascii=False)[:3500]
                                     + f"\n\n玩家说：{goal}"},
        ]

        for n in range(1, steps_budget + 1):
            if time.time() - t0 > self.budget_s:
                return self._finish(goal, messages, {"ok": False, "why": "时间预算用完",
                                                     "how": mode}, t0)
            resp = self.llm.chat(messages, tools)
            if not resp:
                return self._finish(goal, messages, {"ok": False, "why": "模型没有可用输出",
                                                     "how": mode}, t0)
            msg = _message_of(resp)
            calls = msg.get("tool_calls") or []

            # ① 原生 tool_calls
            if calls:
                messages.append({"role": "assistant", "content": msg.get("content") or "",
                                 "tool_calls": calls})
                for c in calls[:3]:                  # 一次最多接三条，防模型一次性轰炸
                    fn = (c.get("function") or {})
                    name = str(fn.get("name") or "")
                    raw = fn.get("arguments")
                    args = raw if isinstance(raw, dict) else (_loose_json(str(raw or "")) or {})
                    out = self._exec(name, args)
                    s = Step(n=n, tool=name, args=args, ok=bool(out.get("ok")),
                             data=json.dumps(out, ensure_ascii=False)[:600])
                    self.trace.append(s)
                    self.log({"ev": "agent_step", "n": n, "tool": name, "args": args,
                              "ok": s.ok, "data": s.data[:240]})
                    messages.append({"role": "tool", "tool_call_id": c.get("id") or f"c{n}",
                                     "content": s.data[:1200]})
                continue

            # ② 没有 tool_calls：要么是收尾话，要么是 JSON 风格的工具（兼容老端点）
            content = str(msg.get("content") or "")
            obj = _loose_json(content)
            if obj and (obj.get("tool") or obj.get("macro")):
                name = str(obj.get("tool") or f"macro_{obj.get('macro')}")
                args = dict(obj.get("args") or {})
                out = self._exec(name, args)
                s = Step(n=n, tool=name, args=args, ok=bool(out.get("ok")),
                         data=json.dumps(out, ensure_ascii=False)[:600])
                self.trace.append(s)
                self.log({"ev": "agent_step", "n": n, "tool": name, "args": args,
                          "ok": s.ok, "data": s.data[:240]})
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user",
                                 "content": f"工具返回：{s.data[:800]}\n继续，或说一句收尾。"})
                continue

            say = (obj.get("say") if obj else content).strip() if content or obj else ""
            say = (say or "办完了")[:120]
            if self.speech:
                self.speech.say(say, force=True)
            self.log({"ev": "agent_say", "say": say, "steps": len(self.trace)})
            return self._finish(goal, messages, {"ok": True, "say": say, "how": mode}, t0)

        return self._finish(goal, messages, {"ok": False, "why": f"{steps_budget} 步没收敛",
                                             "how": mode}, t0)

    def run_stream(self, goal: str, *, player: str = "Xiaorun",
                   on_sentence=None, max_steps: int | None = None) -> dict:
        """**流式**版 `run()`：边生成边念，第一句出来就开始说。

        为什么单开一个入口而不是把 `run()` 改成流式：
        工具循环里"说话"和"调工具"是互斥的两条路（模型要么回一句、要么给 tool_calls），
        混在一个函数里会让非流式路径（脚本化端点、老端点）也跟着变复杂。
        这里只服务实时语音那条路。
        """
        t0 = time.time()
        self.trace = []
        said: list[str] = []
        first_at = None
        steps_budget = max_steps or self.max_steps
        if not self.has_model:
            res = self._rule_fallback(goal, player)
            if on_sentence and res.get("say"):
                on_sentence(str(res["say"]))
            res.update({"sec": round(time.time() - t0, 1), "how": "rule"})
            return res

        tools = self.tools_schema(native=True)
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM.format(bot=self.bot_name, player=player)},
            {"role": "user", "content": "当前状态："
             + json.dumps(self.context(player), ensure_ascii=False)[:3500]
             + f"\n\n玩家说：{goal}"},
        ]

        def _emit(text: str) -> None:
            nonlocal first_at
            if not text:
                return
            said.append(text)
            if first_at is None:
                first_at = round(time.time() - t0, 2)
            if on_sentence:
                try:
                    on_sentence(text)
                except Exception:
                    pass

        for n in range(1, steps_budget + 1):
            if time.time() - t0 > self.budget_s:
                break
            splitter = SentenceSplitter()
            calls: list[dict] = []
            for ev in self.llm.chat_stream(messages, tools):
                if ev["type"] == "delta":
                    for s in splitter.feed(ev["text"]):
                        _emit(s)
                elif ev["type"] == "tool_calls":
                    calls = ev["calls"]
                elif ev["type"] == "error":
                    _emit("我这边网有点问题，稍等。")
                    break
            _emit(splitter.flush())
            if not calls:
                break
            messages.append({"role": "assistant", "content": "".join(said)[-500:]
                             or "(调工具)"})
            for c in calls[:2]:
                args = _loose_json(c["args"]) or {}
                out = self._exec(c["name"], args)
                s = Step(n=n, tool=c["name"], args=args, ok=bool(out.get("ok")),
                         data=json.dumps(out, ensure_ascii=False)[:400])
                self.trace.append(s)
                self.log({"ev": "agent_step", "n": n, "tool": c["name"],
                          "args": args, "ok": s.ok, "stream": True})
                messages.append({"role": "user",
                                 "content": f"工具 {c['name']} 返回：{s.data[:800]}\n"
                                            f"继续，或用一句人话收尾。"})
            said.clear()   # 每轮重新累计，避免把中间话术带进最终结论

        text = "".join(said).strip() or "这个我一时办不成，先说一声"
        res = {"ok": bool(said), "say": text, "how": "llm-stream",
               "first_sentence_s": first_at, "sec": round(time.time() - t0, 1),
               "steps": [s.brief() for s in self.trace],
               "tokens": dict(self.llm.usage), "model": self.llm.model}
        self._dump(goal, res, messages)
        self.log({"ev": "agent_stream_done", "goal": goal[:60], "first_s": first_at,
                  "sec": res["sec"], "steps": len(self.trace)})
        return res

    def _finish(self, goal: str, messages: list[dict], res: dict, t0: float) -> dict:
        # 2026-10-27：预算/步数用完时也必须**留一句人话**。否则外面看到的是
        # "它闷头干了 125 秒，一个字都没说"（实测就这样）。
        if not res.get("say"):
            said = ("这个我一时办不成，先说一声" if not res.get("ok")
                    else "办完了")
            res["say"] = said
            if self.speech:
                try:
                    self.speech.say(said, force=True)
                except Exception:
                    pass
        res = {**res, "steps": [s.brief() for s in self.trace],
               "sec": round(time.time() - t0, 1), "tokens": dict(self.llm.usage),
               "model": self.llm.model}
        self._dump(goal, res, messages)
        self.log({"ev": "agent_done", "goal": goal[:80], "ok": res.get("ok"),
                  "how": res.get("how"), "steps": len(self.trace),
                  "sec": res["sec"], "tokens": res["tokens"]})
        return res

    def _dump(self, goal: str, res: dict, messages: list[dict] | None = None) -> None:
        """每轮落一份 transcript —— 接真模型之后，"它为什么这么干"只能靠这份。"""
        try:
            d = self.outdir / time.strftime("%Y%m%d-%H%M%S")
            d.mkdir(parents=True, exist_ok=True)
            n = len(list(d.glob("run-*.json"))) + 1
            (d / f"run-{n}.json").write_text(json.dumps(
                {"goal": goal, "result": res,
                 "messages": (messages or [])[-40:]}, ensure_ascii=False, indent=1)[:200000],
                encoding="utf-8")
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="agent 自检 / 配置查看")
    ap.add_argument("--show-config", action="store_true")
    ap.add_argument("--set-key", default="", help="写入 config/ai.json 的 api_key")
    ap.add_argument("--set-model", default="")
    ap.add_argument("--ping", action="store_true", help="用真模型问一句，验证端点/密钥")
    ap.add_argument("--stream", default="", help="流式演示：边生成边切句（实时语音的地基）")
    ap.add_argument("--voice", action="store_true", help="流式演示时同时朗读（Windows SAPI）")
    a = ap.parse_args()

    if a.set_key or a.set_model:
        p = save_config(api_key=a.set_key, model=a.set_model)
        print(f"[agent] 已写入 {p}")
    if a.show_config:
        cfg = load_config()
        shown = {**cfg, "api_key": (cfg["api_key"][:6] + "…" + cfg["api_key"][-4:]
                                    if len(cfg["api_key"]) > 12 else ("(未配)" if not cfg["api_key"] else "***"))}
        print(json.dumps(shown, ensure_ascii=False, indent=2))
    if a.stream:
        cfg = load_config()
        if not cfg.get("enabled"):
            print(json.dumps({"ok": False, "why": "没有 api_key"}, ensure_ascii=False))
            return 1
        from .voice_io import VoiceIO
        voice = VoiceIO.auto(enabled=a.voice) if a.voice else None
        spoken: list[tuple[float, str]] = []
        t0 = time.time()

        def on_sentence(s: str) -> None:
            spoken.append((round(time.time() - t0, 2), s))
            print(f"  +{spoken[-1][0]:>5.2f}s  {s}", flush=True)
            if voice is not None:
                voice.speak(s)

        ag = Agent.__new__(Agent)          # 只借流式通道，不碰身体
        ag.reg = None
        ag.memory = ag.speech = None
        ag.log = lambda o: None
        ag.runner = ag.obs_fn = None
        ag.allow = None
        ag.bot_name, ag.max_steps, ag.budget_s = "Buddy", 4, 40.0
        ag.outdir = ROOT / "runs" / "agent"
        ag.trace = []
        ag.llm = LLM(cfg["base_url"], cfg["model"], cfg["api_key"], timeout=30.0,
                     retries=1, log=ag.log)
        ag.has_model = True
        ag.tools_schema = lambda **kw: []       # 没有身体，只演示"边生成边念"
        ag.context = lambda player="Xiaorun": {"note": "无身体，仅流式演示"}
        ag.__dict__["_exec"] = lambda name, args: {"ok": False, "why": "无身体"}
        res = Agent.run_stream(ag, a.stream, on_sentence=on_sentence)
        print(json.dumps({"first_sentence_s": res.get("first_sentence_s"),
                          "sec": res.get("sec"), "said": res.get("say"),
                          "voice": bool(voice)}, ensure_ascii=False, indent=2))
        return 0 if res.get("first_sentence_s") else 1
    if a.ping:
        cfg = load_config()
        llm = LLM(cfg["base_url"], cfg["model"], cfg["api_key"], log=lambda o: print(o))
        r = llm.chat([{"role": "user", "content": "只回四个字：连接正常"}], max_tokens=64)
        content, reasoning = _content_of(r)
        ok = bool(content or reasoning)
        print(json.dumps({"ok": ok, "content": content[:120],
                          "reasoning": reasoning[:80],
                          "finish": _finish_of(r), "usage": llm.usage,
                          "model": llm.model}, ensure_ascii=False, indent=2))
        return 0 if content else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
