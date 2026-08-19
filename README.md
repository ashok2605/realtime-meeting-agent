# Gladiator

A realtime meeting assistant that joins a Google Meet as a **Recall.ai** bot, holds a natural
spoken conversation powered end-to-end by **Gemini Live** (audio-native, no other LLM), and runs a
post-sales technical discovery — theme: *implementing Salesforce for an automotive company*. As
participants answer, Gladiator records every answer to a live dashboard **via Gemini tool calls**,
with per-turn latency waterfalls and a full structured event log.

## Assignment test cases → where they're handled

| Test case | Mechanism | Evidence |
|---|---|---|
| Responds to "Gladiator" and "Glad" | persona + naming rules in the system instruction | `tests/test_gemini_integration.py::test_responds_to_both_names` (real API, spoken fixtures) |
| 5–10 preset discovery questions | 8 questions in `core/discovery.py`, worked through conversationally | dashboard checklist |
| Dashboard updated via tool calls | `record_answer` / `mark_question_asked` / `mark_question_skipped` / `set_meeting_phase` function declarations | `::test_spoken_answer_triggers_record_answer_tool` |
| Web search | `google_search` grounding inside the Live session (combined-tools config verified against the real API by `gladiator doctor`) | `::test_web_search_used_when_asked` |
| Glad never interrupts a speaker | `FloorGate` on Recall's speaker-attributed `speech_on/off` events + grace period + stale-drop + hard-stop watchdog | `tests/test_turn_gate.py`, `tests/test_stale_policy.py` |
| Anyone can interrupt Glad | Gemini VAD barge-in → `interrupted` → server buffer epoch-flush + page buffer flush in one hop | `::test_barge_in_interrupts_and_flushes`, `tests/test_receive_loop.py` |
| Smooth, non-jittery audio | AudioWorklet playback (sample-accurate), server-side pacer with a ~160 ms primed jitter buffer, `web_4_core` bot | `tests/test_latency.py::test_pacer_keeps_bounded_head_start` |

## Architecture (one asyncio process + one tunnel)

```
Google Meet ◀━━ Recall bot (web_4_core headless Chrome) ━━━━━━━━━━━┓
  ▲   │  renders /agent page = Gladiator's face + audio I/O          ┃ ngrok (wss)
  ┃   ├─ page "mic" = meeting audio → AudioWorklet → 20ms PCM ───────╂──▶ /ws/agent ─┐
  ┃   └─ AudioWorklet player ◀── paced 24k PCM + flush ──────────────╂─── /ws/agent ◀┤
  ┗━ Recall realtime WS: participant speech_on/off, join/leave ──────╂──▶ /ws/recall ┤
                                                                     ┛               │
  gladiator server (FastAPI) ────────────────────────────────────────────────────────┤
    GeminiSession   Live API: audio↔audio, tools, transcription,                     │
                    context compression + session resumption + GoAway reconnects     │
    receive loop ──▶ TurnAudioBuffer (unbounded, epoch-flushed) ──▶ Pacer ──▶ page   │
    FloorGate       agent audio waits for human silence; stale answers get dropped   │
    DiscoveryTracker / LatencyTracker / EventBus (JSONL log)                         │
    Dashboard (localhost) ◀──────────────────────────────────────────────────────────┘
```

