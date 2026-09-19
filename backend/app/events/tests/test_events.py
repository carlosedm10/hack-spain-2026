from __future__ import annotations

from app.events import EventPhase, IdentityState, normalize_event, redact_event


def test_normalize_legacy_tool_event_preserves_raw_payload():
    event = normalize_event(
        "run-1",
        {
            "event": "file_read",
            "path": "/workspace/.env",
            "identity_state": "unverified",
        },
        sequence=3,
    )

    assert event.run_id == "run-1"
    assert event.sequence == 3
    assert event.kind == "file_read"
    assert event.phase == EventPhase.COMPLETED
    assert event.tool == "read_file"
    assert event.target == "/workspace/.env"
    assert event.identity_state == IdentityState.UNVERIFIED
    assert event.raw["path"] == "/workspace/.env"


def test_redaction_happens_before_event_is_persisted():
    event = normalize_event(
        "run-1",
        {
            "event": "network_request",
            "target": "https://alice:secret@example.com/send?token=abc&mode=test",
            "args": {
                "authorization": "Bearer abc.def",
                "nested": {"password": "hunter2"},
            },
        },
    )

    redacted = redact_event(event)

    assert redacted.args["authorization"] == "[REDACTED]"
    assert redacted.args["nested"]["password"] == "[REDACTED]"
    assert "alice" not in redacted.target
    assert "abc" not in redacted.target
    assert redacted.metadata["redaction"]["policy_version"] == "redaction:v1"
