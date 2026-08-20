# Gladiator — Design Notes

Decisions, trade-offs, and failure-mode analysis. Written to be defensible line-by-line.

## 1. Model: `gemini-3.1-flash-live-preview` (pinned)

Google's stated recommendation for all Live API use cases. The alternative on this key,
`gemini-2.5-flash-native-audio-*`, has an open P2 defect (googleapis/python-genai#2117):
`turnComplete` without `interrupted` mid-audio, truncating responses — reported to worsen with
function calling and context-window compression, which is exactly this workload. Pinned (not
`-latest`) so a hot-swap can't change behavior between rehearsal and demo.

Costs accepted with 3.1: no `NON_BLOCKING` async function calling (fine — all our handlers are
sub-millisecond local mutations), and one server event can carry several content parts, so the
receive loop uses independent `if`s, never `elif` (regression-tested in
`test_multipart_event_processes_all_parts`).

## 2. Audio out: Output Media webpage (the only streaming option)

Recall has no websocket audio *output*; the only way to stream dynamic speech into a meeting is a
webpage the bot renders in headless Chrome, whose audio becomes the bot's voice. Consequences:

- The page is a deliberately thin **speaker + microphone + status face**. All intelligence is
  server-side, because (a) Gemini Live is server-to-server auth only — the key must not reach a
  publicly-tunneled page; (b) latency and behavior are observable/logged in one process;
  (c) the bot's Chrome is capped CPU we don't control.
- `variant: web_4_core` — the default 250-millicore bot audibly stutters under Output Media
  (Recall's own FAQ and their voice-agent demo both use `web_4_core`).
- The camera tile is always visible (can't be disabled) → the page renders Gladiator's live state
  (listening / thinking / speaking / holding), which doubles as an in-meeting debug display.

**Smoothness chain:** Gemini emits audio in bursts → server re-chunks to 40 ms frames →
Pacer releases at realtime rate keeping ~160 ms queued page-side → AudioWorklet renders
sample-accurately on the audio thread (immune to main-thread jank). Jitter is absorbed by the page
buffer; burstiness never reaches the meeting.

## 3. Audio in: page capture, with a transport-agnostic fallback

Primary: the page's `getUserMedia` (Recall injects the meeting mix as the page's microphone),
captured at 16 kHz by an AudioWorklet, shipped as 20 ms Int16 frames — matching Gemini's
recommended 20–40 ms input chunking. AEC/NS/AGC are explicitly disabled: the "mic" is already a
clean conference mix, and Chrome's processing would fight it.

**Fallback (`AUDIO_SOURCE=recall_ws`):** Recall's `audio_mixed_raw` websocket (16 kHz mono s16le)
delivers **200 ms chunks** — a fixed ~200 ms latency floor on the input path, ~10× Gemini's
recommended chunk size. The bridge is transport-agnostic (`AudioBridge.feed_frame` is the single
entry point; `recall/events.py` feeds it identically), so the swap is one config flag and confined
to one module. Trade-off if engaged: input-path latency grows by up to ~200 ms per turn and VAD
granularity coarsens; everything else is unchanged. We keep it because it removes the only
empirically-unverifiable dependency in the design (getUserMedia behavior inside Recall's Chrome).

## 4. Turn-taking: layered guarantees

"Humans always interrupt Glad" and "Glad never interrupts humans" have different failure costs, so
they get different mechanisms:

**Humans interrupt Glad (latency-critical):** Gemini's server VAD detects barge-in and sends
`interrupted`. One synchronous hop later: the server buffer is epoch-flushed (O(1)) and the page
is told to drop its worklet queue. Because the page holds ≤ ~200 ms, cutoff is near-instant.
Tested: `test_interrupted_processed_instantly_with_saturated_backlog` (flush happens in <50 ms
even with 60 s of undelivered audio queued).

**Glad never interrupts humans (correctness-critical):** an independent outer gate fed by **two
signals**, consulted by the Pacer before every 40 ms chunk:

1. *EnergyVAD on the live meeting audio* — **primary, while live**. Continuous and truthful.
   Added after the first live rehearsal falsified the original events-only design: in a
   single-participant Google Meet, Recall delivered exactly one `speech_on` and zero `speech_off`
   in 4 minutes, which first held the gate on a phantom speaker for 45 s (dropping the greeting
   as stale) and then, after the stuck-speaker prune, left the gate wide open for the rest of the
   call. A later run showed events also fire at bogus times (a speech_on pre-admission blocked
   the reply to a provably silent room). The rehearsal logs proved the page's mic feed excludes
   the bot's own voice (input RMS zero during every agent turn) — on the page path only, which is
   why VAD authority is restricted to `audio_source=page` (the recall_ws fallback feeds a mixed
   stream that may include our own output). **Staleness rule**: a VAD that hasn't seen frames for
   1.5 s (stalled feed, capture failure, no audio yet) surrenders authority back to events — a
   frozen "quiet" must not open the floor over talking humans, and a frozen "voice" must not mute
   the bot forever. `VAD_GATE=false` reverts to events-only gating entirely.
2. *Recall's `speech_on`/`speech_off`* — speaker attribution (dashboard "who is speaking"; the
   bot's own name is filtered) and the gating/watchdog signal whenever the VAD is stale or
   disabled. Reliable only with multiple participants — and even then only between utterances,
   not within them (events stay "on" through a speaker's pauses).

Gate mechanics:

- **grace** (300 ms): silence on BOTH signals required before the agent may begin speaking
  (on the VAD side this stacks on its 300 ms hangover — ~600 ms after the last voiced frame,
  still inside Gemini's own 700 ms end-of-turn silence window, so it adds no user-visible delay).
- **stale-drop** (2.5 s): if a response has been gated this long, the human has moved on; the
  buffered answer AND the still-streaming remainder of that generation are discarded
  (`_discarding` until the turn boundary). Gemini's own VAD normally also cancels the stale
  generation (its input never stopped streaming) — asserted in
  `test_interrupted_while_gate_held_flushes_before_release` — but we do not rely on it.
- **max-hold** (20 s): a stuck `speech_on` (lost event, participant left mid-word) is pruned so
  the agent can't be muted forever; participant-leave also releases the floor.
- **hard-stop watchdog** (400 ms): if a human has been talking over playing agent audio and no
  `interrupted` arrived, we force-drop ourselves. Signal policy converged to: **a live VAD's word
  is final; events back-stop only when the VAD is stale** (pruned first). We shipped the union of
  both signals first — priced as "a false drop is recoverable" — and multi-party testing
  falsified it: speech events stay on through a speaker's between-sentence pauses, so event-only
  kills (18 of 23 in one 5-minute run, mic provably quiet) shredded agent speech into 50–250ms
  blips. Residual soft-speaker risk is carried by Gemini's own barge-in VAD, which hears the same
  mix, plus a configurable RMS threshold. Force-drops are sticky (a re-fire can't clear the
  discard flag and replay a killed generation's tail) and count as handled overlaps.
- **drop coherence** (live finding): a dropped response exists in the model's history but was
  never heard — left alone, the model and the humans diverge ("it asked a question; they heard
  silence"; the greeting was the worst case, since people talk right when admitting a bot). So
  the greeting waits for a quiet floor before being requested, and any force-drop triggers a
  rate-limited note — once the floor frees and no newer generation superseded it — telling the
  model participants never heard it, so it restates naturally.

Epochs make all of this race-free: every chunk carries its generation's epoch; a flush advances
the epoch; Pacer and page discard stale-epoch chunks unconditionally.

## 5. Receive-loop / pacer decoupling (core invariant)

The Gemini receive loop is the only channel delivering `interrupted` and tool calls, so it must
never block on audio delivery. Structure: receive loop does O(1) synchronous work per message
(append to an unbounded, epoch-flushable `TurnAudioBuffer`); a separate Pacer task owns pacing and
gate waits. The unit tests assert the loop processes `interrupted` and tool calls in <50 ms while
the buffer holds a minute of audio. A subtle consequence found in integration testing: the SDK's
`session.receive()` generator completes at *every turn boundary* — treating that as a disconnect
closes a healthy session; the loop must re-enter it forever.

## 6. Session survival for a full-length meeting

Gemini Live sessions die at 15 minutes (audio-only) and connections recycle ~every 10 minutes.
Both mitigations are on:

- `context_window_compression` (sliding window, trigger 100k → target 64k on a 131k-context
  model): removes the 15-min cap and bounds per-turn re-billing (Live re-processes accumulated
  context each turn). **Sizing matters more than the docs suggest**: their 25k/8k cost example,
  tried first, evicted live conversation every ~2 minutes of audio (~25 tokens/sec/direction) —
  observed in testing as mid-call amnesia: the model re-greeted, re-asked skipped questions, and
  once answered its own question in the customer's voice. At 100k/64k compression is a >60-min
  safety net instead of an active participant.
- **Re-anchoring is deterministic, not detection-based** — and the path to that design is a
  useful story. First attempt: detect eviction via drops in `usage_metadata.total_token_count`.
  Live data falsified it twice: interrupted turns report partial-turn usage (single-dip false
  positives), and a third meeting showed totals swing with turn size generally (4.2k → 8.9k →
  4.9k within one minute) — it's per-turn accounting, not a context-size proxy. False positives
  are costly: a text nudge is a user-turn, so the model *responds* to it (observed: unprompted
  re-confirmation of answered questions). Final design: no detection. The oversized window means
  eviction can't occur in a meeting-length session, and the runtime sends a periodic
  context-refresher (default every 8 min, only at a quiet moment) built from
  `DiscoveryTracker.progress_note()` — the server-side state is authoritative and survives
  anything. Persistent usage dips are still logged (`gemini.usage_dip`) for observability only.
  The matching guardrail lives in the system instruction ("greet at most once; when unsure,
  assume you already did").
- `session_resumption`: the rolling handle is kept; on `GoAway` (60 s warning) or socket close the
  run-loop reconnects with it. If a handle goes stale after repeated failures it's dropped and a
  fresh session is re-seeded with `DiscoveryTracker.progress_note()` — a compact "answered /
  still open" state summary — plus a "don't re-greet" instruction, so a worst-case reconnect
  costs conversational tone, not discovery state (the dashboard state lives server-side and
  survives regardless).
- No video frames are ever sent (video halves the session budget and correlates with 1011 drops).

## 7. Web search: combined tools, with a justified fallback

The Gemini API docs say `google_search` + `function_declarations` combine in one Live session;
Vertex docs claim search tools can't mix with non-search tools. **Verified empirically on this
key and model: combined works** (`gladiator doctor` re-checks on every setup; an integration test
covers it). So search runs natively inside the Live session.

If a future model/API change rejects the combination (`SEARCH_MODE=function`): we expose our own
`web_search` function declaration whose *backend* is one grounded `generateContent` call.
Justification against the "reasoning handled by Gemini Live, no other LLM" constraint: every
conversational decision — when to search, what to search, how to use the result, what to say —
stays in the Live session. The grounded call is a retrieval service invoked as a tool
implementation, architecturally identical to calling a search API; it performs no reasoning about
the conversation and sees none of it (it receives only the query string). Deferred-tool plumbing
keeps the receive loop unblocked while the search runs, and honors `tool_call_cancellation` if
the requesting turn is interrupted mid-search.

## 8. Latency: measured, not asserted

Per turn, the dashboard waterfall decomposes user-perceived wait:

| segment | boundary | typical |
|---|---|---|
| detect+think | human speech end (EnergyVAD last-voiced frame) → first model audio byte | ~0.8–1.2 s (bench: p50 ≈ 0.9 s) |
| gate+serve | first audio byte → first chunk sent (includes any floor-gate hold) | ~0 when floor is free |
| transit+play | first chunk sent → page schedules playback (NTP-style clock sync over the page ws, RTT-tracked) | ~0.1–0.3 s |

Knobs and their measured cost live in config: `vad_silence_duration_ms` (700 ms of the detect
segment is deliberate — the price of never splitting a human's sentence), `page_buffer_target_ms`
(smoothness ↔ barge-in cutoff), and the **hybrid-VAD flag** (off by default): on Recall
`speech_off` with no humans left speaking, send `audio_stream_end` to skip the server's silence
wait — implemented and event-logged so its effect is measurable in the waterfall, intentionally
not tuned further.

`gladiator bench` isolates the model's contribution offline; the waterfall attributes the rest.

## 9. Echo analysis (why the agent doesn't fight itself)

1. *Bot hearing its own output via Recall:* doesn't happen — bot audio is excluded from
   recordings/streams unless `include_bot_in_recording.audio=true` (left false).
2. *Bot hearing itself acoustically:* possible only via a participant on speakers with weak AEC.
   Two defenses: `START_SENSITIVITY_LOW` (fewer false barge-ins) and the FloorGate keying on
   *named human* speech events rather than any VAD on the mixed signal. The bot's own
   `speech_on` events (it is a participant) are filtered by name.
3. *False barge-in from echo blips:* a barge-in only kills queued audio; a phantom `interrupted`
   costs at most the current sentence, and the hard-stop watchdog never fires without sustained
   attributed human speech.

## 10. Security

- Every tunnel-exposed surface (agent page, all websockets, dashboard, snapshot API) requires the
  per-run random session token; websockets are rejected at handshake otherwise.
- The Gemini key exists only in the server process. The page receives audio and state, nothing else.
- Recall key is used server→Recall only. `.env` is git-ignored; `.env.example` documents shape.

## 11. Hidden-test hardening (beyond the visible cases)

- Greets only after `in_call_recording` AND live audio capture — never the waiting room.
- Re-answers update the dashboard idempotently with revision history; answers arriving out of
  order or several-in-one-utterance are handled (`record_answer` is per-question, invocable
  anytime); skip refuses to erase an existing answer; "asked" never regresses an "answered".
- Interruption mid-tool-call: `tool_call_cancellation` honored; cancelled deferred searches are
  never answered.
- "Glad, wrap up" / "you can leave": wrap-up summary → `set_meeting_phase` → `leave_meeting`
  (tool responds instantly; Recall REST leave runs as a background task after a 3 s goodbye grace).
- Long silence: gentle re-engagement nudge (once per lull, only mid-discovery).
- Session > 15 min: compression + resumption (§6). Tunnel/websocket drops: page and dashboard
  sockets auto-reconnect; Recall retries 30×; Gemini run-loop reconnects with backoff.
- Everything above is observable in the event log and reproducible via `gladiator replay`.

## 12. Known limitations / next steps

- Speaker attribution in the transcript is heuristic (input transcription isn't diarized; we tag
  by the most recent `speech_on`). Recall's per-participant streams could make this exact.
- Google Meet "adaptive audio" merges room participants into one stream — a conference room looks
  like one speaker to the gate (documented Recall/Meet behavior).
- The stale-drop threshold (2.5 s) is a product decision disguised as a constant: drop too early
  and short interjections kill good answers; too late and stale answers play. It's configurable
  and every drop is event-logged for tuning.
- `getUserMedia` constraint behavior inside Recall's Chrome (AEC off honored?) is verified at the
  first live rehearsal; the `recall_ws` fallback exists precisely for this.
