#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""companion/voice_engine.py —— 语音引擎的"选型 + 兜底"层（2026-10-29）

operator：「语音上网上找现在效果最好的」。结论（见 docs/VOICE-STACK-2026-10-29.md）：

  ASR（中文、实时、Windows、本地）
    第一梯队：**sherpa-onnx + SenseVoice-small**（FunAudioLLM；ONNX int8，CPU 上短句 ~100~200ms）
              **sherpa-onnx + Zipformer streaming**（真流式，可做"打断"）
    备选：FunASR Paraformer（精度稳、部署稍重）／云端（火山/阿里/腾讯，200~300ms，依赖网络）
    今天仍在用的兜底：Windows SAPI（MS-2052-80-DESK，零依赖，非流式）

  TTS（中文、自然、低延迟）
    第一梯队（本地）：**CosyVoice2**（流式、零样本音色，Apache-2.0）
    第一梯队（云端）：**豆包/火山 实时语音大模型**（端到端、支持打断）
    工业零样本：IndexTTS / IndexTTS2（质量高，非流式）
    今天的兜底：Windows SAPI（Microsoft Huihui zh-CN；**每句 ~0.9s 的进程开销**是最大毛病）

这个文件只做三件事：
  1. 按配置/可用性挑引擎（**永远保留 SAPI 兜底**，装不上也不能哑）；
  2. 把"引擎名 + 为什么选它/为什么没用上"写进日志（面板能看到）；
  3. 给上层一个统一的 TTS/STT 对象（协议不变）。
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config" / "voice.json"

DEFAULTS = {
    "tts": "auto",          # auto | sapi | cosyvoice | volc | http
    "stt": "auto",          # auto | sapi | sherpa | volc
    "sherpa_model_dir": "",  # 指向 sherpa-onnx 模型目录（SenseVoice 或 streaming zipformer）
    "cosyvoice_dir": "",     # 指向 CosyVoice2 仓库/模型目录
    "volc_app_id": "",
    "volc_token": "",
    "input_device": "",      # 麦克风：留空=系统默认；也可写索引或名字片段（如 "Microphone Array"）
    "silence_chunks": 6,     # 说完判定：几个 100ms 静音块（6=0.6s；想更快设 3）
    "vad_threshold": 0.012,  # 能量门限（环境吵就调高）
    "tts_rate": 5,           # 语速 -10~10（实测：rate2 一句 5.2s / rate5 3.8s / rate8 2.7s）
    "tts_volume": 100,
}


def load_cfg() -> dict:
    try:
        d = json.loads(CFG.read_text(encoding="utf-8"))
        return {**DEFAULTS, **d}
    except Exception:
        return dict(DEFAULTS)


def has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def pick_tts(cfg: dict | None = None) -> tuple[str, str]:
    """返回 (引擎名, 原因)。**任何异常都退回 sapi**。"""
    cfg = cfg or load_cfg()
    want = str(cfg.get("tts") or "auto")
    have_sherpa, have_http = has("sherpa_onnx"), has("urllib.request")
    cosy_dir = str(cfg.get("cosyvoice_dir") or "")
    if want == "cosyvoice" or (want == "auto" and cosy_dir and Path(cosy_dir).exists()):
        if cosy_dir and Path(cosy_dir).exists():
            return "cosyvoice", f"CosyVoice2 目录存在：{cosy_dir}"
        return "sapi", "配了 cosyvoice 但目录不存在 → 兜底 SAPI"
    if want == "volc" and cfg.get("volc_token"):
        return "volc", "配了火山 token"
    if want == "volc":
        return "sapi", "选了火山但没 token → 兜底 SAPI"
    return "sapi", "默认零依赖 SAPI（中文 Huihui）；要更好听配 cosyvoice/volc"


def pick_stt(cfg: dict | None = None) -> tuple[str, str]:
    cfg = cfg or load_cfg()
    want = str(cfg.get("stt") or "auto")
    model_dir = str(cfg.get("sherpa_model_dir") or "")
    if want == "sherpa" or (want == "auto" and model_dir):
        if not has("sherpa_onnx"):
            return "sapi", "没装 sherpa-onnx（pip install sherpa-onnx）→ 兜底 SAPI"
        if not (model_dir and Path(model_dir).exists()):
            return "sapi", "sherpa 模型目录不存在 → 兜底 SAPI"
        return "sherpa", f"sherpa-onnx + {Path(model_dir).name}"
    if want == "volc" and cfg.get("volc_token"):
        return "volc", "配了火山实时 ASR"
    return "sapi", "默认 Windows 中文识别器（MS-2052-80-DESK，非流式）"


def make_tts(log=None):
    """按选型造一个 TTS。失败一律回 SAPI（绝不哑）。"""
    from companion import voice_io as V
    cfg = load_cfg()
    name, why = pick_tts(cfg)
    if log:
        log({"ev": "voice_engine", "kind": "tts", "engine": name, "why": why})
    try:
        if name == "cosyvoice":
            return V.CosyVoiceTTS(cfg.get("cosyvoice_dir") or "", log=log), name
        if name == "volc":
            return V.HttpTTS(), name          # 形状先摆好，填 key 即用
    except Exception as e:
        if log:
            log({"ev": "voice_engine_failed", "kind": "tts", "err": f"{type(e).__name__}: {e}"[:120]})
    return V.SapiTTS(rate=int(cfg.get("tts_rate") or 5),
                     volume=int(cfg.get("tts_volume") or 100)), "sapi"


def make_stt(log=None):
    from companion import voice_io as V
    cfg = load_cfg()
    name, why = pick_stt(cfg)
    if log:
        log({"ev": "voice_engine", "kind": "stt", "engine": name, "why": why})
    try:
        if name == "sherpa":
            return V.SherpaSTT(cfg.get("sherpa_model_dir") or "", log=log,
                               device=cfg.get("input_device") or None,
                               silence_chunks=int(cfg.get("silence_chunks") or 6),
                               threshold=float(cfg.get("vad_threshold") or 0.012)), name
    except Exception as e:
        if log:
            log({"ev": "voice_engine_failed", "kind": "stt", "err": f"{type(e).__name__}: {e}"[:120]})
    return V.SapiSTT(log=log), "sapi"
