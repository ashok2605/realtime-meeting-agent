"""Recall realtime event parsing, using payload shapes from Recall's docs."""

import base64

from gladiator.audio.bridge import AudioBridge
from gladiator.core.events import EventBus
from gladiator.core.latency import LatencyTracker
from gladiator.core.turn_gate import SpeechState
from gladiator.recall.events import RecallEventRouter


def make():
    bus = EventBus()
    speech = SpeechState(bot_name="Gladiator")
    bridge = AudioBridge(bus, LatencyTracker(bus), chunk_ms=40)
    return RecallEventRouter(speech, bus, bridge), speech, bridge, bus


def recall_event(event: str, participant: dict | None = None, **inner):
    data = dict(inner)
    if participant is not None:
        data["participant"] = participant
    return {
        "event": event,
        "data": {
            "data": data,
            "realtime_endpoint": {"id": "re-1", "metadata": {}},
            "recording": {"id": "rec-1", "metadata": {}},
            "bot": {"id": "bot-1", "metadata": {}},
        },
    }


PRIYA = {"id": 7, "name": "Priya", "is_host": True, "platform": "google_meet",
         "extra_data": None, "email": None}
BOT = {"id": 42, "name": "Gladiator", "is_host": False, "platform": "google_meet",
       "extra_data": None, "email": None}


def test_speech_on_off_updates_state():
    router, speech, _, _ = make()
    router.handle(recall_event("participant_events.speech_on", PRIYA))
    assert speech.any_human_speaking()
    router.handle(recall_event("participant_events.speech_off", PRIYA))
    assert not speech.any_human_speaking()


def test_bots_own_speech_filtered():
    router, speech, _, _ = make()
    router.handle(recall_event("participant_events.speech_on", BOT))
    assert not speech.any_human_speaking(), "the bot must not gate itself"


def test_join_leave_roster_and_floor_release():
    router, speech, _, _ = make()
    router.handle(recall_event("participant_events.join", PRIYA))
    assert router.roster == {"7": "Priya"}
    router.handle(recall_event("participant_events.speech_on", PRIYA))
    router.handle(recall_event("participant_events.leave", PRIYA))
    assert router.roster == {}
    assert not speech.any_human_speaking(), "leave mid-speech releases the floor"


def test_audio_fallback_feeds_bridge():
    router, _, bridge, _ = make()
    pcm = b"\x01\x02" * 3200  # 200ms @ 16k
    router.handle(recall_event("audio_mixed_raw.data",
                               buffer=base64.b64encode(pcm).decode(),
                               timestamp={"relative": 1.0, "absolute": "2026-08-19T00:00:00Z"}))
    assert bridge._inbound.qsize() == 1
    assert bridge._inbound.get_nowait() == pcm


def test_unknown_event_logged_not_crashed():
    router, _, _, bus = make()
    router.handle({"event": "participant_events.chat_message", "data": {"data": {}}})
    assert any(e["type"] == "recall.event" for e in bus.ring)


def test_hybrid_vad_hook_fires_when_all_quiet():
    calls = []
    bus = EventBus()
    speech = SpeechState(bot_name="Gladiator")
    bridge = AudioBridge(bus, LatencyTracker(bus), chunk_ms=40)
    router = RecallEventRouter(speech, bus, bridge,
                               on_human_speech_off=lambda: calls.append(1))
    marco = dict(PRIYA, id=8, name="Marco")
    router.handle(recall_event("participant_events.speech_on", PRIYA))
    router.handle(recall_event("participant_events.speech_on", marco))
    router.handle(recall_event("participant_events.speech_off", PRIYA))
    assert not calls, "Marco is still talking — no audio_stream_end yet"
    router.handle(recall_event("participant_events.speech_off", marco))
    assert calls == [1]
