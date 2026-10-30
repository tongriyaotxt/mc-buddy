#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/_voice_probe.py —— 不接 MC，直接验"能不能听见、听成什么"

用途（装完模型第一件事）：
    python -X utf8 tools/_voice_probe.py            # 录一句，打印设备/音量/识别结果
    python -X utf8 tools/_voice_probe.py --raw      # 只测麦克风音量（不加载模型）
    python -X utf8 tools/_voice_probe.py --device "Microphone Array"

判据：
  · 说话时 RMS 应该明显高于静音（默认门限 0.012）；
  · 识别文本应与你说的话接近（同音字可接受，语义要对）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--raw", action="store_true")
    a = ap.parse_args()

    try:
        import numpy as np
        import sounddevice as sd
    except Exception as e:
        print(f"❌ 缺依赖：{type(e).__name__}（pip install sounddevice numpy）")
        return 2

    from companion.voice_io import resolve_input_device
    dev = resolve_input_device(a.device or None)
    info = sd.query_devices(dev, kind="input")
    print(f"设备：#{dev if dev is not None else 'default'} {info['name']} "
          f"@ {int(info['default_samplerate'])}Hz")

    rate = int(info["default_samplerate"])
    print(f"请说话…（录 {a.seconds:.0f} 秒）")
    rec = sd.rec(int(rate * a.seconds), samplerate=rate, channels=1,
                 dtype="float32", device=dev)
    sd.wait()
    x = rec[:, 0]
    rms = float(np.sqrt(np.mean(x ** 2)))
    peak = float(np.max(np.abs(x)))
    print(f"音量：RMS={rms:.4f} 峰值={peak:.3f}  "
          f"{'✅ 有声音' if rms > 0.012 else '⚠️ 太小（换设备/调增益）'}")

    if a.raw:
        return 0
    from companion import voice_engine as VE
    cfg = VE.load_cfg()
    stt, name = VE.make_stt(log=lambda o: print(" ", o))
    print(f"识别引擎：{name}")
    if name != "sherpa":
        print("⚠️ 现在还是 SAPI（没配模型）。按 tools/get_voice_model.py --guide 配好再来。")
        return 0
    t0 = time.time()
    text = stt.listen()
    print(f"识别结果：{text!r}  用时 {time.time()-t0:.1f}s")
    return 0 if text else 1


if __name__ == "__main__":
    raise SystemExit(main())
