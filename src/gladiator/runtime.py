"""Runtime: wires every component together and owns the background tasks.

Data flow (page-capture path, the default):
  page mic frames --/ws/agent--> feed_frame -> inbound_sender -> Gemini
  Gemini audio -> receive loop -> TurnAudioBuffer -> Pacer (FloorGate) -> /ws/agent -> page
  Recall speech events --/ws/recall--> SpeechState (gate ground truth)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import struct
import time
from datetime import datetime
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from google import genai

from .audio.bridge import AudioBridge, Pacer
from .config import Settings
from .core.discovery import DiscoveryTracker
from .core.events import EventBus
from .core.latency import LatencyTracker
from .core.turn_gate import FloorGate, SpeechState
from .gemini.prompts import DROPPED_RESPONSE_NUDGE, SILENCE_NUDGE, context_refresher
from .gemini.session import GeminiSession
from .gemini.tools import ToolDispatcher
from .recall.client import RecallClient
from .recall.events import RecallEventRouter

ADMITTED_STATUS = "in_call_recording"
TERMINAL_STATUSES = {"call_ended", "done", "fatal"}


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        run_dir = settings.runs_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_dir = run_dir
        self.bus = EventBus(log_path=run_dir / "events.jsonl")
        self.latency = LatencyTracker(self.bus)
        self.tracker = DiscoveryTracker(self.bus)
        self.speech = SpeechState(bot_name=settings.bot_name)
        self.bridge = AudioBridge(self.bus, self.latency, chunk_ms=settings.outbound_chunk_ms)
        # EnergyVAD is the gate's primary signal (Recall speech events barely
        # fire in small meetings — see DESIGN.md §4); vad_gate=false reverts to
        # events-only if a room's speakers ever leak agent audio into the mic.
        self.gate = FloorGate(self.speech, self.bus,
                              grace_ms=settings.gate_grace_ms,
                              stale_drop_ms=settings.gate_stale_drop_ms,
                              max_hold_ms=settings.gate_max_hold_ms,
                              vad=self.bridge.vad if settings.vad_gate else None)
        self.dispatcher = ToolDispatcher(
            self.tracker, self.bus,
            request_leave=self._request_leave,
            web_search=self._grounded_search if settings.search_mode == "function" else None,
        )
        self.session = GeminiSession(settings, self.bus, self.bridge,
                                     self.dispatcher, self.tracker)
        self.recall = RecallClient(settings, self.bus)
        self.router = RecallEventRouter(
            self.speech, self.bus, self.bridge,
            on_human_speech_off=self._on_all_humans_quiet if settings.hybrid_vad else None,
            on_roster_change=self._on_roster_change)
        self.pacer = Pacer(self.bridge, self.gate, self.bus, self.latency,
                           chunk_ms=settings.outbound_chunk_ms,
                           prime_ms=settings.page_buffer_target_ms,
                           send=self._send_audio_to_page)

        # wiring
        self.bridge.send_audio_to_gemini = self.session.send_audio
        self.bridge.notify_page_flush = self._send_page_flush
        self.bridge.on_state = self.set_face
        self.bridge.vad.on_speech_end = self._on_speech_end
        self.bridge.on_forced_drop = self._on_forced_drop

        # state
        self.bot_id: str | None = None
        self.bot_status: str = "not_started"
        self.meeting_url: str | None = None
        self.face = "idle"
        self.page_ws: WebSocket | None = None
        self.page_capture_ok = False
        self.dashboard_ws: set[WebSocket] = set()
        self._admitted = False
        self._last_nudge_mono = time.monotonic()
        self._greet_task: asyncio.Task | None = None
        self._last_drop_note_mono = 0.0
        self._tasks: list[asyncio.Task] = []
        self._genai_client: genai.Client | None = None

    # ------------------------------------------------------------------ auth
    def check_token(self, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(token, self.settings.session_token)

    # ------------------------------------------------------------ lifecycle
    async def start(self, meeting_url: str | None) -> None:
        self._tasks = [
            asyncio.create_task(self.session.run(), name="gemini"),
            asyncio.create_task(self.pacer.run(), name="pacer"),
            asyncio.create_task(self.bridge.inbound_sender(), name="inbound"),
            asyncio.create_task(self.bridge.stats_reporter(), name="stats"),
            asyncio.create_task(self._hard_stop_watchdog(), name="hardstop"),
            asyncio.create_task(self._silence_nudger(), name="nudger"),
            asyncio.create_task(self._periodic_reanchor(), name="reanchor"),
        ]
        self.bus.emit("runtime.started", audio_source=self.settings.audio_source,
                      model=self.settings.gemini_model, run_dir=str(self.run_dir))
        if meeting_url:
            self.meeting_url = meeting_url
            bot = await self.recall.create_bot(meeting_url)
            self.bot_id = bot.get("id")
            info = json.dumps({"bot_id": self.bot_id, "meeting_url": meeting_url,
                               "run_dir": str(self.run_dir)})
            (self.run_dir / "bot.json").write_text(info)
            (self.settings.runs_dir / "last_bot.json").write_text(info)
            self._tasks.append(asyncio.create_task(self._poll_bot_status(), name="botpoll"))

    async def shutdown(self) -> None:
        if self.bot_id:
            try:
                await self.recall.leave(self.bot_id)
            except Exception:
                pass
        self.session.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.recall.close()
        self.bus.emit("runtime.stopped")
        self.bus.close()

    # ------------------------------------------------------- page websocket
    async def handle_page_ws(self, ws: WebSocket) -> None:
        await ws.accept()
        self.page_ws = ws
        self.bus.emit("page.connected")
        await self._page_send_json({"type": "face", "state": self.face})
        try:
            while True:
                message = await ws.receive()
                if message.get("bytes") is not None:
                    data: bytes = message["bytes"]
                    if len(data) > 8:
                        # [f64 LE client_ts_ms][int16 pcm @16k]
                        self.bridge.feed_frame(data[8:])
                elif message.get("text") is not None:
                    await self._handle_page_text(json.loads(message["text"]))
                elif message.get("type") == "websocket.disconnect":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if self.page_ws is ws:
                self.page_ws = None
                self.page_capture_ok = False
            self.bus.emit("page.disconnected")

    async def _handle_page_text(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "ping":
            await self._page_send_json({"type": "pong", "t0": msg.get("t0"),
                                        "t1": time.time() * 1000})
        elif kind == "clock":
            self.latency.clock_sync.update(float(msg.get("offset_ms", 0)),
                                           float(msg.get("rtt_ms", 0)))
        elif kind == "play_ack":
            self.latency.mark_page_play(int(msg.get("epoch", -1)),
                                        float(msg.get("client_play_ts_ms", 0)))
        elif kind == "capture_state":
            self.page_capture_ok = bool(msg.get("ok"))
            self.bus.emit("page.capture_state", ok=self.page_capture_ok,
                          detail=msg.get("detail", ""))
            await self._maybe_greet()
        elif kind == "hello":
            self.bus.emit("page.hello", user_agent=msg.get("user_agent", "")[:120])

    async def _send_audio_to_page(self, epoch: int, seq: int, pcm: bytes) -> bool:
        ws = self.page_ws
        if ws is None:
            return False
        try:
            await ws.send_bytes(struct.pack("<II", epoch, seq) + pcm)
            return True
        except Exception:
            return False

    async def _send_page_flush(self, epoch: int) -> None:
        await self._page_send_json({"type": "flush", "epoch": epoch})

    async def _page_send_json(self, obj: dict[str, Any]) -> None:
        ws = self.page_ws
        if ws is not None:
            try:
                await ws.send_text(json.dumps(obj))
            except Exception:
                pass

    # ----------------------------------------------------- recall websocket
    async def handle_recall_ws(self, ws: WebSocket) -> None:
        await ws.accept()
        self.bus.emit("recall.ws_connected")
        try:
            while True:
                raw = await ws.receive_text()
                self.router.handle(json.loads(raw))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self.bus.emit("recall.ws_disconnected")

    async def _poll_bot_status(self) -> None:
        while self.bot_id:
            try:
                bot = await self.recall.get_bot(self.bot_id)
                changes = bot.get("status_changes") or []
                code = changes[-1].get("code") if changes else "unknown"
                if code != self.bot_status:
                    self.bot_status = code
                    self.bus.emit("recall.bot_status", status=code,
                                  sub_code=changes[-1].get("sub_code") if changes else None)
                    if code == ADMITTED_STATUS:
                        self._admitted = True
                        self._last_nudge_mono = time.monotonic()
                        await self._maybe_greet()
                    if code in TERMINAL_STATUSES:
                        self.set_face("idle")
                        return
            except Exception as exc:
                self.bus.emit("recall.poll_error", error=str(exc)[:200])
            await asyncio.sleep(4)

    async def _maybe_greet(self) -> None:
        # Never greet the waiting room: require admission AND a live audio path.
        audio_ready = (self.page_capture_ok
                       if self.settings.audio_source == "page" else True)
        if not (self._admitted and audio_ready) or self.session.greeted:
            return
        self.set_face("listening")
        if self._greet_task is None or self._greet_task.done():
            self._greet_task = asyncio.create_task(self._greet_when_quiet())

    async def _greet_when_quiet(self, max_wait_s: float = 20.0) -> None:
        """People usually ARE talking right when they admit the bot; a greeting
        requested then just gets stale-dropped and the bot seems mute (observed
        live). Wait for a quiet floor so the greeting actually plays."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < max_wait_s and not self.gate.is_open():
            await asyncio.sleep(0.1)
        waited = round((time.monotonic() - t0) * 1000, 1)
        if waited > 200:
            self.bus.emit("runtime.greeting_waited_for_floor", waited_ms=waited)
        await self.session.request_greeting()

    def _on_forced_drop(self, reason: str) -> None:
        asyncio.get_running_loop().create_task(self._notify_dropped_response())

    async def _notify_dropped_response(self, max_wait_s: float = 30.0) -> None:
        """A stale/hard drop means the humans never heard what the model said —
        but the model doesn't know that. Once the floor frees up, tell it, so it
        can restate (unless it already moved on with a newer response)."""
        epoch_at_drop = self.bridge.buffer.epoch
        t0 = time.monotonic()
        while time.monotonic() - t0 < max_wait_s:
            if self.bridge.buffer.epoch != epoch_at_drop or self.bridge.turn_open:
                return  # a newer generation superseded the dropped one
            if self.gate.is_open() and not self.pacer.agent_playing:
                break
            await asyncio.sleep(0.1)
        else:
            return
        if time.monotonic() - self._last_drop_note_mono < 15:
            return
        self._last_drop_note_mono = time.monotonic()
        self.bus.emit("runtime.dropped_response_note")
        await self.session.send_text(DROPPED_RESPONSE_NUDGE)

    # ------------------------------------------------------------- controls
    async def control(self, action: str, **kwargs: Any) -> dict[str, Any]:
        self.bus.emit("control", action=action, **kwargs)
        if action == "leave":
            self._request_leave("dashboard control")
            return {"status": "ok"}
        if action == "mute":
            self.bridge.muted = True
            return {"status": "ok", "muted": True}
        if action == "unmute":
            self.bridge.muted = False
            return {"status": "ok", "muted": False}
        if action == "greet":
            await self.session.request_greeting()
            return {"status": "ok"}
        if action == "skip_question":
            qid = kwargs.get("question_id", "")
            result = self.tracker.mark_skipped(qid, "skipped from dashboard")
            await self.session.send_text(
                f"[system note] The operator skipped question '{qid}' from the dashboard; "
                "don't ask it.")
            return result
        if action == "nudge":
            await self.session.send_text(SILENCE_NUDGE)
            return {"status": "ok"}
        return {"status": "error", "detail": f"unknown action '{action}'"}

    def _request_leave(self, reason: str) -> None:
        """Tool-handler safe: schedules the REST call, returns immediately."""
        self.bus.emit("runtime.leave_scheduled", reason=reason)

        async def _leave() -> None:
            if not self.bot_id:
                return
            # Don't yank the bot mid-goodbye: wait for the farewell audio to
            # start (it's generated in the same turn as the tool call), then
            # for it to fully drain — bounded so a leave can never hang.
            t0 = time.monotonic()
            while time.monotonic() - t0 < 4.0:
                if self.pacer.agent_playing or self.bridge.buffer.buffered_ms > 0:
                    break
                await asyncio.sleep(0.1)
            while time.monotonic() - t0 < 20.0:
                if not self.pacer.agent_playing and self.bridge.buffer.buffered_ms == 0:
                    break
                await asyncio.sleep(0.2)
            self.bus.emit("runtime.leave_playout_done",
                          waited_ms=round((time.monotonic() - t0) * 1000, 1))
            try:
                await self.recall.leave(self.bot_id)
            except Exception as exc:
                self.bus.emit("recall.leave_error", error=str(exc)[:200])

        asyncio.get_running_loop().create_task(_leave())

    # ----------------------------------------------------------- background
    def _hard_stop_due(self) -> float | None:
        """Overlap in ms if a human has been talking over playing agent audio
        for >= gate_hard_stop_ms with no interruption handled yet, else None.
        VAD-authoritative when vad_gate is on (Recall speech events are
        unreliable in small meetings — a stuck speech_on must not be able to
        force-drop healthy agent speech); events-only otherwise."""
        if not self.pacer.agent_playing:
            return None
        hard_stop_s = self.settings.gate_hard_stop_ms / 1000
        vad = self.bridge.vad
        if self.settings.vad_gate:
            since_candidates = ([vad.voice_since]
                                if vad.in_voice and vad.voice_since is not None else [])
        else:
            since_candidates = [s.since_mono for s in self.speech.speaking.values()]
        if not since_candidates:
            return None
        earliest = min(since_candidates)
        now = time.monotonic()
        if now - earliest >= hard_stop_s and self.bridge.last_interrupt_mono < earliest:
            return (now - earliest) * 1000
        return None

    async def _hard_stop_watchdog(self) -> None:
        """Belt-and-suspenders for 'never talk over a human': if a human has been
        speaking over agent audio for gate_hard_stop_ms and Gemini hasn't sent
        `interrupted`, force-drop the response ourselves."""
        while True:
            await asyncio.sleep(0.1)
            overlap_ms = self._hard_stop_due()
            if overlap_ms is not None:
                self.bus.emit("gate.hard_stop",
                              speaking=self.speech.snapshot()["speaking"],
                              vad_voice=self.bridge.vad.in_voice,
                              overlap_ms=round(overlap_ms, 1))
                self.bridge.force_drop("hard_stop")

    async def _silence_nudger(self, silence_s: float = 45.0) -> None:
        """If the room goes quiet mid-discovery, gently re-engage (once per lull)."""
        while True:
            await asyncio.sleep(5)
            if not self._admitted or self.tracker.phase in ("wrapup", "done"):
                continue
            if self.speech.any_human_speaking() or self.pacer.agent_playing:
                continue
            last_activity = max(self.speech.last_speech_off_mono,
                                self.pacer._last_send_mono,
                                self._last_nudge_mono)
            if time.monotonic() - last_activity >= silence_s:
                self._last_nudge_mono = time.monotonic()
                self.bus.emit("runtime.silence_nudge")
                await self.session.send_text(SILENCE_NUDGE)

    async def _periodic_reanchor(self) -> None:
        """Deterministic context maintenance: at a regular cadence, re-tell the
        model the authoritative discovery state (answered/skipped/open, already
        greeted). Delivered at a quiet moment so it doesn't collide with anyone
        speaking; the note itself instructs the model not to acknowledge it."""
        interval = self.settings.reanchor_interval_s
        if interval <= 0:
            return
        while True:
            await asyncio.sleep(interval)
            if not (self._admitted and self.session.greeted):
                continue
            if self.tracker.phase == "done":
                continue
            t0 = time.monotonic()
            while time.monotonic() - t0 < 30:
                if self.gate.is_open() and not self.pacer.agent_playing:
                    break
                await asyncio.sleep(0.5)
            else:
                continue  # never found a quiet moment; try next cycle
            self.bus.emit("runtime.periodic_reanchor")
            await self.session.send_text(context_refresher(self.tracker.progress_note()))

    def _on_speech_end(self, last_voice_mono: float) -> None:
        self.latency.mark_speech_end(last_voice_mono)
        if not self.pacer.agent_playing:
            self.set_face("thinking")

    def _on_all_humans_quiet(self) -> None:
        """Hybrid-VAD hook (ships OFF): skip Gemini's server-side silence wait."""
        asyncio.get_running_loop().create_task(self.session.send_audio_stream_end())

    def _on_roster_change(self, kind: str, name: str) -> None:
        """Give the model explicit multi-party awareness: who joined or left.
        Suppressed before the greeting (the bot itself joining, the host already
        present) so the opening isn't derailed."""
        if name.strip().lower() == self.settings.bot_name.strip().lower():
            return
        if not self._admitted or not self.session.greeted:
            return
        note = (f"[system note] {name} just joined the meeting. Acknowledge them briefly "
                "when natural and continue the discovery — do not start over."
                if kind == "join" else
                f"[system note] {name} left the meeting. Continue with whoever remains; "
                "do not comment unless someone brings it up.")
        asyncio.get_running_loop().create_task(self.session.send_text(note))

    # ------------------------------------------------------------- fallback
    async def _grounded_search(self, query: str) -> dict[str, Any]:
        """web_search tool backend for search_mode="function" (documented fallback:
        a grounded generateContent call is a retrieval service; all conversational
        reasoning stays in the Live session)."""
        if self._genai_client is None:
            self._genai_client = genai.Client(api_key=self.settings.gemini_api_key)
        try:
            resp = await self._genai_client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"Answer concisely with sources: {query}",
                config={"tools": [{"google_search": {}}]},
            )
            return {"status": "ok", "answer": (resp.text or "")[:2000]}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)[:300]}

    # ------------------------------------------------------------ dashboard
    def set_face(self, state: str) -> None:
        if state == self.face:
            return
        self.face = state
        self.bus.emit("face.state", state=state)
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(self._page_send_json({"type": "face", "state": state}))

    def snapshot(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "bot_status": self.bot_status,
            "meeting_url": self.meeting_url,
            "face": self.face,
            "muted": self.bridge.muted,
            "page_connected": self.page_ws is not None,
            "page_capture_ok": self.page_capture_ok,
            "speaking": self.speech.snapshot()["speaking"],
            "discovery": self.tracker.snapshot(),
            "latency": self.latency.stats(),
            "audio_source": self.settings.audio_source,
            "model": self.settings.gemini_model,
            "gemini_reconnects": self.session.reconnects,
            "log_path": str(self.bus.log_path) if self.bus.log_path else None,
        }

    async def handle_dashboard_ws(self, ws: WebSocket) -> None:
        await ws.accept()
        self.dashboard_ws.add(ws)
        queue = self.bus.subscribe()
        try:
            await ws.send_text(json.dumps({"type": "hello", "snapshot": self.snapshot()},
                                          default=str))
            backlog = list(self.bus.ring)[-2000:]
            await ws.send_text(json.dumps({"type": "backlog", "events": backlog},
                                          default=str))

            async def pump_events() -> None:
                while True:
                    event = await queue.get()
                    await ws.send_text(json.dumps({"type": "event", "event": event},
                                                  default=str))

            async def pump_controls() -> None:
                while True:
                    raw = await ws.receive_text()
                    msg = json.loads(raw)
                    if msg.get("type") == "control":
                        result = await self.control(msg.get("action", ""),
                                                    **(msg.get("args") or {}))
                        await ws.send_text(json.dumps({"type": "control_result",
                                                       "action": msg.get("action"),
                                                       "result": result}, default=str))

            pump = asyncio.create_task(pump_events())
            try:
                await pump_controls()
            finally:
                pump.cancel()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self.bus.unsubscribe(queue)
            self.dashboard_ws.discard(ws)
