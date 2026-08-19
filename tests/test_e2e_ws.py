"""Whole-system integration test (RUN_INTEGRATION=1): a synthetic Output Media
page talks to the REAL server over the REAL websocket protocol, with the REAL
Gemini Live session behind it.

This covers everything a live meeting exercises except Recall itself:
  page ws binary protocol (both directions) -> runtime -> bridge -> Gemini
  -> pacer -> page, plus clock sync, play_ack, and the latency waterfall.
"""

import asyncio
import json
import struct
import time
import wave
from pathlib import Path

import pytest
import uvicorn
import websockets

from gladiator.config import Settings
from gladiator.runtime import Runtime
from gladiator.server import build_app

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures"
PORT = 8321


class FakePage:
    """Speaks the agent page's exact wire protocol."""

    def __init__(self, ws):
        self.ws = ws
        self.received_chunks: list[tuple[int, int, bytes]] = []
        self.acked_epochs: set[int] = set()
        self.flushes: list[int] = []

    async def pump(self) -> None:
        async for msg in self.ws:
            if isinstance(msg, bytes):
                epoch, seq = struct.unpack_from("<II", msg)
                self.received_chunks.append((epoch, seq, msg[8:]))
                if epoch not in self.acked_epochs:
                    self.acked_epochs.add(epoch)
                    await self.ws.send(json.dumps({
                        "type": "play_ack", "epoch": epoch,
                        "client_play_ts_ms": time.time() * 1000}))
            else:
                data = json.loads(msg)
                if data["type"] == "pong":
                    t3 = time.time() * 1000
                    await self.ws.send(json.dumps({
                        "type": "clock",
                        "offset_ms": (data["t0"] + t3) / 2 - data["t1"],
                        "rtt_ms": t3 - data["t0"]}))
                elif data["type"] == "flush":
                    self.flushes.append(data["epoch"])

    async def handshake(self) -> None:
        await self.ws.send(json.dumps({"type": "hello", "user_agent": "fake-page"}))
        await self.ws.send(json.dumps({"type": "ping", "t0": time.time() * 1000}))
        await self.ws.send(json.dumps({"type": "capture_state", "ok": True,
                                       "detail": "synthetic"}))

    async def speak(self, fixture: str) -> None:
        with wave.open(str(FIXTURES / f"{fixture}.wav"), "rb") as wf:
            pcm = wf.readframes(wf.getnframes())
        frame = 640  # 20ms @ 16k

        async def send_frame(payload: bytes) -> None:
            await self.ws.send(struct.pack("<d", time.time() * 1000) + payload)
            await asyncio.sleep(0.02)

        for _ in range(25):
            await send_frame(b"\x00" * frame)
        for off in range(0, len(pcm), frame):
            await send_frame(pcm[off:off + frame])
        for _ in range(75):
            await send_frame(b"\x00" * frame)


async def wait_for(rt: Runtime, event_type: str, timeout: float = 45,
                   predicate=lambda e: True) -> dict:
    deadline = time.monotonic() + timeout
    seen = 0
    while time.monotonic() < deadline:
        ring = list(rt.bus.ring)
        for ev in ring[seen:]:
            if ev["type"] == event_type and predicate(ev):
                return ev
        seen = len(ring)
        await asyncio.sleep(0.1)
    raise TimeoutError(f"no {event_type}; recent: "
                       f"{[e['type'] for e in list(rt.bus.ring)[-12:]]}")


async def test_full_stack_over_websockets(tmp_path):
    settings = Settings(port=PORT, runs_dir=tmp_path)
    rt = Runtime(settings)
    server = uvicorn.Server(uvicorn.Config(build_app(rt), host="127.0.0.1",
                                           port=PORT, log_level="error"))
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    await rt.start(None)
    rt._admitted = True  # local-test semantics: greet once capture is live

    try:
        url = f"ws://127.0.0.1:{PORT}/ws/agent?token={settings.session_token}"
        async with websockets.connect(url, max_size=None) as ws:
            page = FakePage(ws)
            pump = asyncio.create_task(page.pump())
            await page.handshake()

            # Greeting reaches the page as paced, epoch-stamped audio.
            await wait_for(rt, "gemini.greeting_sent", timeout=25)
            await wait_for(rt, "turn.start", timeout=30)
            await wait_for(rt, "latency.turn", timeout=30)
            assert page.received_chunks, "no audio delivered to the page"

            # A spoken answer through the page mic updates the dashboard state.
            await page.speak("siebel_answer")
            ev = await wait_for(rt, "tool.call", timeout=45,
                                predicate=lambda e: e["data"]["name"] == "record_answer")
            assert ev["data"]["args"]["question_id"] == "current-systems"

            # Latency waterfall assembled with real page acks + clock sync.
            lat = await wait_for(rt, "latency.turn", timeout=45,
                                 predicate=lambda e: e["data"]["epoch"] > 1)
            assert lat["data"]["transit_play"] is not None
            pump.cancel()
    finally:
        await rt.shutdown()
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, 5)
        except (TimeoutError, asyncio.CancelledError):
            pass
