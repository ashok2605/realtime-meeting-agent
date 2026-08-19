"""User-mandated invariant: the Gemini receive loop stays responsive while the
pacer is saturated. `interrupted` and tool calls must be processed instantly
even with minutes of undelivered audio buffered."""

import asyncio
import time

from google.genai import types

from gladiator.audio.bridge import AudioBridge
from gladiator.config import Settings
from gladiator.core.discovery import DiscoveryTracker
from gladiator.core.events import EventBus
from gladiator.core.latency import LatencyTracker
from gladiator.gemini.session import GeminiSession
from gladiator.gemini.tools import ToolDispatcher


class StubSession:
    """Stands in for the google-genai AsyncSession in handle_message."""

    def __init__(self):
        self.tool_responses = []

    async def send_tool_response(self, *, function_responses):
        self.tool_responses.append(function_responses)


def make_stack():
    settings = Settings(gemini_api_key="test", recall_api_key="test")
    bus = EventBus()
    latency = LatencyTracker(bus)
    bridge = AudioBridge(bus, latency, chunk_ms=40)
    tracker = DiscoveryTracker(bus)
    dispatcher = ToolDispatcher(tracker, bus, request_leave=lambda reason: None)
    session = GeminiSession(settings, bus, bridge, dispatcher, tracker)
    return bus, bridge, tracker, session


def audio_msg(seconds: float) -> types.LiveServerMessage:
    pcm = b"\x00\x01" * int(24000 * seconds)
    return types.LiveServerMessage(server_content=types.LiveServerContent(
        model_turn=types.Content(parts=[types.Part(
            inline_data=types.Blob(data=pcm, mime_type="audio/pcm;rate=24000"))])))


INTERRUPTED = types.LiveServerMessage(
    server_content=types.LiveServerContent(interrupted=True))


async def test_interrupted_processed_instantly_with_saturated_backlog():
    _, bridge, _, session = make_stack()
    stub = StubSession()

    flushes: list[int] = []
    async def note_flush(epoch: int) -> None:
        flushes.append(epoch)
    bridge.notify_page_flush = note_flush

    # No pacer running at all = maximal saturation: nothing drains the buffer.
    for _ in range(30):
        await session.handle_message(audio_msg(2.0), stub)  # 60s of audio backlog
    assert bridge.buffer.buffered_ms > 55_000
    epoch_before = bridge.buffer.epoch

    t0 = time.monotonic()
    await session.handle_message(INTERRUPTED, stub)
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert elapsed_ms < 50, f"interrupted took {elapsed_ms:.1f}ms with a full buffer"
    assert bridge.buffer.buffered_ms == 0, "backlog must be flushed on barge-in"
    assert bridge.buffer.epoch == epoch_before + 1, "epoch must advance so stale chunks die"
    await asyncio.sleep(0)  # let the page-flush task run
    assert flushes == [bridge.buffer.epoch], "page must be told to drop its buffer"


async def test_tool_call_processed_while_backlog_pending():
    _, bridge, tracker, session = make_stack()
    stub = StubSession()
    for _ in range(10):
        await session.handle_message(audio_msg(2.0), stub)

    tool_msg = types.LiveServerMessage(tool_call=types.LiveServerToolCall(
        function_calls=[types.FunctionCall(
            id="call-1", name="record_answer",
            args={"question_id": "current-systems",
                  "answer_summary": "They run Siebel today."})]))
    t0 = time.monotonic()
    await session.handle_message(tool_msg, stub)
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert elapsed_ms < 50
    assert tracker.questions["current-systems"].status == "answered"
    assert stub.tool_responses, "tool response must go back to Gemini immediately"


async def test_multipart_event_processes_all_parts():
    """3.1 packs audio + transcript + flags into one event; no elif chains."""
    bus, bridge, _, session = make_stack()
    stub = StubSession()
    msg = types.LiveServerMessage(server_content=types.LiveServerContent(
        model_turn=types.Content(parts=[types.Part(
            inline_data=types.Blob(data=b"\x00\x01" * 960, mime_type="audio/pcm"))]),
        output_transcription=types.Transcription(text="Hello there."),
        turn_complete=True,
    ))
    await session.handle_message(msg, stub)
    types_seen = {e["type"] for e in bus.ring}
    assert "transcript.agent" in types_seen
    assert "turn.complete" in types_seen
    assert bridge.buffer.buffered_ms > 0  # audio landed too (finish_turn keeps partial)
