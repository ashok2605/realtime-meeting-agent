"""Gemini Live session: connect/reconnect loop and the receive loop.

Invariant: the receive loop does only O(1) synchronous work per message —
it also carries `interrupted` and tool calls, so it never awaits the pacer,
gate, or page. Longevity = context compression (15-min cap) + resumption
handles (~10-min connection recycles) + a state note on reconnect.
"""

from __future__ import annotations

import asyncio

from google import genai
from google.genai import types
from websockets.exceptions import ConnectionClosed

from ..audio.bridge import AudioBridge
from ..config import Settings
from ..core.discovery import DiscoveryTracker
from ..core.events import EventBus
from .prompts import GREETING_NUDGE, reconnect_nudge, system_instruction
from .tools import ToolDispatcher, build_declarations


class GeminiSession:
    def __init__(self, settings: Settings, bus: EventBus, bridge: AudioBridge,
                 dispatcher: ToolDispatcher, tracker: DiscoveryTracker) -> None:
        self._settings = settings
        self._bus = bus
        self._bridge = bridge
        self._dispatcher = dispatcher
        self._tracker = tracker
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._session: genai.live.AsyncSession | None = None
        self._send_lock = asyncio.Lock()  # ws writes come from several tasks
        self._resumption_handle: str | None = None
        self._stop = False
        self._greeted = False
        self._greeting_pending = False
        self.reconnects = 0

    @property
    def greeted(self) -> bool:
        return self._greeted

    # ---- config -----------------------------------------------------------
    def _build_config(self) -> types.LiveConnectConfig:
        s = self._settings
        search_as_function = s.search_mode == "function"
        tools: list[dict] = []
        if not search_as_function:
            tools.append({"google_search": {}})
        tools.append({"function_declarations": build_declarations(self._tracker,
                                                                  search_as_function)})
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=types.Content(parts=[types.Part(
                text=system_instruction(self._tracker, search_as_function))]),
            tools=tools,
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=s.voice_name))),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    prefix_padding_ms=s.vad_prefix_padding_ms,
                    silence_duration_ms=s.vad_silence_duration_ms,
                )),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=s.compression_trigger_tokens,
                sliding_window=types.SlidingWindow(target_tokens=s.compression_target_tokens)),
            session_resumption=types.SessionResumptionConfig(handle=self._resumption_handle),
            thinking_config=types.ThinkingConfig(thinking_level="minimal"),
        )

    # ---- lifecycle ---------------------------------------------------------
    async def run(self) -> None:
        backoff = 0.5
        while not self._stop:
            try:
                async with self._client.aio.live.connect(
                        model=self._settings.gemini_model,
                        config=self._build_config()) as session:
                    self._session = session
                    backoff = 0.5
                    self._bus.emit("gemini.connected",
                                   model=self._settings.gemini_model,
                                   reconnects=self.reconnects,
                                   resumed=self._resumption_handle is not None)
                    if self.reconnects and self._greeted:
                        await self.send_text(reconnect_nudge(self._tracker.progress_note()))
                    elif self._greeting_pending:
                        await self._send_greeting()
                    await self._receive_loop(session)
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                # Normal for long meetings: connections recycle ~every 10 min
                # (GoAway usually precedes this). Reconnect with the handle.
                self._bus.emit("gemini.disconnected", reason=str(exc)[:120])
            except Exception as exc:
                # Resumption handles can go stale; drop after repeated failures
                # so we can at least start a fresh session (context re-seeded).
                self._bus.emit("gemini.error", kind=type(exc).__name__, error=str(exc)[:400])
                if backoff >= 4 and self._resumption_handle is not None:
                    self._bus.emit("gemini.resumption_handle_dropped")
                    self._resumption_handle = None
            finally:
                self._session = None
            if self._stop:
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    def stop(self) -> None:
        self._stop = True

    # ---- receive loop (responsiveness invariant lives here) ----------------
    async def _receive_loop(self, session: genai.live.AsyncSession) -> None:
        # receive() is a PER-TURN generator: it completes at each turn boundary,
        # so it must be re-entered forever. A real disconnect raises
        # ConnectionClosed out of this loop into run()'s reconnect logic.
        while True:
            async for msg in session.receive():
                await self.handle_message(msg, session)

    async def handle_message(self, msg: types.LiveServerMessage,
                             session: genai.live.AsyncSession) -> None:
        """One server message. Independent ifs — a 3.1 event can carry several
        parts (audio + transcript + turn flags) in a single message."""
        sc = msg.server_content
        if sc is not None:
            if sc.model_turn is not None:
                for part in sc.model_turn.parts or []:
                    if part.inline_data is not None and part.inline_data.data:
                        self._bridge.on_model_audio(part.inline_data.data)
            if sc.input_transcription is not None and sc.input_transcription.text:
                self._bus.emit("transcript.user", text=sc.input_transcription.text)
            if sc.output_transcription is not None and sc.output_transcription.text:
                self._bus.emit("transcript.agent", text=sc.output_transcription.text)
            if sc.interrupted:
                self._bus.emit("gemini.interrupted")
                self._bridge.on_interrupted()
            if sc.generation_complete:
                self._bridge.on_generation_complete()
            if sc.turn_complete:
                reason = getattr(sc, "turn_complete_reason", None)
                self._bridge.on_turn_complete(reason=str(reason) if reason else None)

        if msg.tool_call is not None and msg.tool_call.function_calls:
            responses: list[types.FunctionResponse] = []
            for fc in msg.tool_call.function_calls:
                immediate = self._dispatcher.handle(fc)
                if immediate is not None:
                    responses.append(types.FunctionResponse(
                        id=fc.id, name=fc.name, response=immediate))
                else:
                    # Deferred tool (web_search): never block the receive loop.
                    asyncio.get_running_loop().create_task(
                        self._finish_deferred(fc, session))
            if responses:
                await self._send_tool_responses(responses, session)

        if msg.tool_call_cancellation is not None:
            ids = list(msg.tool_call_cancellation.ids or [])
            self._dispatcher.cancel(ids)

        if msg.session_resumption_update is not None:
            update = msg.session_resumption_update
            if update.resumable and update.new_handle:
                self._resumption_handle = update.new_handle
                self._bus.emit("gemini.resumption_handle", handle_prefix=update.new_handle[:12])

        if msg.go_away is not None:
            self._bus.emit("gemini.go_away", time_left=str(msg.go_away.time_left))

        if msg.usage_metadata is not None:
            um = msg.usage_metadata
            # total_token_count is per-turn accounting, NOT a context-size
            # proxy — never attach online logic to drops in it (DESIGN.md §6).
            self._bus.emit("gemini.usage", total_tokens=um.total_token_count)

    async def _finish_deferred(self, fc: types.FunctionCall,
                               session: genai.live.AsyncSession) -> None:
        result = await self._dispatcher.run_deferred(fc)
        if self._dispatcher.is_cancelled(fc.id):
            return  # the turn that requested this was interrupted; don't answer it
        if session is not self._session:
            return  # connection was recycled while searching; the call died with it
        await self._send_tool_responses([types.FunctionResponse(
            id=fc.id, name=fc.name, response=result)], session)

    async def _send_tool_responses(self, responses: list[types.FunctionResponse],
                                   session: genai.live.AsyncSession) -> None:
        # Respond on the session that delivered the call — never a reconnected one.
        try:
            async with self._send_lock:
                await session.send_tool_response(function_responses=responses)
        except ConnectionClosed:
            self._bus.emit("gemini.send_dropped", kind="tool_response")

    # ---- sends (all tolerate a mid-reconnect gap; run() restores the session)

    async def send_audio(self, pcm_16k: bytes) -> None:
        session = self._session
        if session is None:
            return
        try:
            async with self._send_lock:
                await session.send_realtime_input(
                    audio=types.Blob(data=pcm_16k, mime_type="audio/pcm;rate=16000"))
        except ConnectionClosed:
            pass  # audio frames are ephemeral; reconnect is in flight

    async def send_audio_stream_end(self) -> None:
        """Hybrid-VAD knob: skip the server's silence wait after Recall speech_off."""
        session = self._session
        if session is None:
            return
        try:
            async with self._send_lock:
                await session.send_realtime_input(audio_stream_end=True)
            self._bus.emit("gemini.audio_stream_end")
        except ConnectionClosed:
            pass

    async def send_text(self, text: str) -> bool:
        """True only if the note reached the model — callers log on success,
        so the event log can't claim nudges a reconnect gap swallowed."""
        session = self._session
        if session is None:
            self._bus.emit("gemini.send_dropped", kind="text", reason="no_session")
            return False
        try:
            async with self._send_lock:
                await session.send_realtime_input(text=text)
        except ConnectionClosed:
            self._bus.emit("gemini.send_dropped", kind="text", reason="closed")
            return False
        # A text nudge, not user speech, causes the next turn: fix attribution.
        self._bridge.clear_speech_anchor()
        self._bus.emit("gemini.nudge", text=text[:120])
        return True

    # ---- greeting ----------------------------------------------------------
    async def request_greeting(self) -> None:
        """Called by the runtime once the bot is admitted AND audio is flowing."""
        if self._greeted:
            return
        if self._session is None:
            self._greeting_pending = True
            return
        await self._send_greeting()

    async def _send_greeting(self) -> None:
        self._greeted = True
        self._greeting_pending = False
        await self.send_text(GREETING_NUDGE)
        self._bus.emit("gemini.greeting_sent")
