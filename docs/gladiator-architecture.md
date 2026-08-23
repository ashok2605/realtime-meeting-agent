# Gladiator — Architecture Diagrams

Screen-share companion. Four diagrams, each with a numbered narration script —
present them in order, top to bottom. Technically exact per docs/WALKTHROUGH.md.

---

## 1. System topology — where things run and how they connect

```mermaid
flowchart LR
    subgraph MEET["Google Meet"]
        HUM(["Participants"])
    end

    subgraph RECALL["Recall.ai cloud"]
        BOT["Bot = headless Chrome<br/>(variant web_4_core)"]
        PAGE["our /agent page<br/>capture-worklet · player-worklet · face"]
        BOT -- "renders as bot camera,<br/>page audio = bot voice" --- PAGE
    end

    subgraph LAPTOP["Laptop — one asyncio process (FastAPI)"]
        SRV["gladiator server<br/>(Runtime hub)"]
        DASH["Dashboard<br/>localhost only"]
    end

    GEM["Gemini Live API<br/>gemini-3.1-flash-live-preview"]

    HUM -- "meeting audio becomes<br/>the page microphone" --> PAGE
    PAGE == "wss /ws/agent (via ngrok)<br/>IN: 20ms PCM 16k binary<br/>OUT: 40ms PCM 24k, epoch-stamped" ==> SRV
    RECALL -- "wss /ws/recall (via ngrok)<br/>participant join/leave, speech on/off" --> SRV
    SRV -- "REST: create bot, poll status, leave" --> RECALL
    SRV == "wss (google-genai SDK)<br/>audio in 16k / audio out 24k<br/>tool calls · transcription" ==> GEM
    SRV -- "wss /ws/dashboard<br/>event stream + controls" --> DASH
```

**Narrate it like this:**
1. "Recall puts a bot into the Meet. Its camera is a webpage *we* serve — that's
   the only way Recall supports streaming speech out, and conveniently the page's
   microphone *is* the meeting audio."
2. "The page is deliberately dumb — a mic, a speaker, a status face. All
   intelligence is in one Python process on my laptop, because Gemini Live is
   server-to-server auth and everything worth observing — latency, dashboard, debugging —
   wants to live where I can instrument it."
3. "The tunnel exists only because Recall's cloud must reach my laptop (the page
   plus its event stream). Nothing is deployed."
4. "Thick edges are the realtime audio path; thin edges are control. Note the
   two separate Recall channels: audio rides the page, events ride
   `/ws/recall`."

---

## 2. Inside the server — components and the audio engine

```mermaid
flowchart TB
    subgraph IN["inbound: meeting to model"]
        WS1["/ws/agent handler<br/>Runtime.handle_page_ws"]
        FF["AudioBridge.feed_frame"]
        VAD["EnergyVAD<br/>RMS threshold + 300ms hangover<br/>in_voice · voice_since · staleness"]
        Q["inbound queue (64 frames,<br/>drop-oldest)"]
        SND["inbound_sender task"]
        GSI["GeminiSession.send_audio"]
    end

    subgraph OUT["outbound: model to meeting"]
        RL["GeminiSession receive loop<br/>(never blocks — carries interrupted + tool calls)"]
        BUF["TurnAudioBuffer<br/>unbounded · epoch-stamped · O(1) flush"]
        PAC["Pacer task<br/>40ms chunks, ≤160ms page head start"]
        GATE["FloorGate<br/>live VAD authoritative,<br/>events fallback · 300ms grace ·<br/>2.5s stale-drop"]
        WS2["binary to page<br/>[epoch][seq][pcm]"]
    end

    subgraph STATE["state and observability"]
        DISP["ToolDispatcher → DiscoveryTracker<br/>(authoritative Q&A state)"]
        LAT["LatencyTracker + ClockSync<br/>per-turn waterfall"]
        BUS["EventBus<br/>ring + JSONL + dashboard ws"]
    end

    SS["SpeechState<br/>(Recall speech events,<br/>bot self-filtered)"]

    WS1 --> FF --> Q --> SND --> GSI
    FF --> VAD
    RL -- "audio parts" --> BUF --> PAC --> WS2
    PAC -- "may I speak?" --> GATE
    VAD -- "primary signal" --> GATE
    SS -- "fallback + attribution" --> GATE
    RL -- "tool_call" --> DISP
    RL -- "interrupted" --> BUF
    VAD -- "speech-end anchor" --> LAT
    DISP --> BUS
    LAT --> BUS
    GATE --> BUS
```

**Narrate it like this:**
1. "Follow the inbound lane: 20ms frames come off the page websocket, brush past
   a local energy VAD — pure RMS math, no model — and queue toward Gemini.
   Drop-oldest on that queue means a Gemini reconnect can never back audio up
   into the websocket handler."
2. "The outbound lane is the core invariant: the receive loop also carries the
   *interrupted* signal and tool calls, so it must never block. It just appends
   into an epoch-stamped buffer; a separate pacer drains at realtime rate."
