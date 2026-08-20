# Gladiator — Codebase Walkthrough

A guided tour for defending every line of this code in a technical interview.
Read top to bottom: each section assumes only the ones before it.

- §0 What we built and why it's shaped this way
- §1 Prerequisite concepts (read cold, then any file makes sense)
- §2 Execution traces (every flow, hop by hop, with latency capture points)
- §3 File map (every file, every function)
- §4 Design decisions (decision / rejected alternative / one-line defense)
- §5 Invariants and failure modes (and which test guards each)
- §6 Glossary
- §7 Interview prep (15 likely questions with answers)

---

## §0 The problem and our approach

### The assignment

Build "Gladiator": a realtime meeting assistant that joins a Google Meet via
**Recall.ai** and holds a natural spoken conversation whose goal is a post-sales
technical discovery (our theme: implementing Salesforce for an automotive company).
Hard requirements: responds to both "Gladiator" and "Glad"; works through 5–10
preset discovery questions; answers land on a dashboard **via tool calls** as they
happen; can use web search; never interrupts a speaking human; any human can
interrupt it; output audio is smooth and non-jittery. All reasoning/tool-calling by
**Gemini Live only** — no other LLM. Local demo is sufficient. Graded on code
quality, latency breakdown & visibility, the monitoring dashboard, open+hidden test
cases, debugging, and understanding of the code.

### The shape of the solution, and why

**One Python asyncio process** (FastAPI/uvicorn) on the laptop, **one browser page**
running inside Recall's cloud, **one tunnel** connecting them.

The two external systems force most of the shape:

1. **Recall.ai has no streaming-audio output API.** The *only* way for a bot to
   speak continuously is "Output Media": the bot runs a webpage we control in its
   headless Chrome, and whatever that page plays becomes the bot's voice in the
   meeting. So a webpage of ours must exist. Symmetrically, Recall injects the
   meeting's mixed audio into that page as its *microphone* — which turns out to be
   the best audio input too (20ms granularity vs the 200ms chunks of Recall's
   server-side websocket feed).

2. **Gemini Live is server-to-server only** (a raw API key must never reach a
   publicly tunneled page), and everything gradeable — latency instrumentation,
   dashboard, event log, turn-taking logic — wants to live where we can see it.

Those two facts dictate the split: the **page is a dumb microphone + speaker +
status face** (three small JS files, no build step), and **all intelligence is
server-side**. The page and Recall's event stream reach the server through an ngrok
tunnel (Recall's cloud can't connect to `localhost`; this is local-dev plumbing,
not deployment).

Inside the server, five concerns are kept in separate modules:

- **Session** (`gemini/session.py`): the Gemini Live connection — audio in/out,
  tool calls, transcription, reconnects, long-session survival.
- **Audio engine** (`audio/bridge.py`): buffering, pacing, flushing, and a local
  voice detector. Built around one invariant: *the Gemini receive loop never
  blocks*, because the same loop carries the barge-in signal.
- **Turn-taking** (`core/turn_gate.py`): the "never interrupt a human" guarantee,
  implemented as a floor gate consulted before every 40ms of agent audio.
