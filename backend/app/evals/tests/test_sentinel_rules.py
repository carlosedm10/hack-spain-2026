from app.events import EventEffect, IdentityState, MonitorEvent, TrustState
from app.monitor.neighborhood import linked_history
from app.monitor.sentinel import inspect


def _event(**kwargs) -> MonitorEvent:
    payload = {
        "id": "e",
        "run_id": "r1",
        "kind": "utterance",
        "content": "ok",
    }
    payload.update(kwargs)
    return MonitorEvent(**payload)


def _pad(n: int, run_id: str = "r1") -> list[MonitorEvent]:
    return [
        _event(id=f"p{i}", run_id=run_id, kind="utterance", content=f"benign {i}") for i in range(n)
    ]


def test_n1_unknown_tool():
    history = _pad(16)
    current = _event(id="bad", kind="tool_write", tool="docker")
    rules = {item.rule_id for item in inspect(current, history)}
    assert "N1_UNKNOWN_TOOL" in rules
    assert "N1_UNKNOWN_TOOL" not in {
        item.rule_id for item in inspect(_event(id="ok", tool="read_file"), history)
    }


def test_e1_unauthorized_write():
    history = _pad(16)
    current = _event(
        id="w",
        kind="tool_write",
        identity_state=IdentityState.UNVERIFIED,
    )
    assert any(item.rule_id == "E1_UNAUTHORIZED_WRITE" for item in inspect(current, history))


def test_e9_provenance_upgrade():
    history = _pad(16)
    current = _event(id="m", kind="memory_write", trust=TrustState.UNTRUSTED)
    assert any(item.rule_id == "E9_PROVENANCE_UPGRADE" for item in inspect(current, history))


def test_p1_exfiltration_path():
    history = _pad(16) + [
        _event(
            id="secret",
            kind="file_read",
            effect=EventEffect(sensitivity="credential"),
        )
    ]
    current = _event(id="net", kind="network_request")
    assert any(item.rule_id == "P1_EXFILTRATION_PATH" for item in inspect(current, history))
    assert not any(
        item.rule_id == "P1_EXFILTRATION_PATH"
        for item in inspect(_event(id="talk", kind="utterance"), history)
    )


def test_n2_write_burst_needs_five_writes_in_history():
    writes = [_event(id=f"w{i}", kind="tool_write", content=f"write {i}") for i in range(5)]
    history = _pad(10) + writes
    current = _event(id="after", kind="utterance")
    assert any(item.rule_id == "N2_WRITE_BURST" for item in inspect(current, history))
    assert not any(
        item.rule_id == "N2_WRITE_BURST" for item in inspect(current, _pad(10) + writes[:4])
    )


def test_e2_scope_over_cap():
    history = _pad(16)
    current = _event(id="big", kind="schedule", effect=EventEffect(scope=250))
    assert any(item.rule_id == "E2_SCOPE_OVER_CAP" for item in inspect(current, history))


def test_e4_cross_channel_and_cross_run():
    history = _pad(16) + [
        _event(
            id="a",
            run_id="r1",
            kind="tool_write",
            target="rate-card",
            channel="slack",
        )
    ]
    current = _event(
        id="b",
        run_id="r2",
        kind="memory_write",
        target="rate-card",
        channel="email",
    )
    findings = inspect(current, history)
    hit = next(item for item in findings if item.rule_id == "E4_CROSS_CHANNEL_REENTRY")
    assert hit.cross_run is True


def test_e8_stale_authority():
    history = _pad(16)
    current = _event(
        id="stale",
        kind="memory_write",
        content="Reuse the cached old dispatcher approval",
    )
    assert any(item.rule_id == "E8_STALE_AUTHORITY" for item in inspect(current, history))


def test_p3_memory_propagation_cross_run():
    history = _pad(16) + [_event(id="mem", run_id="r1", kind="memory_read", target="approval")]
    current = _event(
        id="sched",
        run_id="r2",
        kind="schedule",
        derived_from=["mem"],
    )
    hit = next(
        item for item in inspect(current, history) if item.rule_id == "P3_MEMORY_PROPAGATION"
    )
    assert hit.cross_run is True


