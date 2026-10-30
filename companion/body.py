#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/body.py —— 身体通道：AltoClef 客户端的一根杠杆（JSON-RPC over HTTP）

只做三件事，别在这里加业务：
  1. 把 MCP 调用包成 python 方法
  2. 统一解码（**有的工具把 JSON 再编码成字符串**，不统一处理会静默失败）
  3. 暴露 `tools()` 让指挥层自动发现全部能力 —— 这是"保留 AltoClef 所有能力"的入口

⚠️ 2026-10-26 夜 operator 改口：**零聊天输出这条铁律取消了**。
   陪玩可以说话（`say()`），而且以后要接声音。所以这里不再拦 `ChatMessage`；
   只留 `allow_chat=False` 这个开关给回归测试用（老用例仍然按"不说话"打分）。
   说话的礼貌（别刷屏、别抢话、语速）交给上层 `speech.py`，不放在通道层。
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

# 曾经用来拦聊天；现在只在 allow_chat=False 时生效（回归用）
CHAT_TOOLS = {"ChatMessage"}


class ToolError(RuntimeError):
    pass


def as_obj(v):
    """工具返回值的真实形状不统一：
      · `getGameState`/`getBlockAt` 是正常 dict
      · `playerDetail`/`dropItem`/`gesture` 是**双重编码的 JSON 字符串**（json 里套 json）
    所以这里**连续解到不是字符串为止**（最多 3 层）。只解一层会让调用方拿到一个字符串，
    再当 dict 用就永远读不到值 —— 这个坑让 look_detect/supply 静默失效过一整轮。
    """
    cur = v
    for _ in range(4):
        if isinstance(cur, dict):
            return cur
        if not isinstance(cur, str):
            break
        s = cur.strip()
        if not s:
            return {"_text": cur}
        try:
            nxt = json.loads(s)
        except Exception:
            return {"_text": cur}
        if nxt == cur:                # 解不动了
            return {"_text": cur}
        cur = nxt
    if isinstance(cur, dict):
        return cur
    if isinstance(cur, list):
        return {"_raw": cur}
    return {"_val": cur}


def truthy(v) -> bool:
    """`"is_air": "false"` —— 字符串布尔，`bool("false")` 是 True。"""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


class Body:
    def __init__(self, port: int, settings: Path | str, token: str | None = None,
                 allow_chat: bool = True):
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.port = port
        if token is None:
            token = json.loads(Path(settings).read_text(encoding="utf-8")).get("mcpAuthToken", "")
        self.token = token
        self.allow_chat = allow_chat
        self._tools: list[dict] | None = None
        # 2026-10-29 (W6-4): 往返计数器 —— "一个 tick 花了几次 RPC" 只能靠数字说话。
        # 用法：before = body.rpc_count; …; 用了 body.rpc_count - before 次。
        self.rpc_count = 0

    # ── 底层 ────────────────────────────────────────────────────────────────
    def rpc(self, method: str, params: dict | None = None, timeout: float = 25.0) -> dict:
        self.rpc_count += 1
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params or {}}).encode()
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        if "error" in j:
            raise ToolError(j["error"].get("message", "rpc error"))
        return j["result"]

    def call(self, tool: str, args: dict | None = None, timeout: float = 25.0) -> dict:
        if not self.allow_chat and tool in CHAT_TOOLS:
            raise ToolError(f"{tool} 被 allow_chat=False 关掉了（回归模式要求不说话）")
        r = self.rpc("tools/call", {"name": tool, "arguments": args or {}}, timeout)
        item = (r.get("content") or [{}])[0]
        text = item.get("text", "{}")
        if r.get("isError"):
            raise ToolError(f"{tool}: {text[:200]}")
        return as_obj(text)

    # ── 说话（2026-10-26 起合法） ────────────────────────────────────────────
    def say(self, text: str, timeout: float = 12.0) -> bool:
        """在游戏里说一句话。礼貌（限流/别抢话/别刷屏）在上层 speech.py，不在这里。"""
        t = (text or "").strip()
        if not t:
            return False
        try:
            self.call("ChatMessage", {"msg": t[:240]}, timeout)
            return True
        except Exception:
            return False

    # ── 流式调用（MCP progress over SSE） ───────────────────────────────────
    def call_stream(self, tool: str, args: dict | None = None, *, on_progress=None,
                    timeout: float = 300.0) -> dict:
        """调工具，并且**边跑边收进度**（MCP `notifications/progress`）。

        协议：请求里带 `params._meta.progressToken` + `Accept: text/event-stream`，
        服务端在返回最终结果之前先推几条 `notifications/progress`
        （`progress` 递增、`message` 是它自己的任务链）。没有这个能力的服务端
        会直接回普通 JSON —— 这里也兼容（收到的是 result 而不是 notification）。

        `on_progress(dict)` 收到每条进度（`{"progress":n,"message":"..."}`）。
        """
        args = args or {}
        token = f"p{int(time.time() * 1000) % 1000000}"
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": tool, "arguments": args,
                              "_meta": {"progressToken": token}}}
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream, application/json",
                     "Authorization": "Bearer " + self.token})
        result: dict = {}
        notes: list[dict] = []
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in ctype:
                raw = r.read().decode("utf-8", "replace")
                j = json.loads(raw or "{}")
                return as_obj(((j.get("result") or {}).get("content") or [{}])[0].get("text", "{}"))
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    msg = json.loads(line[5:].strip())
                except Exception:
                    continue
                if msg.get("method") == "notifications/progress":
                    p = msg.get("params") or {}
                    notes.append(p)
                    if on_progress:
                        try:
                            on_progress(p)
                        except Exception:
                            pass
                    continue
                if "result" in msg:
                    item = (msg["result"].get("content") or [{}])[0]
                    result = as_obj(item.get("text", "{}"))
                    break
        if isinstance(result, dict):
            result.setdefault("_progress", notes[-3:])
        return result

    # ── 能力发现 ────────────────────────────────────────────────────────────
    def tools(self, refresh: bool = False) -> list[dict]:
        """AltoClef 当前暴露的**全部** MCP 工具（原始 schema）。指挥层不裁剪它。"""
        if self._tools is None or refresh:
            self._tools = self.rpc("tools/list", {}, 30)["tools"]
        return self._tools

    def tool_names(self) -> list[str]:
        return [t["name"] for t in self.tools()]

    def alive(self) -> bool:
        try:
            self.call("getGameState", {}, 8)
            return True
        except Exception:
            return False
