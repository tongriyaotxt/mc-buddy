#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/voice_io.py —— 声音接口：**先说清楚接缝，再把能落地的先落地**

operator 2026-10-26：「以后接入 ai 还要有声音接入和输出」。
所以这里不是"等以后再说"，而是**现在就把接缝留好、把能跑的先跑起来**：

    ┌────────────── 输入 ──────────────┐        ┌────────── 输出 ──────────┐
    │ 玩家在游戏里打字  ──┐             │        │  游戏聊天（body.say）      │
    │ 语音 STT ─────────┼→ 同一个 NLU → 意图        ↑                    │
    │ EXE 面板/热键 ────┘             │        │  TTS（这里）              │
    └──────────────────────────────────┘        └──────────────────────────┘

设计原则
--------
1. **可缺席**：STT/TTS 都要能在"没装、没网、没麦"时安静地降级成空实现，绝不拖垮主循环。
2. **不阻塞**：TTS 用独立线程 + 队列；说一句话不该让 30 Hz 的决策循环等 3 秒。
3. **可替换**：`TTS` / `STT` 都是 Protocol；现在落地 Windows 自带的 SAPI（零依赖、离线可用），
   以后接云（Fish/火山/Azure/本地 whisper）只换实现，不换调用方。
4. **同一套礼貌**：语音也走 `speech.Speech` 的限流，不因为"声音"就刷屏。
"""
from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Protocol


# ── 输出：TTS ───────────────────────────────────────────────────────────────
class TTS(Protocol):
    name: str

    def speak(self, text: str) -> bool: ...

    def stop(self) -> None: ...


class NullTTS:
    """没有语音时的空实现 —— 一切都照常，只是不出声。"""
    name = "none"

    def speak(self, text: str) -> bool:
        return False

    def stop(self) -> None:
        pass


class SapiTTS:
    """Windows 自带语音合成（System.Speech）。零依赖、离线、能立刻用。

    2026-10-29 改：**常驻子进程**（旧版每句起一个 PowerShell，实测每句 ~0.9s 开销）。
    现在一次起好、循环读 stdin：说一句 → 打 `DONE` → 再说下一句。
    顺带解决了"自听"：`busy` 能精确表示"它还在说"，STT 在这期间直接不听。

    崩了/没起来就自动退回"每句起进程"的老路，绝不哑。
    """

    name = "sapi"

    PS_LOOP = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.Rate=__RATE__;$s.Volume=__VOL__;__VOICE__"
        "while($null -ne ($line=[Console]::In.ReadLine())){"
        "  if($line -eq ''){continue}"
        "  try{ $s.Speak($line) }catch{}"
        "  [Console]::Out.WriteLine('DONE');[Console]::Out.Flush() }"
    )

    def __init__(self, rate: int = 2, volume: int = 100, voice: str = "",
                 persistent: bool = True):
        self.rate = max(-10, min(10, rate))
        self.volume = max(0, min(100, volume))
        self.voice = voice
        self.persistent = persistent
        self._q: queue.Queue[str] = queue.Queue(maxsize=32)
        self._stop = False
        self._proc: subprocess.Popen | None = None
        self._pending = 0                  # 还没说完的条数（>0 = 正在说话）
        self._lock = threading.Lock()
        self._t = threading.Thread(target=self._run, daemon=True, name="tts")
        self._t.start()

    # ── 状态：给"防自听"用 ─────────────────────────────────────────────────
    @property
    def busy(self) -> bool:
        with self._lock:
            return self._pending > 0 or not self._q.empty()

    def speak(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        try:
            with self._lock:
                self._pending += 1
            self._q.put_nowait(t[:300])
            return True
        except queue.Full:
            with self._lock:
                self._pending = max(0, self._pending - 1)
            return False

    # ── 常驻进程（首选）────────────────────────────────────────────────────
    def _ensure_proc(self) -> bool:
        if not self.persistent:
            return False
        if self._proc is not None and self._proc.poll() is None:
            return True
        ps = (self.PS_LOOP
              .replace("__RATE__", str(self.rate))
              .replace("__VOL__", str(self.volume))
              .replace("__VOICE__", f"$s.SelectVoice('{self.voice}');" if self.voice else ""))
        try:
            self._proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", ps],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, encoding="utf-8", errors="replace")
            return True
        except Exception:
            self._proc = None
            return False

    def _speak_persistent(self, text: str) -> None:
        p = self._proc
        if p is None or p.stdin is None or p.stdout is None:
            raise RuntimeError("no persistent proc")
        p.stdin.write(text.replace("\n", " ") + "\n")
        p.stdin.flush()
        # 等它把 DONE 打出来 —— 这样"说完"是**精确**的（旧版只能靠猜时长）
        while True:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("tts proc died")
            if line.strip() == "DONE":
                return

    def _run(self) -> None:
        while not self._stop:
            try:
                text = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            done = False
            try:
                if self._ensure_proc():
                    self._speak_persistent(text)
                    done = True
            except Exception:
                done = False
            if not done:
                # 兜底：老路（每句起一个进程）
                ps = (
                    "Add-Type -AssemblyName System.Speech;"
                    "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                    f"$s.Rate={self.rate};$s.Volume={self.volume};"
                    + (f"$s.SelectVoice('{self.voice}');" if self.voice else "")
                    + "$s.Speak([Console]::In.ReadToEnd());"
                )
                try:
                    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                   input=text, text=True, capture_output=True, timeout=60,
                                   encoding="utf-8", errors="replace")
                except Exception:
                    pass
            with self._lock:
                self._pending = max(0, self._pending - 1)

    def stop(self) -> None:
        self._stop = True
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass


class HttpTTS:
    """接云端 TTS 的通用形状（现在不用，接的时候填进去）。

    约定：`POST endpoint {"text": "...", "voice": "..."}` 返回音频字节（wav/mp3），
    这里负责把字节落到临时文件并用系统播放器放出来。之所以先写形状，是因为"以后要接声音"
    这件事一旦落到代码形状上，就不会在排期里被忘掉。
    """

    name = "http"

    def __init__(self, endpoint: str, voice: str = "", tmp: Path | None = None,
                 timeout: float = 15.0):
        self.endpoint = endpoint
        self.voice = voice
        self.tmp = Path(tmp or (Path.home() / ".buddy_tts.wav"))
        self.timeout = timeout

    def speak(self, text: str) -> bool:
        import json as _json
        import urllib.request
        try:
            req = urllib.request.Request(
                self.endpoint, method="POST",
                data=_json.dumps({"text": text, "voice": self.voice}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                self.tmp.write_bytes(r.read())
            subprocess.Popen(["powershell", "-NoProfile", "-Command",
                              f"(New-Object Media.SoundPlayer '{self.tmp}').PlaySync()"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def stop(self) -> None:
        pass


# ── 输入：STT ───────────────────────────────────────────────────────────────
class STT(Protocol):
    name: str

    def listen(self) -> str | None: ...

    def stop(self) -> None: ...


class NullSTT:
    name = "none"

    def listen(self) -> str | None:
        return None

    def stop(self) -> None:
        pass


class WhisperSTT:
    """本地 whisper（faster-whisper）。没装包时自动降级成空实现，不抛异常。

    真正的按键说话（PTT）由上层决定：这里只负责"录一段 → 出一句文本"。
    录音用 sounddevice（若装了），没装就返回 None。
    """

    name = "whisper"

    def __init__(self, model_size: str = "base", seconds: float = 4.0, device: str = "cpu"):
        self.seconds = seconds
        self._model = None
        self._ok = True
        try:
            from faster_whisper import WhisperModel       # type: ignore
            import sounddevice as sd                       # type: ignore
            self._sd = sd
            self._WhisperModel = WhisperModel
            self._model_size = model_size
            self._device = device
        except Exception:
            self._ok = False

    def _load(self):
        if self._model is None and self._ok:
            self._model = self._WhisperModel(self._model_size, device=self._device,
                                             compute_type="int8")
        return self._model

    def listen(self) -> str | None:
        if not self._ok:
            return None
        try:
            import numpy as np
            sr = 16000
            audio = self._sd.rec(int(self.seconds * sr), samplerate=sr, channels=1,
                                 dtype="float32")
            self._sd.wait()
            m = self._load()
            if m is None:
                return None
            segs, _ = m.transcribe(np.asarray(audio).reshape(-1), language="zh", beam_size=1)
            text = "".join(s.text for s in segs).strip()
            return text or None
        except Exception:
            return None

    def stop(self) -> None:
        pass


class SapiSTT:
    """**Windows 自带的中文语音识别**（System.Speech）——零下载、离线、真能听懂。

    为什么不用 whisper：本机是 Python 3.13，`faster-whisper` 没有轮子；
    whisper.cpp 最新 release 已经**不再发 Windows 二进制**了。
    而这边 `SpeechRecognitionEngine::InstalledRecognizers()` 里**已经装了
    `zh-CN / MS-2052-80-DESK`** —— 那就先用它，接缝留着，以后换 whisper / 云端只换实现。

    做法：起一个**长驻** PowerShell 子进程，里面跑 SpeechRecognitionEngine，
    每识别出一句就往 stdout 打一行；Python 这边按行读进队列。
    好处是识别引擎一直热着（不用每次重新加载），坏处是它依赖 8kHz/16kHz 默认麦克风，
    没有麦克风时会在启动那一步报错 —— 这里捕获并降级成空实现。

    语法：先加一小份**陪玩常用指令**（命中率高），再加 Dictation 兜底自由说话。
    """

    name = "sapi"

    COMMANDS = ["跟着我", "过来", "来我身边", "别干了", "停下", "挖这块", "挖掉这块",
                "把这块挖了", "清怪", "打怪", "给我木头", "给我圆石", "要点吃的",
                "自己找吃的", "你在哪", "你能干什么", "把东西给我", "盖上",

                "follow me", "come here", "stop", "mine this", "kill mobs",
                "give me wood", "where are you"]

    PS = r"""
