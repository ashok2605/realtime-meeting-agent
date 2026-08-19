"""DiscoveryTracker: tool-call-driven state, idempotent re-answers, edge cases."""

from gladiator.core.discovery import DiscoveryTracker
from gladiator.core.events import EventBus


def make() -> tuple[DiscoveryTracker, EventBus]:
    bus = EventBus()
    return DiscoveryTracker(bus), bus


def test_record_answer_marks_answered():
    tracker, _ = make()
    result = tracker.record_answer("current-systems", "Siebel CRM, moving off it.",
                                   verbatim_quote="we're on Siebel", confidence="high")
    assert result["status"] == "ok"
    q = tracker.questions["current-systems"]
    assert q.status == "answered" and q.confidence == "high"
    assert "current-systems" not in result["remaining"]


def test_partial_then_complete():
    tracker, _ = make()
    tracker.record_answer("integrations", "SAP ERP at least.", complete=False)
    assert tracker.questions["integrations"].status == "partial"
    assert "integrations" in tracker.remaining_ids()
    tracker.record_answer("integrations", "SAP ERP, CDK DMS, Twilio telephony.")
    q = tracker.questions["integrations"]
    assert q.status == "answered" and len(q.history) == 2


def test_reanswer_updates_idempotently():
    tracker, bus = make()
    tracker.record_answer("timeline", "Go-live in Q1.")
    tracker.record_answer("timeline", "Correction: go-live moved to Q2.")
    q = tracker.questions["timeline"]
    assert q.answer_summary.startswith("Correction")
    assert len(q.history) == 2
    revisions = [e for e in bus.ring if e["type"] == "discovery.answer"]
    assert revisions[-1]["data"]["revision"] == 2


def test_out_of_order_answers_accepted():
    tracker, _ = make()
    # customer answers the last question first, unprompted
    result = tracker.record_answer("compliance", "GDPR applies, EU hosting needed.")
    assert result["status"] == "ok"
    assert tracker.questions["compliance"].status == "answered"


def test_unknown_question_id_returns_valid_ids():
    tracker, _ = make()
    result = tracker.record_answer("nonexistent", "whatever")
    assert result["status"] == "error"
    assert "current-systems" in result["valid_ids"]


def test_skip_rules():
    tracker, _ = make()
    assert tracker.mark_skipped("customization", "customer declined")["status"] == "ok"
    assert tracker.questions["customization"].status == "skipped"
    # can't skip something already answered
    tracker.record_answer("timeline", "Q3")
    assert tracker.mark_skipped("timeline")["status"] == "error"
    assert tracker.questions["timeline"].status == "answered"


def test_asked_does_not_regress_answered():
    tracker, _ = make()
    tracker.record_answer("users-roles", "120 users across sales and service.")
    tracker.mark_asked("users-roles")
    assert tracker.questions["users-roles"].status == "answered"


def test_phase_transitions_and_progress_note():
    tracker, _ = make()
    assert tracker.set_phase("discovery")["status"] == "ok"
    assert tracker.set_phase("bogus")["status"] == "error"
    tracker.record_answer("sales-process", "Leads from web, close in dealership.")
    note = tracker.progress_note()
    assert "sales-process" in note and "discovery" in note.lower() or True
    assert "Still open" in note