3. "Before every 40ms chunk, the pacer asks the FloorGate: is a human speaking?
   The gate trusts the VAD while its feed is live, falls back to Recall's
   speech events when it's stale, and drops answers that waited more than 2.5
   seconds — a late answer is worse than none."
4. "Everything on the right is observability: tool calls mutate the
   server-side question tracker — the model's memory is never the source of
   truth — and every event lands on one bus feeding the log, the dashboard,
   and the replay tool."

---

## 3. Golden path — one spoken exchange, with latency capture points

```mermaid
sequenceDiagram
    autonumber
    participant U as You (in Meet)
    participant P as /agent page
    participant S as Server (Runtime/Bridge)
    participant G as Gemini Live

    U->>P: speak (meeting mix = page mic)
    P->>S: 20ms PCM frames (binary ws)
    S->>G: send_realtime_input(audio 16k)
    Note over S: ⏱ EnergyVAD hangover fires →<br/>mark_speech_end (true last-voiced instant)
    Note over G: server VAD waits ~700ms silence,<br/>then generates (audio native)
    G-->>S: audio parts (24k, burst)
    Note over S: ⏱ mark_first_audio (epoch++)<br/>segment 1: detect+think
    S->>S: Pacer asks FloorGate → OPEN (quiet + 300ms grace)
    Note over S: ⏱ mark_first_send (+gate hold)<br/>segment 2: serve
    S->>P: 40ms chunks [epoch][seq][pcm], ≤160ms ahead
    P->>U: player-worklet renders → tab audio → Meet
    P->>S: play_ack (first sample of epoch rendered)
    Note over S: ⏱ mark_page_play via ClockSync<br/>segment 3: transit+play → emit latency.turn
    G-->>S: tool_call record_answer
    S->>G: FunctionResponse (microseconds — local state only)
    Note over S: DiscoveryTracker updates →<br/>dashboard row flips live
```

**Narrate it like this:**
1. "Three clocks are involved — page, server, Gemini — so the waterfall is built
   from real timestamps: the VAD anchors when you *actually* stopped talking,
   and the page acks when the first sample *actually* rendered, converted
   through NTP-style clock sync."
2. "The ~2s typical end-to-end splits as: detect+think ≈ 1.8s — mostly
   Gemini's deliberate 700ms silence window plus generation — serve ≈ 0
   when the floor is free, transit+play ≈ 170ms of tunnel plus jitter buffer."
3. "Tool calls ride the same stream. The handler only mutates local state, so
   the model — which is frozen until we respond — is unblocked in
   microseconds."

---

## 4. Interruption and floor-gating — both directions of "never talk over"

```mermaid
sequenceDiagram
    autonumber
    participant U as Human
    participant P as Page (player-worklet)
    participant PC as Pacer + FloorGate
    participant B as TurnAudioBuffer
    participant G as Gemini Live

    rect rgb(120, 90, 60)
    Note over U,G: Direction 1 — human interrupts Glad (barge-in)
    U->>G: speaks over playing audio (input never stops)
    G-->>B: interrupted: true
    B->>B: flush() — epoch++ in O(1)
    B->>P: {"type":"flush"} — worklet queue cleared
    Note over P: ≤160ms buffered = max overhang ever heard
    PC->>PC: wakes, sees stale epoch, discards chunk
    end

    rect rgb(60, 90, 120)
    Note over U,G: Direction 2 — Glad never interrupts a human
    G-->>B: answer arrives while human is talking
    PC->>PC: wait_for_floor — HOLD (VAD in_voice)
    alt human stops within 2.5s
        PC->>P: plays after 300ms grace
    else human keeps talking
        PC->>B: DROP → force_drop: flush + discard tail (sticky)
        Note over PC: later, floor quiet → nudge Gemini:<br/>"participants could NOT hear your last response —<br/>briefly restate"
    end
    Note over PC: backstop: 400ms of VAD-heard overlap, no interrupted →<br/>hard-stop force-drop (events back-stop only if the audio feed dies)
    end
```

**Narrate it like this:**
1. "Humans interrupting Glad is latency-critical: Gemini's own VAD detects it,
   and one epoch increment kills the server buffer in O(1) while a flush
   message empties the page's worklet queue. The page only ever holds 160ms,
   so that's the worst overhang you can hear."
2. "Glad never interrupting humans is correctness-critical, so it doesn't trust
   any single signal: the gate holds on the live audio VAD, answers stale after
   2.5 seconds get deleted rather than played late, and a 400ms hard-stop
   watchdog covers the case where Gemini misses the overlap."
3. "The subtle part: a dropped answer exists in the model's memory but was
   never heard — so once the floor frees, we tell the model exactly that, and
   it restates naturally. Turn-taking isn't just audio plumbing; it's keeping
   the model's beliefs synchronized with reality."

---

*Deep dives: docs/WALKTHROUGH.md (traces + file map), docs/DESIGN.md (decision
rationale), `gladiator replay runs/<ts>/events.jsonl` (live receipts).*
