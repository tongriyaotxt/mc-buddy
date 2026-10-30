#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""companion/voice_hub.py —— 3 个陪玩**共享一路**麦克风与喇叭（W3-2）

为什么要它：SenseVoice 模型加载后常驻 ~300MB；3 个 commander 各自开一路 STT，
就是 3 份内存 + 3 个麦克风抢占（Windows 上同一麦克风被多进程同时打开，实测会互相打断）。
而玩家只有一张嘴。

结构（文件即总线，零新依赖）：

    麦克风 ──▶ VoiceHub（**一个进程**，加载一次模型）
                  ├─ 说完了 → 追加 `runs/voice/inbox.jsonl`  {seq, ts, text, to}
                  └─ 喇叭队列 `runs/voice/outbox.jsonl`  {ts, text, voice}  ← 由**它一个人**朗读

    commander #1..#3 ──▶ HubSTT（只读 inbox.jsonl 的增量，带自己的 offset）
                     └─▶ HubTTS（只往 outbox.jsonl 追加一行）

三条关键设计：

1. **自听在唯一一点上解决**：hub 自己知道"喇叭在响"，所以它在 `tts_busy` 期间
   **根本不读麦**（`stt_muted` 事件同 `VoiceIO` 一个口径）。分散到 3 个进程时
   谁也拿不到别人的播放状态，这才是必须收成一份的真正原因。
2. **`to` 字段**：`to=null` = 广播（三个都动）；`to="buddy2"` = 只有它动。
   说话权（`team.may_speak()`）仍由各 commander 自己判 —— hub 不认识队伍规则，只分发。
3. **截断自愈**：hub 重启会把 inbox 写新文件（append 到旧文件也可），
   客户端发现 `offset > size` 就归零重读，绝不"从此再也收不到"。

用法：
    python -X utf8 -m companion.voice_hub                      # 起 hub（真麦克风）
    python -X utf8 -m companion.voice_hub --no-tts             # 只做识别
    python -X utf8 -m companion.voice_hub --selftest           # 离线自检（不碰硬件）

    # commander 侧（每个实例各一条）：
    python -X utf8 -m companion.commander ... --voice --voice-hub runs\voice\inbox.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOICE_DIR = ROOT / "runs" / "voice"
INBOX = VOICE_DIR / "inbox.jsonl"
OUTBOX = VOICE_DIR / "outbox.jsonl"