- **Discovery state** (`core/discovery.py` + `gemini/tools.py`): the 8 questions
  and their answers — mutated *only* by Gemini tool calls and dashboard controls,
  authoritative on the server (the model's memory is not trusted).
- **Observability** (`core/events.py`, `core/latency.py`, the dashboard): every
  event flows through one bus into a JSONL log, a live dashboard, and a `replay`
  command. Per-turn latency is decomposed into a waterfall with real timestamps at
  every hop, including on the page (clock-synced).

Everything else — greeting etiquette, stale-answer drops, drop-recovery notes,
periodic state re-anchoring, task supervision — exists because live testing
falsified a simpler design. §4 and §5 carry those stories; the code carries only
the resulting rules.

### The end-to-end audio path (memorize this)

```
you speak in Meet
  → Recall injects meeting audio as the page's "microphone"
  → capture-worklet.js slices it into 20ms Int16 frames @16kHz
  → binary websocket frame [client_ts][pcm] → tunnel → /ws/agent
  → Runtime.handle_page_ws → AudioBridge.feed_frame (EnergyVAD listens here)
  → inbound queue → AudioBridge.inbound_sender → GeminiSession.send_audio
  → Gemini's server VAD ends your turn (~700ms silence) → model generates
  → GeminiSession receive loop → AudioBridge.on_model_audio
  → TurnAudioBuffer (epoch-stamped) → Pacer (asks FloorGate: may I?)
  → binary frame [epoch][seq][pcm] → tunnel → page
  → player-worklet.js renders sample-accurately → tab audio → Meet
you hear Glad
```

---

## §1 Prerequisite concepts

Ordered so each builds on the previous. For each: what it is, how *our* code uses
it, and the gotcha that matters *here*.

### 1. Raw PCM audio

**What**: Sound as a plain array of numbers — amplitude samples taken at a fixed
rate. "16kHz mono s16le PCM" = 16,000 samples/second, one channel, each sample a
signed 16-bit little-endian integer. No compression, no framing: 1 second of 16kHz
audio is exactly 32,000 bytes; byte-math *is* time-math.

**Ours**: Everything is PCM. Input to Gemini: 16kHz (page capture). Output from
Gemini: always 24kHz (fixed by the API). `audio/bridge.py` defines
`OUT_BYTES_PER_MS = 48` (24,000 samples × 2 bytes ÷ 1000 ms) and uses it to convert
buffer sizes to milliseconds everywhere — buffer depth, dropped-audio amounts,
chunk slicing are all byte arithmetic.

**Gotcha**: two different sample rates coexist (16k in, 24k out). We never resample
in Python: the page runs two separate `AudioContext`s (Chrome resamples at the
edges), and Gemini accepts any input rate declared via mime type
(`audio/pcm;rate=16000`). If you ever see math with `48`, it's the 24k output side;
`32` would be the 16k input side.

### 2. WebSockets

**What**: A persistent, bidirectional TCP connection bootstrapped from an HTTP
"Upgrade" handshake. Unlike request/response HTTP, either side can push **frames**
at any time; frames are either text (we use JSON) or binary.

**Ours**: Four distinct websockets, all defined in `server.py` / handled in
`runtime.py`: `/ws/agent` (the page: binary audio both directions + JSON control),
`/ws/recall` (Recall's cloud pushes participant events to us), `/ws/dashboard`
(event stream to the browser dashboard + control messages back), and a fifth we
*consume*: the google-genai SDK's connection to Gemini Live. Binary is used for
audio (33% smaller than base64-in-JSON — matters through a tunnel), JSON for
everything else.

**Gotcha**: idle proxies kill quiet websockets. uvicorn is configured with
`ws_ping_interval=20` (in `main.py cmd_run`), and the page re-pings every 4s (which
doubles as clock sync). Also: Recall requires a **trailing slash before query
params** on its endpoint URL (`/ws/recall/?token=…`) — `server.py` registers both
path variants for this reason alone.

### 3. Python asyncio (single-threaded concurrency)

**What**: One thread, one event loop, many cooperative **tasks**. A task runs until
it `await`s; blocking synchronous code anywhere freezes *everything*. Primitives we
use: `asyncio.Queue` (thread of work between tasks), `asyncio.Event` (wakeup
signal), `asyncio.Lock` (serialize writes), `create_task` (fire-and-forget),
`wait_for` (timeout).

**Ours**: The entire server is one event loop. `Runtime.start` launches seven
long-lived tasks (Gemini session, pacer, inbound sender, stats, hard-stop watchdog,
silence nudger, periodic re-anchor — plus bot-status polling when a bot exists).
Handoffs between them are queues and events, never callbacks-into-blocking-code.

**Gotcha #1**: a fire-and-forget task that raises dies *silently* — asyncio only
surfaces the exception if someone awaits the task. That's why every long-lived task
goes through `Runtime._supervise`, which logs `runtime.task_error` and restarts.
**Gotcha #2**: `time.monotonic()` for all durations (wall clock can jump); wall
time appears only in event logs for humans.

### 4. Recall.ai (meeting-bot-as-a-service)

**What**: An API that puts a controllable participant ("bot") into a Google
Meet/Zoom/Teams call. You `POST /api/v1/bot/` with a meeting URL; their cloud spins
up a headless Chrome that joins the call. Everything about the bot is config on
that one create call. Accounts are region-scoped (ours: `us-west-2`).

**Ours** (`recall/client.py create_bot`): we configure (a) `output_media.camera` =
our `/agent` page URL — the bot's camera shows the page and the page's audio is the
bot's voice; (b) `realtime_endpoints` = our `/ws/recall/` URL with
`participant_events.*` subscribed (join/leave/speech_on/speech_off) — Recall's
cloud connects *to us* and pushes those as JSON; (c) `variant: web_4_core` — the
default 250-millicore bot audibly stutters running Output Media; (d)
`automatic_leave` timeouts. Bot lifecycle is *polled* (`get_bot` every 4s) through
a status ladder: `joining_call → in_waiting_room → in_call_recording → call_ended →
done` (or `fatal`).

**Gotcha**: Recall's `speech_on/speech_off` events are **unreliable in small
meetings** — measured live: one `speech_on`, zero `speech_off` in 4 minutes, plus a
phantom `speech_on` fired while the bot was still in the waiting room. The entire
gate architecture (§1.10, §4) exists around this fact. Second gotcha: `localhost`
is blocked *inside* the bot's page — every URL the page touches must go through
the tunnel.

### 5. Tunneling (ngrok)

**What**: A public HTTPS/WSS hostname that forwards to a local port. An outbound
connection from your laptop to ngrok's edge carries all traffic, so no firewall or
NAT config is needed.

**Ours**: Recall's cloud must reach us twice (load the page; connect the events
websocket), so `scripts/run_demo.sh` starts `ngrok http --domain <static> 8000`,
resolves the public URL, and exports it as `PUBLIC_BASE_URL` before launching the
server. The code is tunnel-agnostic — any public base URL works; the script also
has a cloudflared fallback.

**Gotcha**: we use a **static** (reserved) domain: Recall retries a failed
realtime endpoint only 30×/3s and then marks it dead *permanently* — with an
ephemeral URL, an ngrok restart mid-meeting orphans the bot with no recovery. The
dashboard stays on localhost, but is still token-gated because the tunnel exposes
the whole server.

### 6. Gemini Live API (audio-native, bidirectional)

**What**: A websocket API where you stream raw audio *to* the model and it streams
raw audio *back* — one model does listening, reasoning, and speaking (no separate
STT/TTS). The server runs its own **VAD**: it detects when the user starts/stops
speaking and decides turn boundaries itself. Supports function calling, Google
Search grounding, and text "nudges" injected mid-conversation.

**Ours** (`gemini/session.py`): model pinned to `gemini-3.1-flash-live-preview`.
`_build_config` sets: audio-only responses; a system instruction built from the
live question list; `google_search` + our five function declarations combined;
input+output transcription (that's where the dashboard transcript comes from); VAD
tuned to `silence_duration_ms=700` (never split a human's sentence) with LOW
start/end sensitivity (resist echo); context-window compression at 100k→64k tokens;
session resumption; `thinking_level="minimal"`.

**Gotcha — five, all load-bearing**:
1. `session.receive()` is a **per-turn generator**: it *completes* at every turn
   boundary. Treat that as a disconnect and you close a healthy session
   (`_receive_loop` re-enters it in a `while True`).
2. On barge-in the server sends `interrupted: true` and stops generating — but
   audio already in *our* buffers keeps playing unless we flush (server + page).
3. A **text nudge is a user turn**: the model *responds* to it. Sending one
   mid-generation cancels the pending answer. All nudges therefore wait for a
   quiet floor (`Runtime._wait_for_quiet`).
4. One server event can carry multiple parts (audio + transcript + flags):
   `handle_message` uses independent `if`s, never `elif`.
5. Connections recycle ~every 10 minutes (a `GoAway` warns you) and audio-only
   sessions cap at 15 minutes *without* compression — long meetings need
   resumption handles *and* compression *and* re-seeded state, all three.

### 7. Gemini Live tool calling

**What**: You declare functions (name + JSON-schema parameters) in the session
config; when the model decides to call one, a `tool_call` message arrives with
name/args/id; you execute and send back a `FunctionResponse`. In Live this is
synchronous: **the model won't speak again until you respond.**

**Ours** (`gemini/tools.py`): five declarations — `record_answer`,
`mark_question_asked`, `mark_question_skipped`, `set_meeting_phase`,
`leave_meeting` — each with an explicit "Invocation condition:" sentence in its
description (the single highest-leverage trick for tool-call reliability). The
dispatcher contract: handlers only mutate local state or schedule background work,
so the response returns in microseconds. `question_id` is an **enum** of the real
question ids — the model physically can't invent one.

**Gotcha**: on interruption Gemini may cancel in-flight calls
(`tool_call_cancellation`); we must not answer a cancelled call (checked in
`_finish_deferred`). And responses must go to the connection that delivered the
call, not whatever `self._session` points at after a reconnect race.

### 8. Web Audio API + AudioWorklets (the page)

**What**: The browser's audio pipeline. An `AudioContext` has a fixed sample rate
(Chrome resamples anything entering/leaving it). An **AudioWorklet** is your JS
running on the dedicated *audio thread*, called every 128 samples ("render
quantum") — immune to main-thread jank, which is what makes playback glitch-free.
`getUserMedia` grabs a microphone stream; in the Recall bot, "the microphone" *is
the meeting audio*.

**Ours**: two contexts on the page: capture at 16kHz (`capture-worklet.js` batches
128-sample quanta into 320-sample/20ms Int16 frames) and playback at 24kHz
(`player-worklet.js` holds a queue of chunks and renders them back-to-back,
outputting zeros when empty). `agent.js` wires both to the websocket.

**Gotcha**: `getUserMedia({audio: true})` defaults to echoCancellation +
noiseSuppression + autoGainControl — all three would mangle an already-clean
conference mix, so we explicitly disable them. And an `AudioContext` can start
`suspended` (autoplay policy): the page calls `resume()` on a 1s interval and on
every incoming message.

### 9. VAD — Voice Activity Detection (and our EnergyVAD)

**What**: Any algorithm answering "is someone speaking right now?". Ranges from
trivial loudness thresholds to neural models. Two VADs exist in this system:
Gemini's (server-side, decides *conversation turns*) and ours (local, decides
*floor control*). They are independent on purpose.

**Ours** (`audio/bridge.py EnergyVAD`): deliberately the simplest thing that
works — RMS (root-mean-square loudness) of each 20ms frame against a threshold
(500 ≈ 1.5% of full scale), with a 300ms "hangover" so short pauses don't count as
speech-end. It runs on every inbound frame and provides: `in_voice` (gate),
`voice_since` (hard-stop timing), `last_voice_mono` (latency anchor via the
`on_speech_end` callback), `last_feed_mono` (staleness), `last_rms` (dashboard).

**Gotcha**: the VAD only knows anything while frames arrive. A stalled feed
freezes it — frozen-quiet would open the floor over talking humans, frozen-voice
would mute the bot forever. Hence `is_stale()`: no frames for 1.5s → the VAD
surrenders authority (the gate falls back to Recall events).

### 10. The floor / turn-taking pattern

**What**: "Holding the floor" = being the one speaking. Polite agents acquire the
floor before speaking and yield it instantly when interrupted. Not a library —
a state machine we built.

**Ours** (`core/turn_gate.py`): `FloorGate.wait_for_floor()` is consulted by the
pacer before *every 40ms chunk*. OPEN requires: nobody speaking (live VAD
authoritative; Recall events if VAD stale/absent) **and** ≥300ms of silence
(grace). Waiting >2.5s returns DROP (stale answer — delete, don't play late). A
separate hard-stop watchdog (`Runtime._hard_stop_due`) covers the other direction:
human talks *over* playing agent audio for 400ms with no `interrupted` → force
drop. Both gate and watchdog treat a **live VAD's word as final**; speech events
back-stop only when the audio feed is stale. (A union was tried and falsified
live: events fire through people's between-sentence pauses — 18 of 23 kills in
one multi-party run had a provably quiet mic — shredding agent speech into blips.)

**Gotcha**: dropping audio the model already "said" desynchronizes its beliefs
from reality — it thinks it asked a question nobody heard. Every force-drop
therefore schedules a "participants could NOT hear your last response" note once
the floor frees (`_notify_dropped_response`), and the greeting isn't even
*requested* until the floor is quiet (`_greet_when_quiet`).

### 11. Epochs (generation counters)

**What**: A monotonically increasing integer identifying "which response does this
audio belong to". The cheap, race-free alternative to tracking individual chunks.

**Ours**: `TurnAudioBuffer.epoch` bumps on every new model turn (`new_turn`) and
every flush (`flush`). Every chunk sent to the page carries its epoch in the
binary header; a flush message carries the new epoch. Discard rule, enforced in
*three* places: pacer (re-checks epoch after every await), player-worklet (ignores
chunks below current epoch), and the buffer itself (cleared on flush).

**Gotcha**: after a *stale drop*, the server is still streaming the rest of that
generation — flushing the buffer isn't enough; `AudioBridge._discarding` eats the
tail until the turn boundary, and it's **sticky** (a watchdog re-fire must not
clear it, or the tail replays as a "new" turn).

### 12. Backpressure, jitter buffers, and pacing

**What**: Producers and consumers at different speeds need buffering policy.
A **jitter buffer** keeps a small reserve ahead of playback so network hiccups
don't starve the speaker. **Pacing** means releasing data at realtime rate rather
than as fast as it arrives. Bounded queues need an overflow policy (we drop-oldest:
newest audio is worth most).

**Ours**: Gemini emits a whole response in a burst (seconds of audio in ~100ms).
`TurnAudioBuffer` absorbs it unbounded server-side; `Pacer.run` releases 40ms
chunks at realtime rate, priming the page with at most 160ms head start
(`page_buffer_target_ms`) — enough to absorb jitter, small enough that a barge-in
cutoff is near-instant. Inbound, `AudioBridge._inbound` is a bounded (64-frame)
queue with drop-oldest, so a Gemini reconnect can't back audio up into the
websocket handler.

**Gotcha**: buffer size trades smoothness against interruption latency *directly*.
160ms page-side is the deliberate compromise; the "unbounded" server buffer is safe
only because `interrupted`/DROP flush it in O(1) via the epoch bump.

### 13. Event sourcing (EventBus → JSONL → dashboard → replay)

**What**: Instead of mutating UI state directly, emit immutable events; every
consumer (log file, live UI, replay tool) derives state by folding over the
stream. One write path, N read paths, perfect auditability.

**Ours** (`core/events.py`): `EventBus.emit(type, **data)` → appends to a 20k ring
(late-joining dashboards get backlog), writes a JSONL line
(`runs/<ts>/events.jsonl`), and feeds subscriber queues (dashboard websockets;
overflow drops oldest so the live view stays live). `dashboard.js reduce()` is a
pure event-reducer — which is exactly why `gladiator replay` works: the same page
renders a recorded file identically.

**Gotcha**: `emit` is called from the Gemini receive loop, so it must never block —
file write is buffered-synchronous, queue puts are `put_nowait`. High-frequency
data (per-frame audio) is never evented per-item; it's aggregated
(`bridge.stats` every 5s).

### 14. NTP-style clock sync (cross-machine latency)

**What**: To timestamp an event on machine B against machine A's clock, you need
their offset. Classic trick: A sends t0; B replies with its time t1; A receives at
t3; assuming symmetric network, offset ≈ (t0+t3)/2 − t1, and RTT = t3 − t0.

**Ours**: the page pings every 4s (`agent.js`); `Runtime._handle_page_text` pongs
with server wall-ms; the page computes offset/RTT and reports them; `ClockSync`
(`core/latency.py`) keeps a rolling median. When the page reports "first sample of
epoch N actually rendered" (`play_ack`), `mark_page_play` converts that page-clock
timestamp onto the server's monotonic timeline — that's how the waterfall's
`transit_play` segment is a *real measurement*, not a guess.

**Gotcha**: medians, not means — a single congested ping would otherwise skew the
offset. And conversion goes page-wall → server-wall → server-monotonic via the
current wall/mono anchor, so a wall-clock jump mid-run only affects in-flight
conversions.

### 15. FastAPI / uvicorn / pydantic-settings (the boring parts)

**What**: FastAPI = decorator-routed ASGI framework (HTTP + websockets); uvicorn =
the asyncio server running it; pydantic-settings = typed config loaded from
env/.env.

**Ours**: `server.py` is deliberately *just* routes + token checks, delegating to
`Runtime`. `config.py Settings` holds every knob with defaults; env vars override
(`AUDIO_SOURCE=recall_ws`, `VAD_GATE=false`, …). uvicorn is started
programmatically (`main.py cmd_run`) so the CLI controls startup ordering: server
up → *then* create the bot.

**Gotcha**: this file (and `main.py`) use `from __future__ import annotations`,
which turns annotations into strings resolved against *module globals*. FastAPI
introspects the `ws: WebSocket` annotation to inject the connection — with a
function-local import of `WebSocket`, resolution fails and FastAPI silently
demotes the parameter to a required query param; every handshake 403s. Hence the
module-level `from fastapi import WebSocket` in `main.py` with its warning comment.

### 16. Prompting a Live agent (system instruction + nudges)

**What**: A Live session takes one immutable **system instruction** at connect
time (persona, rules, tool conditions). Mid-session steering happens via realtime
**text nudges**, which the model hears as bracketed system notes from the user
side.

**Ours** (`gemini/prompts.py`): the SI is built per-connection from the live
question list; structure: persona → voice style → questions → tool rules (each
with an invocation condition) → conversation flow → guardrails (greet at most
once; "stop" means stop talking, never leave; bracketed notes are never read
aloud). Five nudges exist: greeting trigger, silence re-engagement, reconnect
state re-seed, drop-recovery ("they didn't hear you"), and the periodic
context refresher.

**Gotcha**: since the SI can't change mid-session, *all* dynamic state flows as
nudges — and every nudge makes the model act, so timing (quiet floor) and wording
("do not acknowledge this note") are part of correctness, not politeness.

---

## §2 Execution traces

Every flow, hop by hop. **⏱ marks a latency-instrumentation point.** File paths
relative to `src/gladiator/`.

### T1 — Startup: `scripts/run_demo.sh <meet-url>` to a bot in the room

1. `scripts/run_demo.sh` — sources `.env`, downloads ngrok if missing, starts
   `ngrok http --domain $NGROK_DOMAIN 8000`, polls `localhost:4040/api/tunnels`
   for the public URL, exports `PUBLIC_BASE_URL`, runs `gladiator run`.
2. `main.py main()` → `cmd_run(args)` — validates keys/URL, builds
   `Runtime(settings)` and `server.build_app(rt)`, starts uvicorn
   programmatically (`ws_ping_interval=20`), waits for `server.started`,
   prints dashboard/agent URLs (with the per-run `session_token`).
3. `runtime.py Runtime.__init__` — constructs the whole object graph (bus →
   latency → tracker → speech → bridge → gate → dispatcher → session → recall →
   router → pacer) and cross-wires callbacks (bridge→session audio,
   bridge→page flush, VAD speech-end→latency+face, forced-drop→recovery note).
4. `Runtime.start(meeting_url)` — `_supervise`s the 7 background tasks;
   `recall/client.py RecallClient.create_bot` POSTs the bot config
   (output_media page URL, realtime endpoint URL, `web_4_core`, auto-leave);
   writes `runs/<ts>/bot.json` + `runs/last_bot.json`; starts `_poll_bot_status`.
5. `GeminiSession.run` (already started) has connected: `_build_config` →
   `client.aio.live.connect` → emits `gemini.connected` → enters `_receive_loop`.
6. Recall's cloud boots the bot → loads `GET /agent?token=…` (`server.py agent`)
   → `web/agent/agent.js` runs: playback context + worklet, websocket to
   `/ws/agent`, `getUserMedia` (AEC/NS/AGC off), capture context + worklet;
   sends `hello`, starts 4s ping loop, sends `capture_state ok`.
7. `Runtime.handle_page_ws` accepts; `_handle_page_text` records
   `page.capture_state` → calls `_maybe_greet` (not admitted yet → no-op).
8. Recall's cloud also connects `wss://…/ws/recall/?token=…` →
   `Runtime.handle_recall_ws` → `recall/events.py RecallEventRouter.handle`
   routes `participant_events.join` (roster) etc.
9. `_poll_bot_status` sees `in_call_recording` → sets `_admitted` →
   `_maybe_greet` → spawns `_greet_when_quiet` → T6.

### T2 — The golden path: you speak, Glad answers

1. **Page**: Recall feeds meeting audio into the page mic →
   `capture-worklet.js CaptureProcessor.process` accumulates 128-sample quanta
   into a 320-sample (20ms) Float32 buffer, converts to Int16, posts to main
   thread.
2. `agent.js cap.port.onmessage` — prepends 8-byte client wall-ms, sends binary
   over `/ws/agent`.
3. `runtime.py Runtime.handle_page_ws` — `message["bytes"][8:]` →
   `AudioBridge.feed_frame`.
4. `audio/bridge.py AudioBridge.feed_frame` — `EnergyVAD.feed` (updates
   `in_voice`/`last_feed_mono`); enqueues the frame (drop-oldest at 64).
   **⏱ When you stop talking**: 300ms after your last loud frame, the VAD's
   hangover expires → `on_speech_end(last_voice_mono)` →
   `Runtime._on_speech_end` → `LatencyTracker.mark_speech_end` (anchor = the
   actual last-voiced instant, not "now") + face→thinking.
5. `AudioBridge.inbound_sender` (task) — dequeues → `GeminiSession.send_audio` →
   `send_realtime_input(audio=Blob(...rate=16000))` under `_send_lock`.
6. **Gemini**: its VAD sees ~700ms of silence → closes your turn → generates.
7. `GeminiSession._receive_loop` → `handle_message`: audio parts →
   `AudioBridge.on_model_audio`.
8. `AudioBridge.on_model_audio` — first part of a fresh generation:
   `buffer.new_turn()` (epoch++), **⏱ `LatencyTracker.mark_first_audio(epoch)`**
   (consumes the speech anchor), emits `turn.start`, face→speaking; appends PCM
   (buffer slices into 1920-byte/40ms chunks). Transcription parts in the same
   loop emit `transcript.user`/`transcript.agent`.
9. `Pacer.run` (task) — `buffer.get()` → chunk; `FloorGate.wait_for_floor()` —
   you're quiet, grace elapsed → OPEN; first chunk of the epoch:
   **⏱ `LatencyTracker.mark_first_send(epoch, gate_hold_ms)`**; sends via
   `Runtime._send_audio_to_page` (binary `[epoch][seq][pcm]`); paces: sleeps
   whenever sent-ahead exceeds 160ms.
10. **Page**: `agent.js ws.onmessage` (binary) → posts chunk to
    `player-worklet.js`; `PlayerProcessor.process` renders it sample-accurately;
    on the first rendered sample of the new epoch posts `started` →
    `agent.js` sends `play_ack {epoch, client_play_ts_ms}`.
11. `Runtime._handle_page_text("play_ack")` →
    **⏱ `LatencyTracker.mark_page_play`** — converts the page timestamp via
    `ClockSync` (fed by the 4s ping/pong/clock loop), `_finalize`s the Turn →
    emits `latency.turn {detect_think, serve, transit_play, e2e, gate_hold_ms}`.
12. `dashboard.js reduce("latency.turn")` → waterfall bar + p50/p95 refresh.
13. Gemini sends `generation_complete`/`turn_complete` →
    `bridge.on_generation_complete` (flush partial chunk) / `on_turn_complete`
    (close turn, clear discard, emit `turn.complete`) → face→listening.

### T3 — Tool call: your answer lands on the dashboard

1. Mid-generation, Gemini emits `tool_call` →
   `GeminiSession.handle_message` → `ToolDispatcher.handle(fc)`.
2. `gemini/tools.py ToolDispatcher._dispatch("record_answer", args)` →
   `core/discovery.py DiscoveryTracker.record_answer` — validates the enum id,
   appends a revision, sets status answered/partial, emits `discovery.answer`,
   returns `{"status":"ok", "remaining":[…]}` (the model sees fresh state every
   call).
3. Back in `handle_message`: the result becomes a `FunctionResponse` →
   `_send_tool_responses(responses, session)` — **to the session that delivered
   the call**, under `_send_lock`. Total handler time: microseconds (the model
   is blocked until this returns).
4. `dashboard.js reduce("discovery.answer")` → `renderAnswer` flips the row
   green with summary/quote/confidence/revision count.

Deferred variant (`search_mode=function` only): `_dispatch` returns `None` →
`handle_message` spawns `_finish_deferred` → `run_deferred` awaits
`Runtime._grounded_search` → checks `is_cancelled` and session identity → sends
the response late. The receive loop never waited.

### T4 — Barge-in: you interrupt Glad mid-sentence

1. You speak while Glad plays. Your frames keep flowing (T2 steps 1–5) — input
   never stops.
2. Gemini's VAD detects speech over its own output → cancels generation → sends
   `interrupted: true`.
3. `handle_message`: emits `gemini.interrupted` → `AudioBridge.on_interrupted`:
   sets `last_interrupt_mono`, **⏱ `mark_interrupted(epoch)`**, `buffer.flush()`
   (epoch++, O(1)), emits `turn.interrupted {dropped_ms}`, schedules
   `notify_page_flush` → `Runtime._send_page_flush` → JSON `{"type":"flush"}`.
4. **Page**: `player-worklet` clears its queue instantly (≤160ms was buffered —
   that's the max overhang you could ever hear).
5. `Pacer` wakes from `buffer.get()`/gate wait, sees the epoch changed, discards
   its chunk silently. Glad is silent; Gemini is already listening to what you're
   saying.

Backstop variant (Gemini misses it — e.g. echo-masked): `_hard_stop_watchdog`
polls `_hard_stop_due()` at 10Hz — sustained overlap ≥400ms from *either* a live
VAD or fresh (pruned) Recall events, with `last_interrupt_mono < voice_since` →
emits `gate.hard_stop` → `bridge.force_drop("hard_stop")` (sticky `_discarding`
eats the still-streaming tail) → T5's recovery note follows.

### T5 — Stale drop and the recovery note

1. Glad has an answer buffered; you start talking before it plays.
   `Pacer.run` → `FloorGate.wait_for_floor()` holds (emits `gate.hold`).
2. You keep talking past 2.5s → gate returns DROP (emits `gate.stale_drop`) →
   `Pacer` calls `bridge.force_drop("gate_stale")` — buffer flushed, epoch++,
   `_discarding=True` for the generation tail, page flushed, emits
   `turn.force_drop {dropped_ms}` → `bridge.on_forced_drop` callback →
   `Runtime._on_forced_drop` spawns `_notify_dropped_response`.
3. `_notify_dropped_response` — waits (≤30s) until the floor is quiet via
   `_wait_for_quiet`, *unless* the buffer epoch moved or a turn opened (a newer
   response superseded the dropped one → exit silently). Then, rate-limited
   15s: `session.send_text(DROPPED_RESPONSE_NUDGE)`; only on successful delivery
   emits `runtime.dropped_response_note`.
4. Gemini receives "participants could NOT hear your last response… briefly
   restate" → generates a restatement → normal T2 flow from step 7.

### T6 — Greeting (admission-gated, quiet-gated, once)

1. Preconditions tracked in `Runtime`: `_admitted` (bot status
   `in_call_recording`, from `_poll_bot_status`) AND `page_capture_ok` (page
   reported working getUserMedia). Either event calls `_maybe_greet`.
2. `_maybe_greet` — no-ops unless both true and not yet greeted; spawns a single
   `_greet_when_quiet` task (guarded against duplicates).
3. `_greet_when_quiet` — `_wait_for_quiet(20s)` (people talk right when they
   admit a bot); on timeout greets anyway; emits
   `runtime.greeting_waited_for_floor` if it waited; →
   `GeminiSession.request_greeting`.
4. `request_greeting` — if the session isn't connected yet, sets
   `_greeting_pending` (delivered on connect in `run()`); else `_send_greeting`
   → `send_text(GREETING_NUDGE)` → sets `_greeted` → emits
   `gemini.greeting_sent`. The SI says: greet at most once, only on this nudge.
5. Model greets and asks question 1 → T2 steps 7–13 (typically with
   `mark_question_asked` riding along as T3).

### T7 — Gemini connection loss and recovery

1. `GeminiSession.run` wraps everything: `go_away` arrives (~60s warning,
   logged) or the socket just closes → `_receive_loop` raises
   `ConnectionClosed` → caught in `run` → emits `gemini.disconnected`.
2. Loop iterates: reconnect with `SessionResumptionConfig(handle=latest)` — the
   handle arrives continuously via `session_resumption_update` messages during
   normal operation.
3. On reconnect with `_greeted` set: `send_text(reconnect_nudge(progress_note))`
   — `DiscoveryTracker.progress_note()` (answered/skipped/open) re-seeds the
   model, with "do not re-greet".
4. Repeated failures back off 0.5→5s; after backoff ≥4s a possibly-stale
   resumption handle is dropped (`gemini.resumption_handle_dropped`) so at least
   a fresh session can start (state still re-seeded — the tracker is
   server-side and lost nothing).
5. Meanwhile: `send_audio` silently tolerates the gap (frames are ephemeral),
   `send_text` returns False (callers don't log their events), and the page/
   dashboard notice nothing except a `gemini: reconnecting` chip.

### T8 — Leaving the meeting

1. You say "Glad, wrap it up" → model summarizes, says goodbye, calls
   `leave_meeting` (T3 mechanics) → `ToolDispatcher._dispatch` →
   `Runtime._request_leave(reason)` — schedules `_leave()` and **returns
   immediately** (`{"status":"ok"}` goes back so the model can finish speaking).
2. `_leave` task — waits ≤4s for goodbye audio to *start* (same turn as the
   tool call), then ≤20s total for `pacer.agent_playing` false and
   `buffer.buffered_ms == 0` → emits `runtime.leave_playout_done {waited_ms}` →
   `RecallClient.leave` (POST `/bot/{id}/leave_call/`).
3. `_poll_bot_status` sees `call_ended`/`done` → face idle → `_periodic_reanchor`
   returns for good on its next tick.
4. Ctrl+C path: `cmd_run` finally-block → `Runtime.shutdown` — best-effort
   `recall.leave`, `session.stop()`, cancel all tasks, close the bus.

### T9 — Dashboard lifecycle and a control round-trip

1. Browser opens `/dashboard?token=…` (`server.py dashboard`) → `dashboard.js`
   connects `/ws/dashboard` → `Runtime.handle_dashboard_ws` sends `hello`
   (full `Runtime.snapshot()`: questions, latency stats, bot/page/session
   state) then `backlog` (last 2000 ring events) → `dashboard.js` initializes
   panels from the snapshot and folds the backlog through `reduce()`.
2. Live: `handle_dashboard_ws.pump_events` forwards every bus event as
   `{"type":"event"}` → `reduce()` dispatches to the right renderer (chips,
   transcript, checklist, waterfall, log).
3. Control: clicking *Mute agent* → `{"type":"control","action":"mute"}` →
   `pump_controls` → `Runtime.control("mute")` → `bridge.muted = True` (frames
   still feed the VAD — the gate stays truthful — but nothing reaches Gemini)
   → `control_result` back → button relabels.
4. Skip: per-row button sends `skip_question {question_id}` →
   `tracker.mark_skipped` + a nudge telling the model not to ask it.

### T10 — Replay

1. `gladiator replay runs/<ts>/events.jsonl --speed 8` → `main.py cmd_replay` —
   loads events, builds a minimal FastAPI app (dashboard page + `/ws/dashboard`,
   no token: local-only).
2. On connect: `hello` with `{replay: true}` + fresh question definitions
   (from a throwaway `DiscoveryTracker`), then each event with inter-event
   delays `(Δts / speed)` capped at 2s.
3. The *same* `dashboard.js` renders it — possible only because the dashboard
   is a pure event-reducer (§1.13).

### T11 — Recall realtime events

1. Recall's cloud pushes JSON on `/ws/recall/` → `Runtime.handle_recall_ws` →
   `RecallEventRouter.handle`.
2. `speech_on`/`speech_off` → `SpeechState.on_speech_on/off` (bot's own name
   filtered) → wakes any gate waiter via the `changed` event; router emits
   `recall.speech_on/off` (dashboard "speaking" chip). With `hybrid_vad` on,
   the last human's `speech_off` also fires `Runtime._on_all_humans_quiet` →
   `session.send_audio_stream_end()` (latency knob, ships OFF).
3. `join`/`leave` → roster upkeep + `Runtime._on_roster_change` → (post-greeting
   only) a nudge: "Priya just joined — acknowledge briefly"; `leave` also
   releases the floor if that participant was mid-speech
   (`SpeechState.on_participant_leave`).
4. `audio_mixed_raw.data` (only if `AUDIO_SOURCE=recall_ws`) → base64-decode →
   `bridge.feed_frame` — same downstream path as page audio (the transport swap
   is exactly one entry point).

---

## §3 File map

**Load-bearing** = a bug here breaks the live demo. **Peripheral** = tooling/docs;
breakage is inconvenient, not fatal.

### `src/gladiator/config.py` — 73 lines — load-bearing

Single responsibility: every tunable and secret, typed, with defaults; loaded from
`.env` by pydantic-settings. Depends on pydantic only. *Everything* depends on it.

- `class Settings(BaseSettings)` — fields grouped: secrets/endpoints, server,
  identity/model (model pinned with the bug reference), audio path
  (`audio_source`, chunk/buffer sizes), Gemini VAD knobs, floor-gate knobs
  (`vad_gate`, grace/stale/max-hold/hard-stop), context management (compression
  window, `reanchor_interval_s`), `search_mode`, bookkeeping (`runs_dir`,
  `session_token` generated per run via `default_factory`).
- `recall_base` / `public_ws_base` (properties) — derived URLs; `public_ws_base`
  swaps `https://`→`wss://` for the Recall endpoint registration.
- `get_settings()` — `lru_cache`d accessor used by the CLI (tests construct
  `Settings(...)` directly to avoid the cache).

### `src/gladiator/main.py` — 348 lines — peripheral (but `cmd_run` startup order matters)

Single responsibility: the CLI. Depends on runtime/server/recall/gemini lazily
(imports inside commands keep `--help` fast). Nothing imports it.

- `main()` — argparse with five subcommands; dispatches via `asyncio.run`;
  swallows KeyboardInterrupt.
- `cmd_run(args)` — validates env, builds Runtime+app, starts uvicorn
  programmatically, waits for `server.started` **before** `rt.start()` creates
  the bot (the bot would otherwise load a dead page URL); `--local-test` sets
  `rt._admitted = True` so greeting logic works without a meeting; prints URLs;
  finally-block guarantees `rt.shutdown()`.
- `cmd_doctor()` + nested `report()` — preflight: keys present, Recall region
  reachable (HTTP 200), pinned model in `models.list`, then `_probe_live_combined`;
  tunnel probe is informational (offline is normal when the demo isn't running).
  Returns exit code = failure count.
- `_probe_live_combined(settings)` — opens a real Live session with
  `google_search` + a dummy function declaration combined (the documented API
  risk), sends a text turn, counts audio bytes until `turn_complete`.
- `cmd_bench(args)` + nested `_first_audio`/`_drain` — TTFA distribution
  straight against Gemini: paces a 16kHz WAV in 20ms frames (or a text turn),
  times to first audio part, prints p50/p95. Isolates the model's share of
  latency from our pipeline's.
- `cmd_leave(args)` — reads `runs/last_bot.json` (or `--bot-id`) → REST leave.
- `cmd_replay(args)` + nested `dashboard`/`ws_dashboard` — see T10. Module-level
  `from fastapi import WebSocket` carries the future-annotations warning (§1.15
  gotcha — this was a live 403 bug).

### `src/gladiator/runtime.py` — 532 lines — load-bearing (the hub)

Single responsibility: object-graph wiring, websocket endpoints' logic, and the
"etiquette" background tasks. Depends on every other module; `server.py` and
`main.py` depend on it.

- `Runtime.__init__` — constructs bus (with `runs/<ts>/events.jsonl`), latency,
  tracker, speech, bridge, gate (VAD injected only if `_vad_authoritative` =
  `vad_gate` AND page audio source), dispatcher (leave callback + optional
  grounded search), session, recall client, router (hybrid-vad + roster hooks),
  pacer; then cross-wires bridge callbacks. Order matters: bridge before gate
  (the gate borrows `bridge.vad`).
- `check_token(token)` — `hmac.compare_digest` against the per-run token
  (constant-time; used by every route/ws).
- `_supervise(coro_fn, name)` — wraps a task: uncaught exception → emit
  `runtime.task_error` → 1s → restart; normal return ends the task;
  CancelledError propagates. Every long-lived task uses it.
- `start(meeting_url)` — spawns the 7 supervised tasks; if a URL: `create_bot`,
  persist `bot.json`/`last_bot.json`, spawn `_poll_bot_status`.
- `shutdown()` — best-effort leave, `session.stop()`, cancel+gather tasks,
  close recall client and bus.
- `handle_page_ws(ws)` — accept; loop `ws.receive()`: bytes → strip the 8-byte
  timestamp header → `bridge.feed_frame`; text → `_handle_page_text`; tracks
  `page_ws`/`page_capture_ok`, emits connect/disconnect events. Only one page
  is expected; latest wins.
- `_handle_page_text(msg)` — `ping`→pong (server wall-ms), `clock`→
  `ClockSync.update`, `play_ack`→`mark_page_play` ⏱, `capture_state`→ flag +
  `_maybe_greet`, `hello`→log.
- `_send_audio_to_page(epoch, seq, pcm)` — `struct.pack("<II")` header + PCM;
  returns False when no page (pacer drains anyway).
- `_send_page_flush(epoch)` / `_page_send_json(obj)` — JSON control to the page;
  both swallow send errors (page may be mid-reconnect; it re-syncs on hello).
- `handle_recall_ws(ws)` — accept, loop `receive_text` → `router.handle(json)`.
- `_poll_bot_status()` — 4s loop on `get_bot`; on change: emit
  `recall.bot_status`; `in_call_recording` → `_admitted` + `_maybe_greet`;
  terminal → face idle, task ends.
- `_maybe_greet()` — requires admitted AND audio-ready AND not greeted; spawns
  one `_greet_when_quiet` (duplicate-guarded via `_greet_task`).
- `_wait_for_quiet(max_wait_s, require_no_open_turn=True)` — THE quiet
  predicate: `gate.is_open()` ∧ ¬`pacer.agent_playing` ∧ (optionally)
  ¬`bridge.turn_open` ∧ buffer empty; 0.1s poll; returns bool. Single definition
  shared by greeting/drop-note/re-anchor (copies diverged once — that was a bug).
- `_greet_when_quiet(20s)` — wait (timeout ⇒ greet anyway), emit wait event if
  >200ms, → `session.request_greeting()`.
- `_on_forced_drop(reason)` / `_notify_dropped_response(30s)` — see T5; exits
  silently if epoch moved or a turn opened (superseded); 15s rate limit;
  event only after successful `send_text`.
- `control(action, **kwargs)` — dashboard verbs: leave / mute / unmute / greet /
  skip_question (tracker + model nudge) / nudge; every action evented.
- `_request_leave(reason)` — tool-handler-safe: emit, schedule `_leave` (see
  T8), return instantly. Nested `_leave` holds the playout-wait logic.
- `_hard_stop_due()` — see §1.10; prunes stuck speakers first; VAD contributes
  only when `_vad_authoritative` and fresh; suppressed if
  `bridge.last_interrupt_mono` already covers this overlap. Pure function of
  state → directly unit-tested.
- `_hard_stop_watchdog()` — 10Hz poll; on overlap: emit `gate.hard_stop` +
  `bridge.force_drop("hard_stop")`.
- `_silence_nudger(45s)` — 5s poll; skips unless admitted, in intro/discovery,
  and *both* signals quiet; "activity" = max(speech_off, VAD last voice, last
  agent audio, last nudge); nudge evented only on delivery.
- `_periodic_reanchor()` — interval from settings (0=off); ends for good on
  terminal bot status; skips pre-greeting and wrapup/done; requires a full
  `_wait_for_quiet(30)` window else skips the cycle; `context_refresher(
  progress_note)`; event only on delivery.
- `_on_speech_end(mono)` — VAD hangover callback: ⏱ `mark_speech_end` +
  face→thinking (only if not playing).
- `_on_all_humans_quiet()` — hybrid-VAD hook (ships OFF): `audio_stream_end`.
- `_on_roster_change(kind, name)` — join/leave nudges; suppressed for the bot
  itself and pre-greeting.
- `_grounded_search(query)` — fallback web_search backend: one grounded
  `generate_content` call (lazy client); returns `{"status","answer"}`.
- `set_face(state)` — dedupes, emits `face.state`, pushes to page.
- `snapshot()` — full dashboard bootstrap dict (bot/page/session state,
  discovery snapshot, latency stats, log path).
- `handle_dashboard_ws(ws)` + nested `pump_events`/`pump_controls` — hello →
  backlog (2000) → two concurrent pumps; unsubscribes in finally.

### `src/gladiator/server.py` — 73 lines — load-bearing (thin)

Single responsibility: route table + auth gate. Depends on Runtime; `main.cmd_run`
depends on it. Every page/ws/API route calls `rt.check_token` (403 / ws-reject
4401); `/healthz` and `/static` are open. `page(name)` reads the HTML fresh per
request (dev-friendly). `/ws/recall` registered with and without trailing slash
(Recall's query-param quirk). Functions: `build_app`, nested `page`, `healthz`,
`agent`, `dashboard`, `snapshot`, `_ws_authed`, `ws_agent`, `ws_recall`,
`ws_dashboard`.

### `src/gladiator/core/events.py` — 65 lines — load-bearing

Single responsibility: the EventBus (§1.13). Depends on stdlib only; everything
emits into it.

- `EventBus.__init__(log_path)` — 20k ring, subscriber list, opens the JSONL
  file in append mode.
- `emit(type_, **data)` — timestamp (wall + mono) → ring → JSONL line
  (immediate flush: a crash loses nothing) → `put_nowait` to each subscriber,
  dropping that subscriber's oldest on overflow. Synchronous, never blocks —
  it's called from the receive loop.
- `subscribe()/unsubscribe(q)` — per-dashboard queues (2000 cap).
- `close()` — closes the file (idempotent).

### `src/gladiator/core/latency.py` — 150 lines — load-bearing (rubric line)

Single responsibility: per-turn waterfall assembly + clock sync (§1.14). Depends
on EventBus; bridge/pacer/runtime feed it; dashboard consumes its events.

- `ClockSync` — `update(offset, rtt)` into rolling deques (9);
  `offset_ms`/`rtt_ms` = medians; `page_ms_to_server_mono(page_ts)` = page-wall
  → server-wall → server-mono conversion (None until first sample).
- `@dataclass Turn` — one response's marks: speech_end / first_audio /
  first_send / page_play (+ gate_hold, interrupted).
- `LatencyTracker.__init__(bus, clock, window)` — injectable clock (tests);
  per-segment 50-deep histories.
- `mark_speech_end(mono)` — sets the anchor (VAD passes the *true* last-voiced
  instant, not now).
- `clear_anchor()` — called (via bridge) whenever a text nudge is sent: the next
  turn is nudge-caused.
- `mark_first_audio(epoch)` — stamps first-audio; **consumes** the anchor
  (one utterance anchors at most one turn — a second model turn without new
  speech gets `None`, and so does any nudge-caused turn).
- `mark_first_send(epoch, gate_hold_ms)` — first chunk released by the pacer.
- `mark_page_play(epoch, page_ts_ms)` — converts via ClockSync, then
  `_finalize`.
- `mark_interrupted(epoch)` — flags the turn (shown in the waterfall).
- `_finalize(turn)` — computes detect_think / serve / transit_play / e2e (None
  where an endpoint is missing — None never enters the percentile history),
  emits `latency.turn`, retires the Turn.
- `stats()` — p50/p95/last per segment (naive index percentiles on ≤50 values).

### `src/gladiator/core/discovery.py` — 146 lines — load-bearing

Single responsibility: the authoritative question/answer state machine. Depends
on EventBus; tools.py and runtime mutate it; prompts/dashboard read it.

- `@dataclass Question` — id, label, text, status
  (`pending|asked|partial|answered|skipped`), answer fields, `updated_at`,
  full revision `history`.
- `QUESTIONS` — the 8 preset questions (module constant; templates only —).
- `DiscoveryTracker.__init__(bus)` — deep-copies fresh `Question`s (no shared
  state across instances/tests); `phase="intro"`.
- `record_answer(question_id, answer_summary, verbatim_quote, confidence,
  complete)` — unknown id → error **including `valid_ids`** (the model can
  self-correct); appends a revision; `answered` vs `partial` from `complete`;
  emits `discovery.answer` with revision number; returns ok + `remaining`.
- `mark_asked(id)` — pending→asked only (never regresses an answered row).
- `mark_skipped(id, reason)` — refuses to skip an answered/partial question
  (protects recorded data from a confused model).
- `set_phase(phase)` — validated against `PHASES`; emits `discovery.phase`.
- `remaining_ids()` — pending/asked/partial ids (drives `progress_note` and
  tool results).
- `snapshot()` — dashboard-shaped dict.
- `progress_note()` — one-line authoritative state ("Answered: …; Still open:
  …") used by reconnect and re-anchor nudges.

### `src/gladiator/core/turn_gate.py` — 176 lines — load-bearing (the guarantee)

Single responsibility: floor control (§1.10). Depends on EventBus (+ the
VoiceSignal protocol); pacer and runtime consult it.

- `VoiceSignal(Protocol)` — duck type for EnergyVAD (`in_voice`,
  `last_quiet_mono()`, `is_stale()`); a Protocol to avoid a core→audio import
  cycle.
- `@dataclass Speaker` — id/name/since_mono.
- `SpeechState.__init__(bot_name, clock)` — injectable clock; `changed`
  asyncio.Event wakes gate waiters.
- `_is_self(name)` — the bot is a participant too; its own name never gates.
- `on_speech_on/off(pid, name)` — maintain `speaking` dict +
  `last_speech_off_mono`; wake.
- `on_participant_leave(pid)` — a leaver mid-speech never sends speech_off;
  release the floor.
- `prune_stuck(max_hold_s)` — evicts speakers "on" longer than max_hold (lost
  speech_off / phantom events); returns pruned ids.
- `any_human_speaking()` / `snapshot()` / `_wake()` — trivial accessors.
- `FloorGate.__init__(speech, bus, grace_ms, stale_drop_ms, max_hold_ms, vad,
  clock)` — all thresholds injected; `last_hold_ms` exposed for latency.
- `VAD_STALE_AFTER_S = 1.5` — the staleness bound (§1.9 gotcha).
- `_live_vad()` — the vad, or None if absent/stale. This one method implements
  the authority-fallback policy.
- `_vad_speaking()` — used for the `vad_voice` field on gate events.
- `_silence_duration()` — since last quiet: VAD's `last_quiet_mono` (voice-end +
  hangover) when live, else `last_speech_off_mono`.
- `is_open()` — ¬speaking ∧ silence ≥ grace, per the live-VAD-else-events rule.
- `wait_for_floor()` — the pacer's blocking call: prune stuck speakers each
  iteration; OPEN (records hold time, emits `gate.release` if it waited); DROP
  past stale threshold (emits `gate.stale_drop`); first wait emits `gate.hold`;
  sleeps on `speech.changed` with ≤50ms timeout (event-wake *and* deadline
  polling — VAD changes don't set the event, so the poll floor matters).

### `src/gladiator/audio/bridge.py` — 333 lines — load-bearing (the engine)

Single responsibility: both audio directions + flush/discard bookkeeping (§1.11,
§1.12). Depends on latency, events, turn_gate; session and runtime drive it.

- `OUT_SAMPLE_RATE/OUT_BYTES_PER_MS` — the 24kHz byte-time constants.
- `TurnAudioBuffer.__init__(chunk_ms)` — deque of exact-size chunks + a
  `_pending` bytearray for re-slicing arbitrary Gemini part sizes; `_data`
  event wakes the pacer.
- `append(data)` — sync, called from the receive loop; slices full 40ms chunks
  out of `_pending`.
- `finish_turn()` — flushes the trailing partial chunk at generation end (so
  the last syllable isn't stuck waiting for 40ms of alignment).
- `flush()` — clear everything, **epoch++**, wake pacer, return dropped ms.
- `new_turn()` — clears pending, epoch++ (each generation owns an epoch).
- `buffered_ms` (property) / `get()` — awaits data, returns `(epoch, chunk)`;
  may return `(epoch, None)` on a flush wake — pacer treats it as a skip.
- `EnergyVAD.__init__(threshold=500, hangover_ms=300, on_speech_end, clock)` —
  `on_speech_end` is a public reassignable hook (runtime layers face-state onto
  the latency callback).
- `is_stale(max_age_s)` / `last_quiet_mono()` — §1.9; note the `-1e9` sentinel
  for "never voiced" (reads as quiet-forever, which is why staleness must gate
  it).
- `_rms(pcm)` — pure-python RMS (audioop is removed in 3.13); ~320 samples is
  trivially cheap.
- `feed(pcm)` — updates `last_feed_mono` (staleness), threshold check → voice
  start (`voice_since`) or hangover-expired end (fires `on_speech_end` with the
  *last-voiced* timestamp ⏱).
- `AudioBridge.__init__(bus, latency, chunk_ms)` — owns buffer + vad + the
  64-frame inbound queue; `last_interrupt_mono` read by the watchdog; four
  runtime-wired callbacks documented at the declaration site.
- `turn_open` (property) — is a generation currently streaming.
- `feed_frame(pcm)` — count, VAD, mute-check, enqueue with drop-oldest (never
  blocks a ws handler).
- `inbound_sender()` — task: dequeue → `send_audio_to_gemini`; catches
  everything (a mid-reconnect send error is logged and rate-limited by a 0.1s
  sleep).
- `stats_reporter(5s)` — task: aggregate telemetry event (frames, drops,
  discarded, buffer depth, rms, mute) — the reason per-frame events don't exist.
- `on_model_audio(data)` — discard-mode check → first part opens the turn
  (new_turn, ⏱ mark_first_audio, `turn.start`, face) → append.
- `on_generation_complete()` / `on_turn_complete(reason)` — finish partial;
  turn_complete additionally closes the turn, clears `_discarding`, emits
  `turn.complete`.
- `on_interrupted()` — ⏱ mark; flush; `_discarding=False` (the server already
  cancelled — next audio is a *new* turn); `turn.interrupted`; page flush;
  face. Sets `last_interrupt_mono`.
- `force_drop(reason)` — our-side kill (stale/hard): like interrupted, but
  `_discarding` goes **sticky** (`or turn_open`) because the server is still
  streaming the corpse; fires `on_forced_drop` (recovery note); also stamps
  `last_interrupt_mono` so the watchdog can't re-fire on the same overlap.
- `_schedule_page_flush()` — fire-and-forget task (called from sync context).
- `clear_speech_anchor()` — pass-through to latency (called by
  `session.send_text`).
- `Pacer.__init__(bridge, gate, bus, latency, chunk_ms, prime_ms, send, clock,
  sleep)` — clock and sleep injectable (pacing math is unit-tested).
- `agent_playing` (property) — "shipped audio recently enough that the page is
  likely still playing" = within prime+3·chunk (≈280ms) of the last send.
- `run()` — task: get chunk → **re-check epoch** → `wait_for_floor` →
  **re-check epoch again** (interrupt during the wait) → DROP ⇒ force_drop →
  first chunk of epoch ⇒ ⏱ mark_first_send(+gate_hold) → send (binary) →
  pacing sleep whenever sent-ahead > prime_ms. The two epoch re-checks are the
  race-freedom of the whole output path.

### `src/gladiator/gemini/session.py` — 263 lines — load-bearing

Single responsibility: everything Gemini Live (§1.6, §1.7). Depends on bridge,
tools, prompts, discovery, config, events; runtime drives it.

- `GeminiSession.__init__(settings, bus, bridge, dispatcher, tracker)` — one
  `genai.Client`; `_send_lock` serializes ws writes (audio sender, tool
  responses, nudges are different tasks); resumption handle + greeting flags.
- `greeted` (property) — runtime reads it (roster/re-anchor gating).
- `_build_config()` — the entire LiveConnectConfig: modality AUDIO, SI from
  `system_instruction(tracker, search_as_function)`, tools (google_search
  unless in fallback mode + `build_declarations`), voice, both transcriptions,
  tuned `AutomaticActivityDetection`, compression window, resumption handle,
  minimal thinking. Rebuilt fresh per connection (the handle changes).
- `run()` — the supervised outer loop: connect → emit `gemini.connected`
  (reconnects/resumed flags) → reconnect-nudge or pending greeting →
  `_receive_loop` → on `ConnectionClosed` emit disconnected (normal recycle);
  other exceptions emit `gemini.error`, and after backoff ≥4s the possibly
  poisoned resumption handle is dropped; backoff 0.5→5s; `finally` clears
  `_session`.
- `stop()` — flag checked by the loop (shutdown).
- `_receive_loop(session)` — `while True: async for msg in session.receive()` —
  the per-turn-generator gotcha in code form.
- `handle_message(msg, session)` — independent `if`s: model_turn parts →
  `bridge.on_model_audio` ⏱; input/output transcription → transcript events;
  interrupted → `bridge.on_interrupted` ⏱; generation/turn complete → bridge;
  tool_call → dispatcher (immediate results batched into one
  `_send_tool_responses`; deferred ones spawn `_finish_deferred`);
  tool_call_cancellation → `dispatcher.cancel`; session_resumption_update →
  store handle; go_away → log; usage_metadata → `gemini.usage` (with the
  never-attach-logic warning comment). Takes `session` as a parameter so
  responses go to the delivering connection — also what makes it unit-testable
  with a stub.
- `_finish_deferred(fc, session)` — await `run_deferred`; drop if cancelled or
  the connection changed; else send late.
- `_send_tool_responses(responses, session)` — locked send; ConnectionClosed →
  `gemini.send_dropped`.
- `send_audio(pcm)` — locked `send_realtime_input(audio=Blob(rate=16000))`;
  silently tolerates a closed connection (frames are ephemeral).
- `send_audio_stream_end()` — hybrid-VAD knob (ships OFF).
- `send_text(text) -> bool` — locked; **clears the latency anchor** on success
  (nudge-caused turn) and emits `gemini.nudge`; returns delivery truth so
  callers only log real deliveries.
- `request_greeting()` / `_send_greeting()` — once-only; pending flag if the
  session isn't up yet (delivered on connect).

### `src/gladiator/gemini/tools.py` — 166 lines — load-bearing

Single responsibility: tool schema + dispatch (§1.7). Depends on discovery,
events; session drives it.

- `build_declarations(tracker, search_as_function)` — the five declarations;
  `question_id` enum built from the tracker's real ids; every description
  carries an "Invocation condition:"; `leave_meeting` explicitly excludes
  "stop"/"wait"/"hold on"; `web_search` appended only in fallback mode.
- `ToolDispatcher.__init__(tracker, bus, request_leave, web_search)` —
  `request_leave` is a *sync scheduler* (contract: instant); `web_search` an
  optional async backend; `_cancelled_ids` set.
- `handle(fc)` — emits `tool.call`, dispatches, catches *any* handler exception
  into an error result (a tool bug must never kill the receive loop), emits
  `tool.result`; returns None for deferred tools.
- `_dispatch(name, args, id)` — the routing table to tracker methods /
  `request_leave` / deferred-None.
- `run_deferred(fc)` — awaits the backend, emits the deferred `tool.result`.
- `cancel(ids)` / `is_cancelled(id)` — interruption bookkeeping.

### `src/gladiator/gemini/prompts.py` — 112 lines — load-bearing (behavior lives here)

Single responsibility: every word the model is told. Depends on discovery (for
the question list); session and runtime consume.

- `system_instruction(tracker, search_as_function)` — persona/names, voice
  style ("one question at a time", concise), the question list interpolated,
  the six tool rules with invocation conditions, conversation flow (greet
  at-most-once on the admission nudge; wrap-up protocol), guardrails
  (stop≠leave, bracketed notes never read aloud, side-conversations → stay
  quiet, English).
- `GREETING_NUDGE` / `SILENCE_NUDGE` — constants.
- `reconnect_nudge(progress_note)` — "connection restored, do not re-greet,
  state: …".
- `context_refresher(progress_note)` — the periodic state note: never re-ask/
  re-confirm, do not acknowledge this note, continue only at a natural opening.

### `src/gladiator/recall/client.py` — 81 lines — load-bearing

Single responsibility: Recall REST. Depends on config/events; runtime + CLI use
it. `RecallClient.__init__` — one `httpx.AsyncClient` with the region base URL
and `Authorization: Token …`. `create_bot(meeting_url)` — the full bot config
(§1.4); 4xx/5xx are evented with the response body before raising (Recall's
error bodies are the only debugging signal you get). `get_bot` (polling),
`leave` (POST leave_call), `close`.

### `src/gladiator/recall/events.py` — 58 lines — load-bearing

Single responsibility: translate Recall's event JSON into our primitives.
`RecallEventRouter.__init__(speech, bus, bridge, on_human_speech_off,
on_roster_change)` — both hooks optional. `handle(raw)` — unwraps Recall's
double-nested `data.data`; speech_on/off → SpeechState (+ hybrid hook when the
*last* human goes quiet); join/leave → roster + hook + floor release;
`audio_mixed_raw.data` → base64 → `feed_frame` (the entire transport fallback);
unknown events logged, never crash.

### `src/gladiator/web/agent/agent.js` — 129 lines — load-bearing

Single responsibility: the bot-side page — speaker, mic, face, clock (§1.8).
IIFE; talks only to `/ws/agent`. Pieces: `renderDebug`/`setFace` (DOM);
playback context+worklet setup with a 1s `resume()` keep-alive; `connect()` —
ws with auto-reconnect (1s), `onmessage` splits binary (header → epoch/seq →
transferable post to player; also opportunistic `resume()`) vs JSON (`flush` →
worklet, `face`, `pong` → offset=(t0+t3)/2−t1 → send `clock`);
`player.port.onmessage` — `started`→`play_ack` ⏱ (the page-side waterfall
mark), `buffer`→debug; 4s ping interval; capture path — `getUserMedia` with
AEC/NS/AGC disabled → 16k context → worklet → `onmessage` prepends f64
timestamp, sends binary; `sendCaptureState`/`sendFail` retry until the ws is
open (capture readiness gates the greeting server-side).

### `src/gladiator/web/agent/capture-worklet.js` — 34 lines — load-bearing

`CaptureProcessor` — audio-thread accumulator: copies 128-sample quanta into a
320-sample buffer; on fill, Float32→Int16 (with clamping) and posts the buffer
*transferably* (zero-copy). Nothing else — the audio thread must stay tiny.

### `src/gladiator/web/agent/player-worklet.js` — 54 lines — load-bearing

`PlayerProcessor` — the jitter buffer's playing end. `onmessage`: `chunk` —
ignored if below current epoch; Int16→Float32; queued. `flush` — queue cleared
instantly, epoch raised (barge-in cutoff lives here). `process` — fills each
128-sample output from the queue head; posts `started` exactly when a new
epoch's first sample renders (true playback timestamp ⏱); zeros on underrun
(silence, not garbage); ~2×/sec posts queue depth. Always returns true (node
lives across turns).

### `src/gladiator/web/dashboard/dashboard.js` — 238 lines — load-bearing (rubric line)

Single responsibility: pure event-reducer UI (§1.13) — identical for live and
replay. Pieces: chip/face helpers; question panel (`initQuestions` from the
hello snapshot — definitions aren't in events; `renderAnswer`, `setQStatus` —
never regresses answered→asked, `renderProgress`); transcript
(`transcriptAppend` accumulates streaming deltas into per-speaker lines,
`transcriptBreak` on turn boundaries); latency (`renderWaterfall` keeps last 8
turns, shared scale; `renderLatStats` client-side p50/p95); event log
(`logEvent` with filter + follow + 600-row cap; `bridge.stats` suppressed as
noise); `reduce(ev)` — the switch mapping every event type to a renderer;
transport `connect()` — hello→snapshot init, backlog→fold, event→reduce,
control_result→button state; auto-reconnect; control buttons (leave confirm,
mute toggle, greet, nudge, download JSONL from memory); `esc()` — HTML
escaping (events contain model/user text — this is XSS hygiene, not
decoration).

### HTML: `web/agent/index.html`, `web/dashboard/index.html` — peripheral

Static shells: the agent page is the bot's camera tile (orb + state label +
debug line — legible at Meet-tile size on purpose); the dashboard html is
layout + CSS for the five panels. No logic beyond loading the JS.

### Tests (`tests/`) — peripheral to the demo, central to the grade

One file per invariant-cluster; see §5 for the mapping. `conftest.py` gates the
5 real-API tests behind `RUN_INTEGRATION=1`. Fixture WAVs are generated by
`scripts/record_fixtures.sh` (macOS `say` → 16kHz mono).

### Scripts & config files — peripheral

`scripts/run_demo.sh` (tunnel+server+bot, ngrok primary/cloudflared fallback,
cleanup trap), `scripts/record_fixtures.sh`, `pyproject.toml` (uv project,
pytest asyncio-auto, ruff config incl. per-file ignores), `.env.example`,
`.gitignore` (`.env`, `runs/`, fixtures, `bin/`).

---

## §4 Design decisions

Format: **Decision** — rejected alternative — the one-line defense.

1. **All intelligence server-side; the page is a dumb mic/speaker.** Rejected:
   Recall's own demo pattern (agent logic in the page, server as relay).
   *Defense*: Gemini Live is server-to-server auth — a key in a publicly
   tunneled page is a leak — and every rubric line (latency, dashboard,
   debugging) needs the logic where we can instrument it.

2. **Audio input from the page's getUserMedia, not Recall's websocket.**
   Rejected: `audio_mixed_raw` server-side feed. *Defense*: the page gives
   ~20ms frames versus Recall's fixed 200ms chunks — a 10× granularity
   difference on the input-latency floor — and the fallback still exists behind
   one config flag (`AUDIO_SOURCE=recall_ws`).

3. **Model: `gemini-3.1-flash-live-preview`, pinned.** Rejected:
   `gemini-2.5-flash-native-audio-*` (and `-latest` aliases). *Defense*: 2.5
   has an open truncation bug that worsens under exactly our workload (tool
   calls + compression), and pinning beats hot-swapped aliases for demo
   reproducibility.

4. **google_search combined with function declarations in one session.**
   Rejected: assuming the docs conflict resolves against us and building
   search-as-a-function first. *Defense*: we *tested* the combination on this
   key (doctor re-verifies each run) and kept the fallback implemented but
   dormant — evidence over doc-lawyering, with the escape hatch ready. (If
   probed on the constraint: even in fallback, all conversational reasoning
   stays in the Live session; the grounded call is a retrieval backend.)

5. **Floor gate: local EnergyVAD authoritative while live; Recall events as
   fallback + attribution.** Rejected (in order, each falsified live):
   events-only (they barely fire in small meetings); VAD∧events conjunction
   (phantom events blocked a silent room); VAD-only (blind to stalled feeds
   and soft speakers). *Defense*: the meeting audio is the only signal that's
   always truthful *while it flows*, so authority follows liveness — with an
   explicit staleness handover instead of a silent failure.

6. **Hard-stop watchdog: live VAD is final; events only when the VAD is
   stale.** Rejected: the union of both signals (we shipped it first).
   *Defense*: multi-party testing falsified the union — speech events stay
   "on" through a speaker's between-sentence pauses, so event-triggered kills
   shredded agent audio into 50–250ms blips (18 of 23 kills with a quiet
   mic); the residual soft-speaker risk is carried by Gemini's own barge-in
   VAD and a configurable RMS threshold.

7. **Stale-drop: a gated answer older than 2.5s is deleted, not played.**
   Rejected: play it when the human stops (simpler). *Defense*: a reply to a
   conversation that has moved on is worse than silence — and we repair the
   model's beliefs afterwards instead (drop-recovery note), which the simpler
   design can't do.

8. **Epoch counters for cancellation.** Rejected: per-chunk IDs/acks or
   tearing down the audio node per turn (the pattern in Recall's demo).
   *Defense*: one integer compare at three checkpoints gives O(1) flush and
   race-freedom across server, pacer, and page — no bookkeeping can be
   "missed".

9. **Receive loop decoupled from playback by an unbounded, epoch-flushed
   buffer.** Rejected: a bounded queue between Gemini and the pacer.
   *Defense*: the receive loop carries `interrupted` — any backpressure on
   audio delays the one signal that must never be late; unbounded is safe
   because a barge-in empties it in O(1).

10. **160ms page-side prime, 40ms pacing chunks.** Rejected: bigger buffers
    (smoother) or dump-everything (simplest). *Defense*: 160ms absorbs tunnel
    jitter yet bounds the worst-case audio a listener hears after an
    interruption; the waterfall proves the choice with numbers instead of
    vibes.

11. **Deterministic periodic re-anchoring; no eviction detection.** Rejected:
    trigger the state note on usage-token drops (built, then deleted).
    *Defense*: live data proved the usage counter is per-turn accounting noise,
    and a false nudge makes the model *speak* — never attach behavior to a
    signal you can't trust when a timer does the job deterministically.

12. **Compression window 100k→64k (not the docs' 25k/8k example).** Rejected:
    docs default. *Defense*: audio costs ~25 tokens/sec/direction, so the
    docs' example evicts live conversation every ~2 minutes — sized correctly,
    eviction simply cannot occur within a meeting.

13. **Server-side discovery state as the single source of truth.** Rejected:
    trusting the model's conversational memory. *Defense*: model memory is
    subject to compression, reconnects, and hallucination; the tracker survives
    all three and every nudge re-seeds from it.

14. **Every mid-session steering message is a quiet-floor-gated text nudge.**
    Rejected: sending state whenever convenient. *Defense*: a nudge is a user
    turn — the model responds to it — so delivery timing is a correctness
    concern; one shared `_wait_for_quiet` predicate enforces it everywhere.

15. **Tool handlers mutate local state only; `leave_meeting` fires REST in the
    background after playout.** Rejected: awaiting the REST call in the
    handler. *Defense*: the model is frozen until the tool response returns —
    and the goodbye must finish before the bot leaves, which the handler can't
    wait for.

16. **Latency anchors: consume-once + cleared on nudges.** Rejected: a max-age
    heuristic (built, then deleted). *Defense*: age both censored genuinely
    slow turns from the p95 and still misattributed fast nudges; the runtime
    *knows* causality (it sends the nudges), so encode knowledge, not
    guesses.

17. **Event-sourced dashboard + JSONL + replay.** Rejected: server-rendered
    state pushes. *Defense*: one write path gives the log, the live UI, and
    post-mortem replay for free — the replay command is the same dashboard fed
    the same events.

18. **One asyncio process.** Rejected: split services (audio worker, API,
    dashboard). *Defense*: every hop between processes adds latency and
    failure modes to a realtime path that fits comfortably in one event loop —
    and a take-home should be runnable with one command.

19. **Supervised background tasks.** Rejected: bare `create_task`. *Defense*:
    asyncio silently swallows exceptions in unawaited tasks — a single
    transport hiccup would have invisibly killed a safety loop for the rest of
    the meeting.

20. **Vanilla JS, no build step; argparse, not click.** Rejected: React/
    bundlers, richer CLI frameworks. *Defense*: reviewers read every line of a
    take-home — 400 lines of dependency-free JS is inspectable in one sitting,
    and nothing here needs a component tree.

21. **ngrok with a reserved static domain.** Rejected: cloudflared quick
    tunnels (zero signup). *Defense*: Recall marks a realtime endpoint
    permanently failed after 30 retries — an ephemeral URL turns any tunnel
    restart into an unrecoverable mid-demo failure; the config stays
    tunnel-agnostic regardless.

---

## §5 Invariants and failure modes

Each: the invariant → what breaks if violated → the guarding test(s).

1. **The Gemini receive loop never blocks** (only O(1) sync work + fast locked
   writes). Violated → `interrupted` arrives late → the bot keeps talking over
   humans; tool responses stall the model. Guard:
   `test_receive_loop.py::test_interrupted_processed_instantly_with_saturated_backlog`
   and `::test_tool_call_processed_while_backlog_pending`.

2. **Epochs are bumped on every turn/flush, and stale epochs are discarded at
   pacer AND page.** Violated → cancelled audio resurrects later (ghost
   speech). Guard: `test_latency.py::test_pacer_discards_stale_epoch_chunks`,
   `test_receive_loop.py` (epoch advance on interrupt),
   `test_e2e_ws.py` (page protocol end-to-end).

3. **No agent audio leaves while a human holds the floor; grace precedes any
   start.** Violated → the headline requirement fails. Guard:
   `test_turn_gate.py` (grace/hold/release/two-speaker/leave-release),
   `test_vad_gate.py` (VAD holds without events; phantom events can't block a
   quiet room; events gate when VAD disabled), `test_stale_policy.py`.

4. **A response held past 2.5s is dropped — including its still-streaming
   tail — and never plays late.** Violated → out-of-date answers play into a
   moved-on conversation. Guard:
   `test_stale_policy.py::test_stale_answer_dropped_not_played_after_monologue`.

5. **`_discarding` is sticky across force-drop re-fires, and force-drops count
   as handled overlaps.** Violated → a killed generation's tail replays as a
   fresh turn (mid-sentence fragment). Guard:
   `test_bridge.py::test_force_drop_discard_is_sticky_across_refires`.

6. **A VAD without recent frames holds no authority (gate falls back to
   events).** Violated → frozen-quiet talks over humans / frozen-voice mutes
   the bot forever. Guard:
   `test_vad_gate.py::test_stale_vad_falls_back_to_events_gating`.

7. **While the VAD is live, speech events can never hard-stop agent audio;
   when it's stale, they back-stop (pruned).** Violated → between-sentence
   pauses shred agent speech into blips (observed live); or a dead feed
   removes the last overlap protection. Guard:
   `test_vad_gate.py::test_hard_stop_vad_is_final_while_live`.

8. **The bot never gates on its own voice/name.** Violated → self-deadlock
   (it waits for itself to stop talking). Guard:
   `test_turn_gate.py::test_bot_own_speech_events_ignored`,
   `test_recall_events.py::test_bots_own_speech_filtered`.

9. **The greeting fires at most once, only after admission, on a quiet floor.**
   Violated → greeting the waiting room, or a stale-dropped ("mute") greeting.
   Guard: `test_drop_recovery.py::test_greeting_waits_for_quiet_floor` /
   `::test_greeting_immediate_when_room_is_quiet` (+ the SI's greet-once rule
   and `runtime._maybe_greet` guards).

10. **Every force-drop leads to belief repair (unless superseded).** Violated →
    the model thinks it said things nobody heard; conversation desyncs.
    Guard: `test_drop_recovery.py::test_dropped_response_note_after_floor_frees`
    / `::test_no_note_if_model_already_moved_on`.

11. **Nudges are delivered only at quiet moments with no generation in flight,
    and logged only when actually delivered.** Violated → a nudge cancels a
    pending answer, or the event log lies about deliveries. Guard:
    `test_context_reanchor.py` (all six tests: quiet-moment, open-generation,
    wrapup/terminal, delivery-truth, pre-greeting).

12. **Speech anchors are consume-once and cleared on nudges.** Violated →
    nudge turns inherit stale timing (fake multi-second detect_think) or slow
    real turns get censored from p95. Guard:
    `test_latency.py::test_anchor_consumed_once_and_clearable`.

13. **The pacer never exceeds ~prime_ms of page-side head start.** Violated →
    smooth audio but slow barge-in cutoff (buffered seconds keep playing).
    Guard: `test_latency.py::test_pacer_keeps_bounded_head_start`.

14. **Discovery state never regresses** (answered rows survive re-asks and
    skip attempts; revisions accumulate). Violated → the dashboard lies; the
    idempotency hidden-test fails. Guard: `test_discovery.py` (all).

15. **Background tasks survive uncaught exceptions (logged + restarted).**
    Violated → a safety loop dies silently for the rest of the meeting. Guard:
    `test_supervisor.py` (both tests).

16. **The bot leaves only after the goodbye finishes draining (bounded).**
    Violated → mid-sentence ejection. Guard: `test_leave_playout.py` (both).

17. **The whole wire path works end-to-end** (page protocol ↔ runtime ↔ real
    Gemini: greeting delivery, spoken answer → `record_answer`, waterfall with
    real acks). Guard: `test_e2e_ws.py::test_full_stack_over_websockets`
    (integration), plus `test_gemini_integration.py` (names, tools-from-voice,
    barge-in, search — the visible test cases, recorded).

---

## §6 Glossary

- **Anchor (latency)** — the timestamp of the user's last voiced audio; the start line for detect_think.
- **Barge-in** — a human talking over the agent; must cut the agent off instantly.
- **Detect_think / serve / transit_play / e2e** — the waterfall segments (speech-end→first-audio / →first-send / →page-play / total).
- **Discard mode (`_discarding`)** — post-drop state where the rest of a killed generation is eaten until the turn boundary.
- **Doctor** — `gladiator doctor`; preflight validation of keys, model, combined tools, tunnel.
- **Epoch** — the generation counter stamped on all outbound audio; stale-epoch data is dropped everywhere.
- **Face** — the agent page's state orb (idle/listening/thinking/speaking/holding), mirrored on the dashboard.
- **Floor** — the right to speak; managed by FloorGate.
- **Grace** — 300ms of confirmed silence required before agent audio may start.
- **Hangover** — 300ms of quiet before the EnergyVAD declares speech ended.
- **Hard-stop** — watchdog force-drop when a human talks over playing agent audio ≥400ms.
- **Hybrid VAD** — optional latency knob: Recall speech_off → `audio_stream_end` to skip Gemini's silence wait; ships OFF.
- **Nudge** — a bracketed system note sent as realtime text; the model responds to it.
- **Output Media** — Recall's mechanism where a webpage's audio/video becomes the bot's camera/voice.
- **Pacer** — the task releasing 40ms chunks at realtime rate through the gate.
- **Phantom event** — a Recall speech_on with no speech_off (observed live); why events lost gate authority.
- **Prime / jitter buffer** — the ≤160ms of audio kept ahead on the page against network jitter.
- **Re-anchor** — the periodic context_refresher nudge carrying authoritative discovery state.
- **Resumption handle** — Gemini's token letting a new connection resume a session's context.
- **Ring** — EventBus's in-memory last-20k events (dashboard backlog).
- **Stale-drop** — deleting an answer that waited >2.5s behind human speech.
- **Staleness (VAD)** — no frames for 1.5s → the VAD loses gate authority.
- **Turn** — one model generation, from `turn.start` to complete/interrupted/dropped.
- **Waterfall** — the per-turn latency bar on the dashboard.
- **web_4_core** — Recall's beefier bot variant; the default stutters under Output Media.

---

## §7 Interview prep — 15 likely questions

**Q1. Why does the agent's audio go through a webpage at all?**
Recall has no streaming-audio output API — Output Media (a page the bot renders,
whose audio becomes the bot's voice) is the only way to speak continuously. We
chose to keep that forced page *thin* (mic/speaker/face) and the brain
server-side (`runtime.py`), because Gemini Live keys are server-to-server only
and all the graded instrumentation lives with the logic.

**Q2. Walk me through what happens between me finishing a sentence and hearing
the bot.**
Trace T2: your last voiced frame timestamps the anchor (`EnergyVAD` hangover →
`mark_speech_end`); Gemini's own VAD closes your turn after ~700ms silence;
first audio part back → `on_model_audio` (⏱ first_audio, epoch++); pacer clears
the floor gate (⏱ first_send + gate hold), streams 40ms chunks; the page worklet
renders and acks true playback (⏱ page_play via clock sync); `latency.turn`
event carries the waterfall. Typical e2e ≈ 2s, dominated by the deliberate
700ms silence window + generation.

**Q3. How do you guarantee it never interrupts a human?**
Two independent layers. Before speaking: `FloorGate.wait_for_floor` per 40ms
chunk — needs the room quiet (live EnergyVAD on the actual meeting audio,
falling back to Recall events when the VAD is stale) plus 300ms grace. While
speaking: Gemini's `interrupted` flushes everything in O(1) (epoch bump + page
flush), and if Gemini misses it, `_hard_stop_due` force-drops on 400ms of
VAD-heard overlap (events back-stop only if the feed dies). Guarded by `test_turn_gate`,
`test_vad_gate`, `test_stale_policy`.

**Q4. Why isn't the Recall speech-event stream enough for turn-taking?**
Measured live, twice: in a single-participant Meet it produced one `speech_on`
and zero `speech_off` in four minutes, and separately a phantom `speech_on`
fired while the bot was still in the waiting room — blocking the floor against
a provably silent room (RMS 0). Events are now attribution + fallback; the
meeting audio itself is the authority while it flows (`turn_gate._live_vad`).

**Q5. Your VAD is a loudness threshold — what about a soft speaker below it?**
A knowingly accepted residual risk, and the story shows why: we first unioned
Recall events into the hard-stop to cover exactly this — and multi-party
testing falsified it (events stay on through between-sentence pauses; 18 of 23
kills fired with a provably quiet mic, shredding agent speech into blips). Now
a live VAD's word is final; events back-stop only if the audio feed dies. What
still protects a soft speaker: Gemini's own VAD hears the full mix and
barge-ins independently, and the RMS threshold is a config knob.

**Q6. What happens if the page's audio feed dies mid-call?**
`EnergyVAD.is_stale` (no frames for 1.5s) strips the VAD's authority, so the
gate falls back to Recall events instead of trusting a frozen reading — a
frozen "quiet" would have opened the floor over talking humans, a frozen
"voice" would have muted the bot forever. Guard:
`test_vad_gate::test_stale_vad_falls_back_to_events_gating`.

**Q7. Why an unbounded buffer between Gemini and the pacer — isn't that a
memory risk?**
The receive loop delivers `interrupted`; any backpressure on it delays the one
signal that must never be late (a bounded queue would block exactly when a long
answer needs cancelling). It's safe because content is bounded by a single
response's length and every barge-in/drop empties it in O(1) via the epoch
bump. Guard: `test_receive_loop` (interrupt processed <50ms under a 60s
backlog).

**Q8. How does an interrupted response avoid "ghost audio" from buffers?**
Three checkpoints all compare epochs: the buffer flush bumps the epoch, the
pacer re-checks it after *every* await, and the page worklet ignores
below-epoch chunks + clears its queue on the flush message. Plus `_discarding`
eats the server-side tail of a force-dropped generation until its turn
boundary — and it's sticky across watchdog re-fires
(`test_bridge::test_force_drop_discard_is_sticky_across_refires`).

**Q9. Where does the dashboard's data come from, and why does replay "just
work"?**
Everything is one event stream (`EventBus`): JSONL file + in-memory ring + live
subscriber queues. `dashboard.js reduce()` is a pure fold over events —
`gladiator replay` feeds the same page the recorded file with re-timed delays
(`main.cmd_replay`), so live and post-mortem rendering are literally the same
code path.

**Q10. How do you measure latency on a machine you don't control (the bot's
browser)?**
NTP-style: the page pings every 4s; offset = (t0+t3)/2 − t1, kept as a rolling
median in `ClockSync`. The player worklet reports the instant a new epoch's
first sample actually *renders* (`started` → `play_ack`), which
`mark_page_play` converts onto the server's monotonic timeline — so
`transit_play` is a measurement, not an estimate.

**Q11. What keeps a 45-minute meeting alive when Gemini sessions cap at 15
minutes?**
Three mechanisms, all needed: context-window compression sized for audio
(100k→64k; the docs' 25k/8k example evicts live conversation every ~2 minutes —
we measured the resulting amnesia), session resumption handles across the
~10-minute connection recycles (`session.run` reconnect loop), and state
re-seeding nudges (reconnect + periodic re-anchor) built from the server-side
tracker, which never forgets.

**Q12. Why did you delete your compression-eviction detector?**
Live falsification: `usage_metadata.total_token_count` turned out to be
per-turn accounting noise — interrupted turns report partial usage, and totals
swing 4.2k→8.9k→4.9k within a minute — and a false trigger *makes the model
speak* (a nudge is a user turn; we watched it re-confirm answered questions).
Deterministic periodic re-anchoring replaced it; dips aren't even logged
anymore. The lesson I'd state: never attach behavior to a signal you can't
trust.

**Q13. What happens if the model calls `record_answer` with a question id that
doesn't exist?**
It can't, mostly — `question_id` is a schema *enum* built from the tracker's
real ids (`tools.build_declarations`). If something slips through anyway,
`DiscoveryTracker.record_answer` returns an error *containing `valid_ids`*, so
the model self-corrects on the next call, and `discovery.unknown_question` is
evented. The dispatcher also wraps every handler so a tool bug can never kill
the receive loop.

**Q14. Someone says "stop" — what does the bot do, and why is that a designed
behavior?**
It stops talking and stays in the meeting. Early testing had it *leave* (the
model read "stop" as "end the meeting"), so the system instruction and the
`leave_meeting` tool description now explicitly define stop/wait/hold-on as
stop-talking, require unambiguous leave phrasing, and tell it to ask when
unsure. Mechanically, the stop itself is just barge-in (T4).

**Q15. Where's the biggest remaining weakness, honestly?**
Speaker attribution: the model hears one mixed stream (no diarization), the
transcript labels sides not people, and Meet's adaptive audio can merge a
whole room into one "participant". The upgrade path is Recall's per-participant
streams feeding labeled text — rejected for now because Gemini Live accepts one
audio stream, and rebuilding a transcribe-then-reason cascade sacrifices the
audio-native latency the assignment is graded on. It's documented in DESIGN.md
§12 rather than hidden.

---

*Companions: DESIGN.md (decision rationale in depth), README.md (operations),
`git log` (each live finding as a commit story), `runs/*/events.jsonl` +
`gladiator replay` (the receipts).*
