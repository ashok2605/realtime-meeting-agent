"""CLI: doctor | run | bench | leave | replay."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# Must be module-level: with `from __future__ import annotations`, FastAPI
# resolves the string annotation "WebSocket" against THIS module's globals.
# A function-local import silently degrades `ws: WebSocket` into a required
# query parameter and every websocket handshake dies with a 403.
from fastapi import WebSocket

from .config import Settings, get_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="gladiator",
                                     description="Realtime Google Meet discovery agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="verify keys, model access, tools and tunnel config")

    p_run = sub.add_parser("run", help="start server (+ bot if --meeting-url)")
    p_run.add_argument("--meeting-url", help="Google Meet URL to send the bot to")
    p_run.add_argument("--local-test", action="store_true",
                       help="no bot: open /agent in your own browser and talk to it")
    p_run.add_argument("--port", type=int, default=None)

    p_bench = sub.add_parser("bench", help="offline latency benchmark against Gemini Live")
    p_bench.add_argument("-n", "--turns", type=int, default=5)
    p_bench.add_argument("--wav", help="16kHz mono s16le wav to use as the spoken turn")

    p_leave = sub.add_parser("leave", help="make the last bot leave its meeting")
    p_leave.add_argument("--bot-id", help="override: explicit bot id")

    p_replay = sub.add_parser("replay", help="re-drive the dashboard from an events.jsonl")
    p_replay.add_argument("log", help="path to events.jsonl from a previous run")
    p_replay.add_argument("--speed", type=float, default=8.0)
    p_replay.add_argument("--port", type=int, default=8100)

    args = parser.parse_args()
    try:
        if args.cmd == "doctor":
            sys.exit(asyncio.run(cmd_doctor()))
        elif args.cmd == "run":
            asyncio.run(cmd_run(args))
        elif args.cmd == "bench":
            asyncio.run(cmd_bench(args))
        elif args.cmd == "leave":
            asyncio.run(cmd_leave(args))
        elif args.cmd == "replay":
            asyncio.run(cmd_replay(args))
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------- run
async def cmd_run(args) -> None:
    import uvicorn

    from .runtime import Runtime
    from .server import build_app

    settings = get_settings()
    if args.port:
        settings.port = args.port
    if not settings.gemini_api_key or not settings.recall_api_key:
        sys.exit("Missing GEMINI_API_KEY / RECALL_API_KEY in .env — run `gladiator doctor`.")
    if args.meeting_url and not settings.public_base_url:
        sys.exit("PUBLIC_BASE_URL is not set — start the tunnel (scripts/run_demo.sh) "
                 "or export it, then retry.")

    rt = Runtime(settings)
    app = build_app(rt)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port,
                                           log_level="warning", ws_ping_interval=20,
                                           ws_ping_timeout=20))
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)

    token = settings.session_token
    print(f"\n  dashboard   http://{settings.host}:{settings.port}/dashboard?token={token}")
    if settings.public_base_url:
        print(f"  agent page  {settings.public_base_url}/agent?token={token}")
    if args.local_test:
        rt._admitted = True  # no waiting room in local mode; greet once audio flows
        print(f"  LOCAL TEST  open http://{settings.host}:{settings.port}/agent?token={token}"
              f"\n              in Chrome, allow the mic, and start talking to Glad.")
    print(f"  event log   {rt.bus.log_path}\n")

    try:
        await rt.start(args.meeting_url)
        if args.meeting_url:
            print("  bot created — admit it from the Meet when it knocks.\n")
        await server_task
    finally:
        await rt.shutdown()
        server.should_exit = True


# ------------------------------------------------------------------ doctor
async def cmd_doctor() -> int:
    settings = get_settings()
    failures = 0

    def report(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures += 1

    print("\ngladiator doctor\n")
    report("RECALL_API_KEY set", bool(settings.recall_api_key))
    report("GEMINI_API_KEY set", bool(settings.gemini_api_key))

    # Recall reachability + region
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(f"{settings.recall_base}/bot/",
                               headers={"Authorization": f"Token {settings.recall_api_key}"})
        report(f"Recall API ({settings.recall_region})", r.status_code == 200,
               f"HTTP {r.status_code}")
    except Exception as exc:
        report("Recall API", False, str(exc)[:120])

    # Gemini: model available + Live session with combined tools
    try:
        from google import genai
        client = genai.Client(api_key=settings.gemini_api_key)
        models = [m.name async for m in await client.aio.models.list()]
        has_model = any(settings.gemini_model in (m or "") for m in models)
        report(f"model {settings.gemini_model}", has_model)
    except Exception as exc:
        report("Gemini API", False, str(exc)[:160])
        has_model = False

    if has_model:
        ok, detail = await _probe_live_combined(settings)
        report("Live session (google_search + functions combined)", ok, detail)
        if not ok:
            print("         -> set SEARCH_MODE=function in .env to use the web_search fallback")

    # Tunnel — only meaningful while the demo stack is up: run_demo.sh starts
    # ngrok + the server itself, so an offline endpoint here is informational.
    if settings.public_base_url:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(f"{settings.public_base_url}/healthz")
            if r.status_code == 200:
                report("tunnel -> local server", True, settings.public_base_url)
            else:
                print(f"  [INFO] tunnel endpoint offline ({settings.public_base_url}, "
                      f"HTTP {r.status_code}) — normal when the demo isn't running; "
                      "run_demo.sh brings it up")
        except Exception:
            print(f"  [INFO] tunnel endpoint unreachable ({settings.public_base_url}) — "
                  "normal when the demo isn't running; run_demo.sh brings it up")
    else:
        report("PUBLIC_BASE_URL set", False, "needed for a real meeting; fine for --local-test")

    print(f"\n{'all checks passed' if failures == 0 else f'{failures} check(s) failed'}\n")
    return 1 if failures else 0


async def _probe_live_combined(settings: Settings) -> tuple[bool, str]:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=settings.gemini_api_key)
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        tools=[{"google_search": {}},
               {"function_declarations": [{"name": "noop"}]}],
    )
    try:
        async with client.aio.live.connect(model=settings.gemini_model,
                                           config=config) as session:
            await session.send_client_content(
                turns={"role": "user", "parts": [{"text": "Say the word ready."}]})
            got_audio = 0

            async def _pump() -> None:
                nonlocal got_audio
                async for msg in session.receive():
                    if msg.server_content and msg.server_content.model_turn:
                        for p in msg.server_content.model_turn.parts or []:
                            if p.inline_data and p.inline_data.data:
                                got_audio += len(p.inline_data.data)
                    if msg.server_content and msg.server_content.turn_complete:
                        return
            await asyncio.wait_for(_pump(), timeout=30)
        return got_audio > 0, f"{got_audio} audio bytes"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:140]}"


# ------------------------------------------------------------------- bench
async def cmd_bench(args) -> None:
    """Time-to-first-audio distribution straight against Gemini Live.

    With --wav: paces real 16k audio frames like the meeting path does, then
    measures silence-detection + generation. Without: text-turn TTFA.
    """
    from google import genai
    from google.genai import types

    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)
    pcm = b""
    if args.wav:
        import wave
        with wave.open(args.wav, "rb") as wf:
            assert wf.getframerate() == 16000 and wf.getnchannels() == 1, \
                "need 16kHz mono wav (scripts/record_fixtures.sh makes them)"
            pcm = wf.readframes(wf.getnframes())

    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                prefix_padding_ms=settings.vad_prefix_padding_ms,
                silence_duration_ms=settings.vad_silence_duration_ms)),
        thinking_config=types.ThinkingConfig(thinking_level="minimal"),
    )
    results: list[float] = []
    async with client.aio.live.connect(model=settings.gemini_model, config=config) as session:
        for i in range(args.turns):
            t_ref = None
            if pcm:
                frame = 640  # 20ms @16k
                for off in range(0, len(pcm), frame):
                    await session.send_realtime_input(audio=types.Blob(
                        data=pcm[off:off + frame], mime_type="audio/pcm;rate=16000"))
                    await asyncio.sleep(0.02)
                t_ref = time.monotonic()  # end of speech
                await session.send_realtime_input(audio_stream_end=True)
            else:
                t_ref = time.monotonic()
                await session.send_client_content(turns={
                    "role": "user",
                    "parts": [{"text": f"Briefly say hello number {i + 1}."}]})

            async def _first_audio() -> float:
                async for msg in session.receive():
                    sc = msg.server_content
                    if sc and sc.model_turn:
                        for p in sc.model_turn.parts or []:
                            if p.inline_data and p.inline_data.data:
                                return time.monotonic()
                raise RuntimeError("session closed")
            t_audio = await asyncio.wait_for(_first_audio(), timeout=30)
            dt = (t_audio - t_ref) * 1000
            results.append(dt)
            print(f"  turn {i + 1}: first audio in {dt:.0f} ms")
            # drain the rest of the turn
            async def _drain() -> None:
                async for msg in session.receive():
                    if msg.server_content and msg.server_content.turn_complete:
                        return
            await asyncio.wait_for(_drain(), timeout=30)

    results.sort()
    p = lambda q: results[min(len(results) - 1, int(len(results) * q))]  # noqa: E731
    print(f"\n  TTFA over {len(results)} turns: p50 {p(0.5):.0f} ms · p95 {p(0.95):.0f} ms "
          f"· min {results[0]:.0f} · max {results[-1]:.0f}")
    print("  (meeting adds: page->server transit + gate + pacing + page buffer — "
          "see the dashboard waterfall for the full path)\n")


# ------------------------------------------------------------------- leave
async def cmd_leave(args) -> None:
    from .core.events import EventBus
    from .recall.client import RecallClient

    settings = get_settings()
    bot_id = args.bot_id
    if not bot_id:
        last = settings.runs_dir / "last_bot.json"
        if not last.exists():
            sys.exit("no runs/last_bot.json found — pass --bot-id")
        bot_id = json.loads(last.read_text())["bot_id"]
    client = RecallClient(settings, EventBus())
    await client.leave(bot_id)
    await client.close()
    print(f"leave requested for bot {bot_id}")


# ------------------------------------------------------------------ replay
async def cmd_replay(args) -> None:
    """Re-drive the dashboard from a recorded JSONL event log. Renders state
    only — no audio, no API calls."""
    import uvicorn
    from fastapi import FastAPI, WebSocketDisconnect
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    from .core.discovery import DiscoveryTracker
    from .core.events import EventBus

    log_path = Path(args.log)
    events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    if not events:
        sys.exit(f"{log_path} contains no events")
    print(f"replaying {len(events)} events from {log_path} at {args.speed}x")

    web_dir = Path(__file__).parent / "web"
    questions = DiscoveryTracker(EventBus()).snapshot()  # fresh question defs for the panel

    app = FastAPI()
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.get("/dashboard")
    async def dashboard() -> HTMLResponse:  # replay is local-only: no token needed
        return HTMLResponse((web_dir / "dashboard/index.html").read_text())

    @app.websocket("/ws/dashboard")
    async def ws_dashboard(ws: WebSocket) -> None:
        await ws.accept()
        try:
            await ws.send_text(json.dumps({"type": "hello", "snapshot": {
                "replay": True, "discovery": questions, "face": "idle",
                "bot_status": f"replay of {log_path.name}",
            }}))
            prev_ts = events[0]["ts"]
            for ev in events:
                delay = max(0.0, min((ev["ts"] - prev_ts) / args.speed, 2.0))
                prev_ts = ev["ts"]
                if delay:
                    await asyncio.sleep(delay)
                await ws.send_text(json.dumps({"type": "event", "event": ev}, default=str))
            while True:  # keep the socket open once the timeline finishes
                await asyncio.sleep(3600)
        except (WebSocketDisconnect, RuntimeError):
            pass

    print(f"\n  replay dashboard: http://127.0.0.1:{args.port}/dashboard\n")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                           log_level="warning"))
    await server.serve()


if __name__ == "__main__":
    main()