def _append(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ── 客户端侧：两个"假引擎"，协议和 voice_io 的 STT/TTS 一致 ──────────────────
class HubSTT:
    """只读 inbox.jsonl 的增量。**不加载模型、不开麦克风**。

    `listen()` 的阻塞语义跟 `SherpaSTT.listen()` 对齐：没有新话就在 `idle` 秒后
    返回 None（上层循环本来就会再调一次），所以不会变成 100% CPU 忙等。
    """

    name = "hub"

    def __init__(self, path: str | Path = INBOX, name: str = "", log=None,
                 idle: float = 20.0, from_start: bool = False):
        self.path = Path(path)
        self.me = name or ""
        self.log = log or (lambda o: None)
        self.idle = float(idle)
        self._offset = 0
        self._buf = ""
        self._pending: list[dict] = []      # 一次读到多行时不能只返回第一条（丢了就永远丢了）
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.ok = True
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()
        if not from_start:
            self._offset = self.path.stat().st_size       # 默认只收"我起来之后"的话

    # 文件增量读取（按行，容忍半行）
    def _read_new(self) -> list[dict]:
        out: list[dict] = []
        try:
            size = self.path.stat().st_size
            if size < self._offset:                        # 被截断/换文件 ⇒ 归零重读
                self._offset = 0
                self._buf = ""
            if size == self._offset:
                return out
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
                self._offset = fh.tell()
        except FileNotFoundError:
            return out
        text = chunk.decode("utf-8", "replace")
        if text.startswith("\ufeff"):            # 别人（PowerShell Add-Content）会带 BOM
            text = text[1:]
        # 半行留到下次（用 \n 切，最后一段没换行 = 还没写完）
        parts = (self._buf + text).split("\n")
        self._buf = parts.pop() if parts else ""
        for line in parts:
            line = line.strip().lstrip("\ufeff")
            if not line.startswith("{"):
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def _next_record(self) -> dict | None:
        """取一条；已经有解好的就先发，没有再去读文件。"""
        while True:
            while self._pending:
                rec = self._pending.pop(0)
                to = rec.get("to") or ""
                if to and self.me and to != self.me:
                    continue                              # 点名给别人，丢掉
                if str(rec.get("text") or "").strip():
                    return rec
            fresh = self._read_new()
            if not fresh:
                return None
            self._pending.extend(fresh)

    def listen(self) -> str | None:
        if not self.ok:
            return None
        deadline = time.time() + self.idle
        while not self._stop.is_set() and time.time() < deadline:
            rec = self._next_record()
            if rec is not None:
                txt = str(rec.get("text") or "").strip()
                self.log({"ev": "voice_hub_in", "text": txt[:80],
                          "to": rec.get("to") or None})
                return txt
            time.sleep(0.1)
        return None

    def stop(self) -> None:
        self.ok = False
        self._stop.set()

    def rewind(self) -> None:
        """调试用：从头再读一遍。"""
        self._offset = 0
        self._buf = ""
        self._pending.clear()


class HubTTS:
    """只往 outbox.jsonl 追加一行；**朗读由 hub 一个人做**。

    `busy` 是估算（文字长度 × 每字 0.22s + 0.6s 起播）：客户端拿它当"别听自己"的窗口。
    真正的精确静音在 hub 侧（它知道喇叭什么时候真的停），这里只是别让客户端在
    自己说话时傻等或立刻去读麦。
    """

    name = "hub-tts"

    def __init__(self, path: str | Path = OUTBOX, voice: str = "", log=None):
        self.path = Path(path)
        self.voice = voice
        self.log = log or (lambda o: None)
        self._until = 0.0
        self.spoken = 0

    @property
    def busy(self) -> bool:
        return time.time() < self._until

    def speak(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        _append(self.path, {"ts": round(time.time(), 3), "text": t[:300],
                            "voice": self.voice})
        est = 0.6 + 0.22 * len(t)
        self._until = max(self._until, time.time() + est)
        self.spoken += 1
        return True

    def stop(self) -> None:
        self._until = 0.0


# ── 服务端：唯一持有麦克风与喇叭的进程 ──────────────────────────────────────
class VoiceHub:
    def __init__(self, stt=None, tts=None, inbox: Path = INBOX, outbox: Path = OUTBOX,
                 log=None, poll: float = 0.1):
        self.stt = stt
        self.tts = tts
        self.inbox = Path(inbox)
        self.outbox = Path(outbox)
        self.log = log or (lambda o: None)
        self.poll = poll
        self.seq = 0
        self.tts_busy = threading.Event()
        self._stop = threading.Event()
        self._out_offset = 0
        self.heard = 0
        self.spoken_n = 0

    # 喇叭：一个人读
    def _tts_loop(self) -> None:
        if self.tts is None:
            return
        if not self.outbox.exists():
            self.outbox.parent.mkdir(parents=True, exist_ok=True)
            self.outbox.touch()
        self._out_offset = self.outbox.stat().st_size   # 只读新请求
        while not self._stop.is_set():
            try:
                size = self.outbox.stat().st_size
                if size > self._out_offset:
                    with self.outbox.open("rb") as fh:
                        fh.seek(self._out_offset)
                        raw = fh.read()
                        self._out_offset = fh.tell()
                    for line in raw.decode("utf-8", "replace").splitlines():
                        line = line.strip()
                        if not line.startswith("{"):
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        text = str(rec.get("text") or "").strip()
                        if not text:
                            continue
                        self.tts_busy.set()            # ← 从这一刻起不读麦
                        try:
                            self.tts.speak(text)
                        except Exception as e:
                            self.log({"ev": "hub_tts_error",
                                      "err": f"{type(e).__name__}: {e}"[:120]})
                        finally:
                            self.tts_busy.clear()
                        self.spoken_n += 1
                        self.log({"ev": "hub_tts", "text": text[:60]})
            except FileNotFoundError:
                pass
            except Exception as e:
                self.log({"ev": "hub_tts_loop_error", "err": f"{type(e).__name__}: {e}"[:120]})
            time.sleep(self.poll)

    # 麦克风：**只有这里有**；喇叭响的时候一格不读
    def run(self, seconds: float = 0.0) -> None:
        if self.tts is not None:
            threading.Thread(target=self._tts_loop, daemon=True, name="hub-tts").start()
        if self.stt is None:
            self.log({"ev": "hub_stt_off"})
        t0 = time.time()
        muted_marks = 0
        while not self._stop.is_set():
            if seconds and time.time() - t0 > seconds:
                break
            if self.tts_busy.is_set():
                if muted_marks == 0:
                    self.log({"ev": "stt_muted", "where": "hub"})
                muted_marks += 1
                time.sleep(self.poll)
                continue
            muted_marks = 0
            try:
                text = self.stt.listen()
            except Exception as e:
                self.log({"ev": "hub_stt_error", "err": f"{type(e).__name__}: {e}"[:120]})
                time.sleep(1.0)
                continue
            if not text:
                continue
            self.seq += 1
            self.heard += 1
            _append(self.inbox, {"seq": self.seq, "ts": round(time.time(), 3),
                                 "text": str(text)[:200], "to": None})
            self.log({"ev": "hub_heard", "seq": self.seq, "text": str(text)[:80]})

    def stop(self) -> None:
        self._stop.set()


def make_hub(no_tts: bool = False, log=None) -> VoiceHub:
    """按 config/voice.json 选型造一个 hub（STT 必选，TTS 可关）。"""
    from companion import voice_engine as VE
    tts = None if no_tts else VE.make_tts(log=log)[0]
    stt = None
    try:
        stt = VE.make_stt(log=log)[0]
    except Exception as e:
        if log:
            log({"ev": "hub_stt_fail", "err": f"{type(e).__name__}: {e}"[:120]})
    return VoiceHub(stt=stt, tts=tts, log=log)


# ── 自检（不碰硬件）────────────────────────────────────────────────────────
def selftest() -> dict:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hubtest-"))
    inbox, outbox = tmp / "inbox.jsonl", tmp / "outbox.jsonl"
    inbox.touch()
    outbox.touch()

    class FakeSTT:
        name = "fake"
        def __init__(self):
            self.q = ["跟着我", "挖点石头", "别跟着了"]
            self.i = 0
        def listen(self):
            if self.i < len(self.q):
                t = self.q[self.i]
                self.i += 1
                time.sleep(0.05)
                return t
            time.sleep(0.05)
            return None
        def stop(self):
            return None

    class FakeTTS:
        name = "fake-tts"
        def __init__(self):
            self.said = []
            self.hold = 0.15
        def speak(self, text):
            self.said.append(text)
            time.sleep(self.hold)
            return True
        def stop(self):
            return None

    hub = VoiceHub(stt=FakeSTT(), tts=FakeTTS(), inbox=inbox, outbox=outbox, poll=0.02)
    clients = [HubSTT(inbox, name=f"buddy{i + 1}", idle=0.6) for i in range(3)]
    th = threading.Thread(target=hub.run, kwargs={"seconds": 6.0}, daemon=True)
    th.start()
    time.sleep(0.8)                       # 让前两句先落盘
    got = {c.me: [] for c in clients}
    def drain(c):
        while True:
            t = c.listen()
            if not t:
                break
            got[c.me].append(t)
    # 三个客户端并发读（模拟 3 个 commander）
    threads = [threading.Thread(target=drain, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=8)
    # 说话：客户端 → outbox → hub 一个人读
    clients[1].ok = True
    tts_client = HubTTS(outbox)
    tts_client.speak("我来了")
    time.sleep(0.6)
    hub.stop()
    th.join(timeout=3)

    probs = []
    if hub.heard < 3:
        probs.append(f"hub 只收到 {hub.heard} 句")
    for me, lst in got.items():
        if len(lst) != 3:
            probs.append(f"{me} 收到 {len(lst)} 句（应 3）")
        if len(set(lst)) < len(lst):
            probs.append(f"{me} 有重复：{lst}")
    if not hub.spoken_n:
        probs.append("hub 没朗读 outbox 里的请求（TTS 共享没生效）")
    # ④ 点名：只给 buddy2，别人不许拿
    _append(inbox, {"seq": 99, "ts": time.time(), "text": "点名测试", "to": "buddy2"})
    seen1 = clients[0].listen()
    seen2 = clients[1].listen()
    if seen2 != "点名测试":
        probs.append(f"点名没送到 buddy2：{seen2!r}")
    if seen1 == "点名测试":
        probs.append("点名给 buddy2 的话被 buddy1 收走了")
    # ⑤ 新客户端：从"我起来之后"开始收（先建再追加，才能收到后面那句）
    client_new = HubSTT(inbox, name="buddy9", idle=0.6)
    _append(inbox, {"seq": 100, "ts": time.time(), "text": "换文件之后", "to": None})
    seen3 = client_new.listen()
    if seen3 != "换文件之后":
        probs.append(f"新客户端读不到新行：{seen3!r}")
    # ⑥ 截断自愈：inbox 被清空（hub 重启换文件）后必须还能收，不许"从此聋了"
    inbox.write_text("", encoding="utf-8")
    _append(inbox, {"seq": 101, "ts": time.time(), "text": "重启之后", "to": None})
    seen4 = clients[2].listen()
    if seen4 != "重启之后":
        probs.append(f"inbox 截断后没收回来：{seen4!r}")
    # ⑦ 外部写入者带 BOM（PowerShell `Add-Content -Encoding UTF8` 实测会带）也必须认
    with inbox.open("ab") as fh:
        fh.write(b"\xef\xbb\xbf" + json.dumps(
            {"seq": 102, "ts": time.time(), "text": "带BOM的一句", "to": None},
            ensure_ascii=False).encode("utf-8") + b"\n")
    seen5 = clients[2].listen()
    if seen5 != "带BOM的一句":
        probs.append(f"带 BOM 的行被丢了：{seen5!r}")
    return {"ok": not probs, "problems": probs, "hub_heard": hub.heard,
            "per_client": {k: len(v) for k, v in got.items()},
            "hub_spoken": hub.spoken_n, "dir": str(tmp)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", default=str(INBOX))
    ap.add_argument("--outbox", default=str(OUTBOX))
    ap.add_argument("--no-tts", action="store_true")
    ap.add_argument("--seconds", type=float, default=0.0, help="0 = 一直跑")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        r = selftest()
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r["ok"] else 1

    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = VOICE_DIR / f"hub-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    fh = log_path.open("a", encoding="utf-8")

    def log(obj: dict) -> None:
        obj = {"t": round(time.time(), 3), **obj}
        try:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
            fh.flush()
        except Exception:
            pass
        if os.environ.get("HUB_ECHO"):
            print(json.dumps(obj, ensure_ascii=False), flush=True)

    hub = make_hub(no_tts=a.no_tts, log=log)
    hub.inbox, hub.outbox = Path(a.inbox), Path(a.outbox)
    print(f"[hub] inbox={hub.inbox}")
    print(f"[hub] outbox={hub.outbox}")
    print(f"[hub] stt={getattr(hub.stt, 'name', None)} tts={getattr(hub.tts, 'name', None)}")
    print("[hub] Ctrl+C 结束；commander 用 `--voice-hub` 接上")
    try:
        hub.run(seconds=a.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        try:
            fh.close()
        except Exception:
            pass
    print(f"[hub] 收了 {hub.heard} 句 / 读了 {hub.spoken_n} 句  log={log_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
