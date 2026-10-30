# mc-buddy

**A Minecraft companion AI that decides for itself what needs doing.**

> for my brother xiaorun — so he never has to play alone.

This is not a scripted bot, and not a chat model wearing a robot suit. There are no learned
weights in the decision path:

- **Hands** live inside a Fabric mod (AltoClef + Tungsten) and are exposed as **89 MCP tools**.
- **Head** is Python (`companion/`): read state, plan, issue commands, check what actually happened.
- **Mouth** is the same Python process, rate-limited (6 s) and de-duplicated (90 s), speaking over
  chat and TTS.
- **Proof** is not "it looked right". It is a replay black box plus a **32-item self-check gate**.

The goal is small and hard: **behave like a person playing next to you — follow, take a hit for you,
do the chore, stay quiet, and never just stand there.**

---

## What it does today (readings, not claims)

| Capability | Status | Reading |
|---|---|---|
| Follow / give way / nod / answer / guard | ✅ | Gate ③④⑪ green; "guard preempts, answering does not" has its own test |
| Speak (chat + system TTS) | ✅ | 6 s rate limit / 90 s de-dup / per-line cap |
| Listen (local SenseVoice STT) | ✅ | Fallback chain sapi→sherpa→cosyvoice→volcengine; two silence filters |
| Never obey its own voice | ✅ | 0 leaks while muted; **the control arm with the muting removed must fail** |
| Three companions on one server | ✅ | 3 online / 3 heartbeats / **only the one holding the speaking token acts** / one voice stream |
| Goal decomposition, prerequisites, fallbacks | ✅ | Gate ⑮ (5 checks), ⑯ (7 checks), ⑫ (no cow → no leather → step down to iron → wooden sword) |
| Dig / place (the "ok but nothing changed" class) | ✅ | Grass block: progress 0.3→0.9→air within 1 s; three distinct placement failures |
| No more fleeing from an empty field | ✅ | With nothing hostile around the chain declines to bid: **0** `fleeLive(0 danger)` lines in 45 s (23 before the fix); with real mobs it still flees (`(2 danger(s))`) |
| A headless client that still works | ✅ | Window 1 px offscreen, crosshair still tracks (render loop is alive) |
| Long run | ✅ | 120 min: survival **1.0**, RSS slope **0.12 MB/min**, `error`/`incident`/`death` all 0 |
| Packaged exe (no Python needed) | ✅ | `Buddy.exe` 74.8 MB, `--selftest` OK, `--run-py` channel works |
| Self-check gate | ✅ | **32/32** in ~45 s (`tools/companion_gate.py`); live items report SKIP when no client is up |

**Five half-green items** (they work, they just have not been re-verified in the field):
guard melee leg, launcher, console v2, melee clearing, job-stall detection.
Full ledger: [`companion/capabilities.yaml`](companion/capabilities.yaml) — 39 items: 34 green, 5 half.

---

## Why it is built this way

1. **The hands have to live in the game.** Driving a player over the wire is always one step short:
   break feel, collision boxes, the 4-tick rate gate, screen focus — all of it is client-internal
   semantics. So the hands are a mod.
2. **The head has to live outside.** Planning, speech, multi-bot coordination and failure triage
   need second-level iteration. Putting them in Java means restarting the game per change. The head
   is Python, talking MCP to those 89 tools.
3. **"Runs" and "runs correctly" are tested separately.** The expensive bugs in this project were all
   of one shape: *the call returned ok and the world did not change* (placement that reported success,
   digging cancelled by vanilla, a death screen nobody could close, a flee from an empty field).
   Every action's verdict is therefore taken from **world state**, never from a return value.

---

## Quick start

Requirements: Windows 10/11, Python 3.11+, JDK 21, a Fabric 1.21.11 client (or the bundled
one-click setup in the launcher).

```powershell
# 0) deps
python -m pip install -r requirements.txt

# 1) server (self-hosted — public servers ban automation, see docs/SECURITY.md)
python -X utf8 tools\server_warden.py ensure --port 25566 --wait-sec 300 --settle-sec 75

# 2) body: a headless client that joins (⚠️ pass --server; the default port is NOT 25566)
python -X utf8 tools\buddy_body.py restart --index 1 --offscreen --server 127.0.0.1:25566
python -X utf8 tools\buddy_body.py status  --index 1 --json

# 3) head: the companion command layer
python -X utf8 -m companion.commander --port 25351 --player Xiaorun --seconds 3600 --voice

# 4) the 32-item gate (~45 s) before you trust any of it
python -X utf8 tools\companion_gate.py
```

### Things you can say

```
follow me / come here / stop following / dig this / clear the mobs / give me wood / some food
where are you / what can you do / it's getting dark / come back
```

Commands are parsed by rules first — fast and predictable. Only what the rules cannot place is
handed to a model (DeepSeek), and the model only **suggests**. Models hallucinate; hands do not.

---

## Layout

```
companion/  head: planner / speech / team / reflexes / task layer (Python)
launcher/   console: instances, jar install, launch args, diagnostics, Qt UI
tools/      runtime tools: gate, body, long-run, release, model fetch
config/     config templates (real config is never committed)
java/       mod-side patches (GPL-3.0, drop-in over a UnionClef 0.95.63 tree)
docs/       quickstart / architecture / security / FAQ
```

---

## Known limits (stated here, so they are not issues)

- **Do not use this on public servers.** Most treat automation as macros and ban for it. Singleplayer,
  your own server, or a friends' server only.
- **No human acceptance run yet.** 30-minute live sessions ×3 and 20 spoken commands still need a person.
- Riding / fishing / trading / enchanting are not verified one by one.
- Windows only (the launcher uses Win32 desktop/session APIs).

---

## Licence

- Our own code (`companion/`, `launcher/`, `tools/`, `docs/`): **Apache-2.0** — see [LICENSE](LICENSE).
- The patches under `java/` are derivative work of **UnionClef** (AltoClef + Tungsten), which is
  **GPL-3.0**; those patches are therefore **GPL-3.0** as well. See [NOTICE](NOTICE).
- Third-party inventory: [THIRD-PARTY.md](THIRD-PARTY.md).

---

## Credits

The AltoClef / Baritone / Tungsten authors solved the hard part — making a block person actually
walk. We only hung a head on that skeleton that asks *what do you need me to do*.