def test_e6_missing_handoff_after_three_history_events():
    pd = _event(id="pd", kind="policy_decision", content="Required handoff to a human")
    history = _pad(16) + [pd] + [_event(id=f"t{i}", kind="utterance") for i in range(3)]
    current = _event(id="late", kind="utterance", content="still talking")
    assert any(item.rule_id == "E6_MISSING_HANDOFF" for item in inspect(current, history))
    early = _event(id="soon", kind="utterance")
    assert not any(item.rule_id == "E6_MISSING_HANDOFF" for item in inspect(early, _pad(16) + [pd]))


def test_n2_does_not_count_unrelated_lab_runs():
    run_a = [_event(id=f"a{i}", run_id="lab-a", kind="tool_write") for i in range(3)]
    run_b = [_event(id=f"b{i}", run_id="lab-b", kind="tool_write") for i in range(3)]
    current = _event(id="probe", run_id="lab-b", kind="utterance")
    neighborhood = linked_history(current, {"lab-a": run_a, "lab-b": run_b})
    assert all(item.run_id == "lab-b" for item in neighborhood)
    assert not any(item.rule_id == "N2_WRITE_BURST" for item in inspect(current, neighborhood))


def test_e4_fires_across_runs_that_share_a_target():
    first = _event(
        id="a",
        run_id="r1",
        kind="tool_write",
        target="rate-card",
        channel="slack",
    )
    current = _event(
        id="b",
        run_id="r2",
        kind="memory_write",
        target="rate-card",
        channel="email",
    )
    neighborhood = linked_history(current, {"r1": [first], "r2": []})
    hit = next(
        item
        for item in inspect(current, neighborhood)
        if item.rule_id == "E4_CROSS_CHANNEL_REENTRY"
    )
    assert hit.cross_run is True


def test_p3_fires_when_write_derives_from_other_run_memory():
    memory = _event(id="mem", run_id="r1", kind="memory_write", target="approval")
    current = _event(
        id="w",
        run_id="r2",
        kind="tool_write",
        derived_from=["mem"],
    )
    neighborhood = linked_history(current, {"r1": [memory], "r2": []})
    hit = next(
        item for item in inspect(current, neighborhood) if item.rule_id == "P3_MEMORY_PROPAGATION"
    )
    assert hit.cross_run is True


def test_p1_ignores_unrelated_credential_read_from_another_run():
    secret = _event(
        id="secret",
        run_id="r1",
        kind="file_read",
        effect=EventEffect(sensitivity="credential"),
    )
    current = _event(
        id="net", run_id="r2", kind="network_request", target="https://outside.invalid"
    )
    neighborhood = linked_history(current, {"r1": [secret], "r2": []})
    assert secret not in neighborhood
    assert not any(
        item.rule_id == "P1_EXFILTRATION_PATH" for item in inspect(current, neighborhood)
    )


def test_p1_fires_when_network_request_is_linked_to_other_run_secret():
    secret = _event(
        id="secret",
        run_id="r1",
        kind="file_read",
        agent="dispatcher",
        target="vault://creds",
        effect=EventEffect(sensitivity="credential"),
    )
    derived = _event(
        id="net-derived",
        run_id="r2",
        kind="network_request",
        derived_from=["secret"],
        target="https://outside.invalid",
    )
    same_actor = _event(
        id="net-actor",
        run_id="r2",
        kind="network_request",
        agent="dispatcher",
        target="vault://creds",
    )
    for current in (derived, same_actor):
        neighborhood = linked_history(current, {"r1": [secret], "r2": []})
        assert any(
            item.rule_id == "P1_EXFILTRATION_PATH" for item in inspect(current, neighborhood)
        )


def test_e6_does_not_use_another_run_handoff():
    pd = _event(id="pd", run_id="r1", kind="policy_decision", content="Required handoff to a human")
    history = [pd] + [_event(id=f"t{i}", run_id="r1", kind="utterance") for i in range(3)]
    current = _event(id="late", run_id="r2", kind="utterance")
    neighborhood = linked_history(current, {"r1": history, "r2": []})
    assert not any(item.rule_id == "E6_MISSING_HANDOFF" for item in inspect(current, neighborhood))
    assert not any(item.rule_id == "E6_MISSING_HANDOFF" for item in inspect(current, history))