Everything runs locally; the tunnel exists only because Recall's bot lives in Recall's cloud and
must reach the page + websockets (Recall blocks `localhost` inside the bot's browser).

## Quickstart

```bash
uv sync                      # Python 3.12 env + deps
cp .env.example .env         # fill in RECALL_API_KEY, GEMINI_API_KEY
uv run gladiator doctor      # verifies keys, model access, combined-tools Live session
```

**Local test (no meeting, no tunnel):** talk to Glad through your own mic —

```bash
uv run gladiator run --local-test
# open the printed /agent URL in Chrome, allow the mic, start talking;
# open the printed /dashboard URL next to it.
```

**Full demo (real Google Meet):**

1. One-time: free ngrok account → authtoken + claim your free static domain
   → put `NGROK_AUTHTOKEN` and `NGROK_DOMAIN` in `.env`.
2. Start a Meet, then:

```bash
scripts/run_demo.sh https://meet.google.com/xxx-yyyy-zzz
```

3. Admit **Gladiator** when it knocks. It greets the room only after admission
   (never the waiting room) and starts the discovery. Watch the dashboard.

To pull the bot out: dashboard **Leave meeting** button, ask it ("Glad, you can drop off"),
Ctrl+C, or `uv run gladiator leave`.

## CLI

| command | purpose |
|---|---|
| `gladiator doctor` | validate keys, region, model, combined-tools Live config, tunnel |
| `gladiator run --meeting-url URL` | server + bot into the meeting |
| `gladiator run --local-test` | no bot; your browser mic is the "meeting" |
| `gladiator bench -n 8 [--wav f.wav]` | time-to-first-audio distribution against Gemini Live |
| `gladiator replay runs/<ts>/events.jsonl [--speed 8]` | re-drive the dashboard from a recorded log |
| `gladiator leave [--bot-id ID]` | eject the last bot |

## Dashboard panels

- **Discovery questions** — live checklist driven purely by Gemini tool calls: status
  (pending/asked/partial/answered/skipped), summary, verbatim quote, confidence, revision count.
  Per-question skip control.
- **Live transcript** — Gemini's input/output audio transcription, speaker-side attributed.
- **Latency waterfall** — per agent turn: `detect+think` (human stops → first model audio byte) /
  `gate+serve` (floor-gate hold + pacing) / `transit+play` (tunnel + page buffer, measured with
  NTP-style page↔server clock sync), plus rolling p50/p95 and token usage.
- **Event log** — every event in the system (gate decisions, tool calls, session resumptions,
  bot status, flushes), filterable, downloadable; the same JSONL that `gladiator replay` consumes.
- **Controls** — leave / mute agent / greet / nudge / skip question.

## Tests

```bash
uv run pytest                     # 36 unit tests, no network
scripts/record_fixtures.sh        # one-time: spoken fixtures via macOS `say`
RUN_INTEGRATION=1 uv run pytest   # + 5 tests against the real Gemini Live API
```

The integration tests exercise the production stack with spoken audio and are recorded evidence
for the visible test cases. `tests/test_e2e_ws.py` goes furthest: a synthetic Output Media page
speaks the page's exact websocket wire protocol against the real server + real Gemini session —
greeting audio delivery, a spoken answer triggering `record_answer`, and a fully-assembled
latency waterfall — everything a live meeting exercises except Recall itself.

Measured with `gladiator bench --wav tests/fixtures/hi_glad.wav`: **~0.9 s median** from
end-of-speech to first model audio; the meeting path adds ~0.2–0.4 s of transit + jitter buffer
(see the waterfall for the live breakdown per turn).

## Repo map

```
src/gladiator/
  config.py            all knobs (VAD, gate, pacing, model) with rationale comments
  main.py              CLI: doctor | run | bench | leave | replay
  runtime.py           wiring + background tasks (watchdogs, bot status, silence nudge)
  server.py            FastAPI routes (token-gated; Gemini key never leaves the server)
  gemini/session.py    Live session: receive loop, reconnect/resumption, GoAway
  gemini/tools.py      tool declarations + dispatch (handlers mutate local state only)
  gemini/prompts.py    system instruction, greeting/reconnect/silence nudges
  audio/bridge.py      TurnAudioBuffer (epoch flush), Pacer, EnergyVAD, inbound queue
  core/turn_gate.py    SpeechState + FloorGate (never-interrupt guarantees)
  core/discovery.py    the 8 questions + answer state machine
  core/latency.py      clock sync + per-turn waterfall assembly
  core/events.py       EventBus → ring + JSONL + dashboard subscribers
  web/agent/           bot-rendered page: capture/playback worklets + status face
  web/dashboard/       mission control (vanilla JS, event-sourced, replay-compatible)
docs/DESIGN.md         decisions, trade-offs, failure modes, hidden-test hardening
```