Add-Type -AssemblyName System.Speech
$ErrorActionPreference = 'Stop'
try {
  $c = [System.Globalization.CultureInfo]::GetCultureInfo('zh-CN')
  $r = New-Object System.Speech.Recognition.SpeechRecognitionEngine($c)
} catch {
  try { $r = New-Object System.Speech.Recognition.SpeechRecognitionEngine } catch { Write-Output "ERR $($_.Exception.Message)"; exit 1 }
}
$words = @(__WORDS__)
# 指令语法走 SRGS 文件（Choices 在中文识别器上会报 "'zh-CN' rule not found or not public"）
try { $r.LoadGrammar((New-Object System.Speech.Recognition.Grammar -ArgumentList '__GRAMMAR__')) } catch { Write-Output "ERR_GRAMMAR $($_.Exception.Message)" }
try { $r.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar)) } catch { Write-Output "WARN_NO_DICTATION $($_.Exception.Message)" }
try { $r.SetInputToDefaultAudioDevice() } catch { Write-Output "ERR_NO_MIC $($_.Exception.Message)"; exit 2 }
$r.InitialSilenceTimeout = [TimeSpan]::FromSeconds(8)
$r.BabbleTimeout = [TimeSpan]::FromSeconds(3)
$r.EndSilenceTimeout = [TimeSpan]::FromSeconds(0.6)
$r.EndSilenceTimeoutAmbiguous = [TimeSpan]::FromSeconds(0.9)
[Console]::Out.WriteLine("READY"); [Console]::Out.Flush()
while ($true) {
  try {
    $res = $r.Recognize([TimeSpan]::FromSeconds(6))
    if ($res -ne $null -and $res.Text -ne $null -and $res.Text.Trim().Length -gt 0) {
      $conf = [math]::Round($res.Confidence, 3)
      [Console]::Out.WriteLine("RES|$conf|" + $res.Text)
      [Console]::Out.Flush()
    }
  } catch { Start-Sleep -Milliseconds 200 }
}
"""

    def __init__(self, log=None, culture: str = "zh-CN", min_conf: float = 0.45):
        self.log = log or (lambda obj: None)
        self.culture = culture
        self.min_conf = min_conf
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str] = queue.Queue(maxsize=64)
        self._thread: threading.Thread | None = None
        self.ok = False
        self.err = ""
        self.heard = 0
        self.rejected = 0
        self.last_conf = 0.0
        self._start()

    def _grammar_file(self) -> Path:
        """SRGS 语法文件：把陪玩常用指令写成一棵规则树（中文识别器只认这个）。"""
        d = Path(__file__).resolve().parent.parent / "runs" / "voice"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "grammar.xml"
        items = "".join(f"<item>{w}</item>" for w in self.COMMANDS)
        p.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<grammar version="1.0" xml:lang="zh-CN" root="cmds" '
            'xmlns="http://www.w3.org/2001/06/grammar">\n'
            f'  <rule id="cmds" scope="public"><one-of>{items}</one-of></rule>\n'
            '</grammar>\n', encoding="utf-8")
        return p

    def _start(self) -> None:
        words = ",".join("'" + w.replace("'", "''") + "'" for w in self.COMMANDS)
        gram = self._grammar_file()
        script = (self.PS.replace("__WORDS__", words)
                  .replace("__GRAMMAR__", str(gram).replace("\\", "\\\\")))
        try:
            self._proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", script],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except Exception as e:
            self.err = f"{type(e).__name__}: {e}"[:160]
            return
        self._thread = threading.Thread(target=self._pump, daemon=True, name="stt")
        self._thread.start()

    def _pump(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = (line or "").strip()
            if not line:
                continue
            if line == "READY":
                self.ok = True
                self.log({"ev": "stt_ready", "engine": "sapi", "culture": self.culture})
                continue
            if line.startswith("ERR"):
                self.err = line[:160]
                self.ok = False
                self.log({"ev": "stt_error", "err": self.err})
                continue
            if line.startswith("WARN"):
                self.log({"ev": "stt_warn", "w": line[:120]})
                continue
            # RES|<置信度>|<文本>
            parts = line.split("|", 2)
            if len(parts) == 3 and parts[0] == "RES":
                try:
                    conf = float(parts[1])
                except Exception:
                    conf = 0.0
                text = parts[2].strip()
                # 静音时 DictationGrammar 会**凭空造词**（实测吐了个"然而"，置信度极低）。
                # 陪玩宁可听不见，也不能对着幻觉瞎答应。
                if len(text) < 2 or conf < self.min_conf:
                    self.rejected += 1
                    continue
                self.heard += 1
                self.last_conf = conf
                try:
                    self._lines.put_nowait(text)
                except queue.Full:
                    pass
                continue
            try:
                self._lines.put_nowait(line)
            except queue.Full:
                pass
        self.ok = False

    def listen(self, timeout: float = 12.0) -> str | None:
        if not self._proc:
            return None
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None


# ── 门面 ────────────────────────────────────────────────────────────────────
class VoiceIO:
    """把 TTS + STT 包成一个可开关的东西。`enabled=False` 时一切调用都是空操作。"""

    def __init__(self, tts: TTS | None = None, stt: STT | None = None,
                 enabled: bool = False, log=None):
        self.tts = tts or NullTTS()
        self.stt = stt or NullSTT()
        self.enabled = enabled
        self.log = log or (lambda obj: None)
        self.spoken = 0
        # 防自听：它说话的时候不要听自己（2026-10-29）
        self._mute = threading.Event()
        # `_mute` 是"正在说话"的旗（set=闭嘴期）；`_speech_done` 是"说完了"的旗。
        # 为什么要两个：`Event.wait()` 在**旗已经 set** 时**立刻返回**，所以拿 `_mute.wait(2.0)`
        # 当"最多等 2 秒"是错的（2026-10-29 探针实测：20 次 stt_muted 在 1.9 秒内打完，
        # 等于没等，6Hz 的指挥层在说话期间空转）。要等的是"旗变回 clear"，
        # 那只能等 `_speech_done`。初始 set = 当前没在说话。
        self._speech_done = threading.Event()
        self._speech_done.set()

    def _watch_speech(self) -> None:
        """等 TTS 真正说完（`busy` 为假）再重新开麦。"""
        try:
            while getattr(self.tts, "busy", False) and self.enabled:
                time.sleep(0.05)
        finally:
            self._mute.clear()
            self._speech_done.set()

    @classmethod
    def auto(cls, enabled: bool = True, log=None) -> "VoiceIO":
        """尽力接上能用的实现；哪个接不上就静默降级。

        顺序：SAPI STT（本机 zh-CN 已装、零下载）→ whisper（有包时）→ 空实现。
        """
        tts: TTS = NullTTS()
        stt: STT = NullSTT()
        if enabled:
            # ① 先问"选型层"（sherpa / cosyvoice / volc），它自己会兜底 SAPI
            try:
                from companion import voice_engine as VE
                tts, tts_name = VE.make_tts(log=log)
                stt, stt_name = VE.make_stt(log=log)
                try:
                    time.sleep(1.2)              # SAPI 要等 READY
                except Exception:
                    pass
                if log:
                    log({"ev": "voice_ready", "tts": tts_name, "stt": stt_name,
                         "stt_ok": bool(getattr(stt, "ok", False))})
                return cls(tts=tts, stt=stt, enabled=enabled, log=log)
            except Exception as e:
                if log:
                    log({"ev": "voice_engine_missing", "err": f"{type(e).__name__}: {e}"[:120]})
            # ② 兜底：原来那套（SAPI）
            try:
                tts = SapiTTS()
            except Exception:
                tts = NullTTS()
            try:
                sapi = SapiSTT(log=log)
                time.sleep(1.5)                      # 等它打 READY
                stt = sapi if sapi.ok else NullSTT()
            except Exception:
                stt = NullSTT()
            if isinstance(stt, NullSTT):
                try:
                    cand = WhisperSTT()
                    stt = cand if getattr(cand, "_ok", False) else NullSTT()
                except Exception:
                    stt = NullSTT()
        return cls(tts=tts, stt=stt, enabled=enabled, log=log)

    def speak(self, text: str) -> bool:
        if not self.enabled:
            return False
        ok = self.tts.speak(text)
        if ok:
            self.spoken += 1
            # 防自听：从"开始说"到"真的说完"这段时间关麦
            if not self._mute.is_set():
                self._speech_done.clear()
                self._mute.set()
                threading.Thread(target=self._watch_speech, daemon=True,
                                 name="tts-mute").start()
            self.log({"ev": "tts", "text": text[:80]})
        return ok

    def listen(self) -> str | None:
        if not self.enabled:
            return None
        if self._mute.is_set():
            # 它正在说话：这段时间**不听**（否则会把自己的话当指令）。
            # 最多阻塞 2 秒（等 `_speech_done`，不是等 `_mute` —— 后者已 set，wait 会立刻返回），
            # 让上层 6Hz 循环不至于在说话期间空转。
            self._speech_done.wait(2.0)
            self.log({"ev": "stt_muted"})
            return None
        t0 = time.time()
        text = self.stt.listen()
        if text:
            self.log({"ev": "stt", "text": text[:120], "sec": round(time.time() - t0, 1)})
        return text

    def stats(self) -> dict:
        return {"enabled": self.enabled, "tts": self.tts.name, "stt": self.stt.name,
                "spoken": self.spoken}

    def close(self) -> None:
        try:
            self.tts.stop()
            self.stt.stop()
        except Exception:
            pass


def _resample_linear(x, sr_in: int, sr_out: int):
    """线性重采样（够用且零依赖）：设备 44.1k/48k → 模型 16k。

    为什么要它：PortAudio 不一定允许在 44.1k 设备上直接开 16k 流（WASAPI 共享模式下
    常报 Invalid sample rate）。先用设备原生率录，再在内存里重采样，最稳。
    """
    if sr_in == sr_out:
        return x
    import numpy as np
    n_out = int(round(len(x) * sr_out / float(sr_in)))
    if n_out <= 0:
        return x[:0]
    idx = np.linspace(0, len(x) - 1, n_out)
    return np.interp(idx, np.arange(len(x)), x).astype("float32")


def resolve_input_device(spec) -> int | None:
    """把配置里的设备（索引或名字片段）解析成 sounddevice 的设备号。

    本机有 13 个输入设备（含 ToDesk 虚拟声卡、"主声音捕获"回环）——
    选错的典型症状是"听见自己说话"或"什么都听不见"。所以允许按名字片段挑。
    """
    try:
        import sounddevice as sd
    except Exception:
        return None
    if spec is None or spec == "":
        return None
    if isinstance(spec, int) or (isinstance(spec, str) and spec.isdigit()):
        return int(spec)
    want = str(spec).lower()
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0 and want in str(d.get("name", "")).lower():
            return i
    return None


def _looks_like_speech(text: str) -> tuple[bool, str]:
    """过滤掉"静音/噪声被识别成怪字"的结果（2026-10-29 实测：纯静音解出 '그.'）。

    规则（宁严勿松，误拒只是没听见，误收会让 AI 去执行莫名其妙的指令）：
      · 至少 2 个字符；
      · 至少 60% 的字符是中文/英文字母/数字；
      · 不能是纯标点。
    """
    t = str(text or "").strip()
    if len(t) < 2:
        return False, "too_short"
    good = sum(1 for ch in t if ("\u4e00" <= ch <= "\u9fff") or ch.isalpha() or ch.isdigit())
    if good / max(1, len(t)) < 0.6:
        return False, "not_text"
    if good < 2:
        return False, "no_letters"
    return True, ""


class SherpaSTT:
    """sherpa-onnx 本地识别（SenseVoice 短句 / Zipformer 流式）。

    为什么选它（2026-10-29 选型）：
      · 纯 ONNX、CPU 可跑、Windows 有 wheel（`pip install sherpa-onnx`）；
      · SenseVoice-small int8 对中文短句又快又准（社区实测百毫秒级）；
      · 想要"边说边出字/能打断"就用 streaming zipformer 模型目录。
    依赖缺失时**明确失败**（由 voice_engine 兜底回 SAPI），绝不假装在听。
    """

    name = "sherpa"

    def __init__(self, model_dir: str = "", log=None, *, sample_rate: int = 16000,
                 threshold: float = 0.012, device=None, silence_chunks: int = 6):
        self.log = log or (lambda o: None)
        self.ok = False
        self.err = ""
        self.heard = 0
        self.rejected = 0
        self.last_conf = 0.0
        self._rec = None
        try:
            import numpy as np                      # noqa: F401
            import sounddevice as sd                # noqa: F401
            import sherpa_onnx
        except Exception as e:
            self.err = f"缺依赖：{type(e).__name__}（pip install sherpa-onnx sounddevice numpy）"
            raise RuntimeError(self.err) from e
        d = str(model_dir or "")
        if not d:
            raise RuntimeError("没有配置 sherpa 模型目录（config/voice.json 的 sherpa_model_dir）")
        import sherpa_onnx
        # 目录里找 onnx：SenseVoice 单模型；streaming 则是 encoder/decoder/joiner 三件套
        p = Path(d)
        onnx = sorted(str(x) for x in p.rglob("*.onnx"))
        try:
            if any("sense" in Path(x).name.lower() or "model.int8" in Path(x).name.lower()
                   for x in onnx):
                self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=onnx[0], tokens=str(next(p.rglob("tokens.txt"))),
                    num_threads=2, use_itn=True)
                self.mode = "sensevoice"
            else:
                raise RuntimeError(f"目录里没认出模型：{p}")
        except Exception as e:
            self.err = f"{type(e).__name__}: {e}"[:160]
            raise
        self._sd, self._np = sd, __import__("numpy")
        self.sample_rate, self.threshold = sample_rate, threshold
        self.silence_chunks = max(2, int(silence_chunks))
        self.device = resolve_input_device(device)
        if device not in (None, "") and self.device is None:
            self.log({"ev": "stt_device_missing", "want": device,
                      "note": "按名字没找到，改用系统默认输入"})
        self.ok = True

    def listen(self) -> str | None:
        """录一小段（能量 VAD 断句）→ 识别。没有语音返回 None。"""
        if not self.ok:
            return None
        np, sd = self._np, self._sd
        # 用设备原生采样率录（44.1k/48k），再重采样到模型要的 16k —— 避免 Invalid sample rate
        try:
            dev_rate = int(sd.query_devices(self.device, kind="input")["default_samplerate"])
        except Exception:
            dev_rate = self.sample_rate
        chunk = max(160, int(dev_rate * 0.1))
        frames, speaking, silence = [], False, 0
        try:
            with sd.InputStream(samplerate=dev_rate, channels=1, dtype="float32",
                                device=self.device) as stream:
                for _ in range(600):                       # 最多 60 秒
                    data, _ = stream.read(chunk)
                    x = data[:, 0]
                    rms = float(np.sqrt(np.mean(x ** 2)))
                    if rms > self.threshold:
                        speaking, silence = True, 0
                        frames.append(x.copy())
                    elif speaking:
                        silence += 1
                        frames.append(x.copy())
                        if silence >= self.silence_chunks:  # 默认 0.6s 静音 = 说完
                            break
        except Exception as e:
            self.log({"ev": "stt_error", "err": f"{type(e).__name__}: {e}"[:120]})
            return None
        if not frames:
            return None
        # 只说了一点点（<0.3s）就别解码了：大概率是咳嗽/碰撞声，解出来全是怪字
        voiced = sum(1 for _ in range(0))  # 占位（下面用块数判断）
        if len(frames) < 3:
            self.rejected += 1
            self.log({"ev": "stt_reject", "why": "too_short_audio", "chunks": len(frames)})
            return None
        audio = np.concatenate(frames).astype(np.float32)
        audio = _resample_linear(audio, dev_rate, self.sample_rate)
        try:
            s = self._rec.create_stream()
            s.accept_waveform(self.sample_rate, audio)
            self._rec.decode_stream(s)
            text = (s.result.text or "").strip()
        except Exception as e:
            self.log({"ev": "stt_decode_error", "err": f"{type(e).__name__}: {e}"[:120]})
            return None
        ok_text, why = _looks_like_speech(text)
        if not ok_text:
            self.rejected += 1
            self.log({"ev": "stt_reject", "why": why, "text": text[:40]})
            return None
        self.heard += 1
        return text

    def transcribe(self, audio, sample_rate: int = 16000) -> tuple[str, float]:
        """识别一段**内存里的**音频（float32 mono）。返回 (文本, 耗时秒)。

        给"离线准确率基准/回归"用：不碰麦克风，只走模型 —— 换模型或改参数时
        能用同一批 WAV 前后对照（`tools/_voice_bench.py`），不用人再念 20 句。
        """
        t0 = time.time()
        if not self.ok:
            return "", 0.0
        s = self._rec.create_stream()
        s.accept_waveform(int(sample_rate), audio)
        self._rec.decode_stream(s)
        text = (s.result.text or "").strip()
        return text, round(time.time() - t0, 3)

    def stop(self) -> None:
        self.ok = False


class CosyVoiceTTS:
    """CosyVoice2 本地合成（流式、零样本音色）。没装就明确失败，由上层兜底 SAPI。

    用法（装好以后）：
        pip install -r CosyVoice/requirements.txt  # 按官方仓库
        config/voice.json: {"tts":"cosyvoice","cosyvoice_dir":"%MODELS%"}
    """

    name = "cosyvoice"

    def __init__(self, model_dir: str = "", log=None):
        self.log = log or (lambda o: None)
        self._q: queue.Queue[str] = queue.Queue(maxsize=32)
        self._stop = False
        if not model_dir or not Path(model_dir).exists():
            raise RuntimeError("cosyvoice_dir 不存在")
        try:
            from cosyvoice.cli.cosyvoice import CosyVoice2      # type: ignore
        except Exception as e:
            raise RuntimeError(f"没装 CosyVoice 包：{type(e).__name__}") from e
        self._cv = CosyVoice2(model_dir, load_jit=False, load_trt=False, fp16=False)
        self._t = threading.Thread(target=self._run, daemon=True, name="cosy-tts")
        self._t.start()

    def speak(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        try:
            self._q.put_nowait(t[:300])
            return True
        except queue.Full:
            return False

    def _run(self) -> None:
        while not self._stop:
            try:
                text = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                for _i, out in enumerate(self._cv.inference_sft(text, "中文女", stream=False)):
                    wav = out.get("tts_speech")
                    if wav is None:
                        continue
                    import tempfile
                    import winsound
                    p = Path(tempfile.gettempdir()) / "buddy_tts.wav"
                    import torchaudio  # type: ignore
                    torchaudio.save(str(p), wav, self._cv.sample_rate)
                    winsound.PlaySound(str(p), winsound.SND_FILENAME)
            except Exception as e:
                self.log({"ev": "tts_error", "err": f"{type(e).__name__}: {e}"[:120]})

    def stop(self) -> None:
        self._stop = True


if __name__ == "__main__":                       # 自测：python -m companion.voice_io
    v = VoiceIO.auto(enabled=True)
    print("stats:", v.stats())
    v.speak("你好，我是你的陪玩，测试一下声音。")
    time.sleep(4)
    print("done")
