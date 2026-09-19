from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.actions.call_status import CallResult, derive_api_base, map_call
from app.actions.journal import ActionJournal
from app.actions.models import ActionTransition
from app.actions.pager import HappyRobotPager, voice_pautas


@pytest.mark.parametrize(
    ("run", "session", "output", "expected"),
    [
        ({"status": "queued"}, None, None, CallResult("queued")),
        ({"status": "running"}, {"status": "ringing"}, None, CallResult("ringing")),
        (
            {"status": "running"},
            {"status": "active", "call_connected_at": "2026-09-19T12:00:00Z"},
            None,
            CallResult("answered"),
        ),
        (
            {"status": "completed"},
            {"status": "completed", "call_connected_at": "2026-09-19T12:00:00Z"},
            None,
            CallResult("hung_up"),
        ),
        (
            {"status": "completed"},
            {"status": "completed", "failure_reason": "no_answer"},
            None,
            CallResult("no_pickup"),
        ),
        (
            {"status": "failed"},
            {"status": "failed", "failure_reason": "invalid_phone_number"},
            None,
            CallResult("failed", error_code="invalid_phone_number"),
        ),
        (
            {"status": "failed"},
            {"status": "failed", "sip_code": 403, "failure_reason": "provider rejected call"},
            None,
            CallResult("failed", error_code="happyrobot_call_failed"),
        ),
    ],
)
def test_maps_happyrobot_call_state(run, session, output, expected):
    assert map_call(run, session, output) == expected


def test_output_can_supply_terminal_call_evidence():
    assert map_call(
        {"status": "completed"},
        None,
        {"call_end_event": "agent_hung_up", "duration": 12},
    ) == CallResult("hung_up")


def test_terminal_run_with_duration_without_connection_timestamp_is_hung_up():
    assert map_call(
        {"status": "completed"},
        {"status": "completed", "duration": 12},
        None,
    ) == CallResult("hung_up")


@pytest.mark.parametrize(
    ("run", "session", "expected"),
    [
        (
            {"status": "completed"},
            {"status": "completed", "duration": 12, "failure_reason": "never_connected"},
            CallResult("no_pickup"),
        ),
        (
            {"status": "completed"},
            {"status": "completed", "duration": 12, "failure_reason": "no_answer"},
            CallResult("no_pickup"),
        ),
        (
            {"status": "completed"},
            {"status": "completed", "duration": 12, "sip_code": 486},
            CallResult("no_pickup"),
        ),
        (
            {"status": "canceled"},
            {"status": "canceled", "duration": 12},
            CallResult("failed", error_code="happyrobot_call_failed"),
        ),
    ],
)
def test_duration_does_not_override_terminal_failure_evidence(run, session, expected):
    assert map_call(run, session, None) == expected


def test_connected_evidence_wins_over_no_pickup_markers():
    assert map_call(
        {"status": "completed"},
        {
            "status": "completed",
            "call_connected_at": "2026-09-19T12:00:00Z",
            "failure_reason": "no_answer busy",
            "sip_code": 486,
        },
        None,
    ) == CallResult("hung_up")


def test_derives_eu_api_base_from_hook():
    assert (
        derive_api_base("https://platform.eu.happyrobot.ai/hooks/abc", None)
        == "https://platform.eu.happyrobot.ai/api/v2"
    )


class HappyRobotStub:
    def __init__(
        self,
        attempts: list[list[tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]]],
        *,
        hook_statuses: list[int] | None = None,
        hook_body: dict[str, Any] | None = None,
    ) -> None:
        self.attempts = attempts
        self.hook_statuses = hook_statuses or [200]
        self.hook_body = hook_body
        self.hook_calls = 0
        self.poll_indexes: dict[str, int] = {}
        self.payloads: list[dict[str, Any]] = []
        self.idempotency_keys: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.hook_calls += 1
            self.payloads.append(json.loads(request.content))
            self.idempotency_keys.append(request.headers.get("Idempotency-Key"))
            status = self.hook_statuses[min(self.hook_calls - 1, len(self.hook_statuses) - 1)]
            if status != 200:
                return httpx.Response(status, json={"error": "temporary"})
            successful_calls = sum(
                item == 200
                for item in self.hook_statuses[: self.hook_calls]
            )
            body = self.hook_body or {"data": {"run_id": f"run-{successful_calls}"}}
            return httpx.Response(200, json=body)

        run_id = request.url.path.split("/runs/", 1)[1].split("/", 1)[0]
        attempt_index = int(run_id.split("-")[-1]) - 1
        poll_index = self.poll_indexes.get(run_id, 0)
        polls = self.attempts[attempt_index]
        run, session, output = polls[min(poll_index, len(polls) - 1)]
        path = request.url.path
        if path.endswith(f"/runs/{run_id}"):
            return httpx.Response(200, json=run)
        if path.endswith("/sessions"):
            self.poll_indexes[run_id] = poll_index + 1
            return httpx.Response(200, json={"data": [] if session is None else [session]})
        if path.endswith("/nodes"):
            nodes = [] if output is None else [{"name": "llamada", "output_id": "out-1"}]
            return httpx.Response(200, json={"data": nodes})
        if path.endswith("/outputs/out-1"):
            return httpx.Response(200, json={"data": output})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


async def run_pager(
    stub: Callable[[httpx.Request], httpx.Response],
    *,
    level: int = 4,
    hook_url: str = "https://platform.happyrobot.ai/hooks/page",
    api_key: str = "secret",
    phone: str = "+34600000000",
    poll_interval: float = 0.001,
    poll_timeout: float = 1,
    intent: str = "sandbox_escape",
    action_taken: str = "Contained all live runs",
) -> list[tuple[str, str | None, str | None]]:
    transitions: list[tuple[str, str | None, str | None]] = []

    async def transition(
        status: str,
        detail: str | None = None,
        error_code: str | None = None,
        *,
        call_status: str | None = None,
    ) -> None:
        transitions.append((status, call_status, error_code))

    client = httpx.AsyncClient(transport=httpx.MockTransport(stub))
    pager = HappyRobotPager(
        hook_url=hook_url,
        api_key=api_key,
        api_base="https://platform.happyrobot.ai/api/v2",
        phone=phone,
        name="Guli",
        poll_interval=poll_interval,
        poll_timeout=poll_timeout,
        client=client,
    )
    try:
        await pager.page(
            level,
            "incident-1",
            intent,
            action_taken,
            transition,
        )
    finally:
        await client.aclose()
    return transitions


def answered_then_hung_up_stub() -> HappyRobotStub:
    return HappyRobotStub(
        [[
            (
                {"status": "running"},
                {"status": "active", "call_connected_at": "now"},
                None,
            ),
            (
                {"status": "completed"},
                {"status": "completed", "call_connected_at": "now"},
                None,
            ),
        ]]
    )


async def test_posts_documented_payload_with_telefono_and_numeric_level():
    stub = answered_then_hung_up_stub()

    await run_pager(stub)

    assert stub.payloads == [
        {
            "tipo_emergencia": "sandbox_escape (level 4, run incident-1)",
            "pautas": voice_pautas("Contained all live runs"),
            "nivel_gravedad": "4",
            "nombre_contacto": "Guli",
            "telefono": "+34600000000",
            "nodos": "sandbox escape. Nivel 4. Contained all live runs.",
        }
    ]
    assert "one minute" in stub.payloads[0]["pautas"]


async def test_reports_answered_then_hung_up():
    stub = answered_then_hung_up_stub()

    transitions = await run_pager(stub)

    assert transitions == [
        ("running", "queued", None),
        ("running", "answered", None),
        ("ok", "hung_up", None),
    ]


async def test_retries_webhook_once_after_5xx():
    stub = HappyRobotStub(
        [[({"status": "completed"}, {"failure_reason": "invalid number"}, None)]],
        hook_statuses=[503, 200],
    )

    transitions = await run_pager(stub)

    assert stub.hook_calls == 2
    expected_key = hashlib.sha256(b"hackspain-pager:incident-1:4:0").hexdigest()
    assert stub.idempotency_keys[0] == expected_key
    assert stub.idempotency_keys[0] == stub.idempotency_keys[1]
    assert transitions[-1] == ("failed", "failed", "invalid_phone_number")


async def test_completed_no_pickup_fails_without_a_second_call():
    no_pickup = (
        {"status": "completed"},
        {"status": "completed", "failure_reason": "no_answer"},
        None,
    )
    stub = HappyRobotStub([[no_pickup]])

    transitions = await run_pager(stub)

    assert stub.hook_calls == 1
    assert transitions[-1] == ("failed", "no_pickup", "no_pickup")


async def test_terminal_duration_stops_polling_as_success():
    stub = HappyRobotStub(
        [[({"status": "completed"}, {"status": "completed", "duration": 12}, None)]]
    )

    transitions = await run_pager(stub)

    assert transitions[-1] == ("ok", "hung_up", None)
    assert stub.hook_calls == 1


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("hook_url", ""),
        ("api_key", ""),
        ("phone", ""),
        ("poll_interval", 0),
    ],
)
async def test_invalid_configuration_fails_before_queued(override, value):
    stub = HappyRobotStub([])

    transitions = await run_pager(stub, **{override: value})

    assert transitions == [("failed", "failed", "pager_configuration")]
    assert stub.hook_calls == 0


async def test_exhausted_webhook_5xx_reports_specific_failure():
    stub = HappyRobotStub([], hook_statuses=[503, 503])

    transitions = await run_pager(stub)

    assert stub.hook_calls == 2
    assert transitions == [("running", "queued", None), ("failed", "failed", "happyrobot_5xx")]


async def test_polling_timeout_reports_specific_failure():
    stub = HappyRobotStub([[({"status": "running"}, {"status": "ringing"}, None)]])

    transitions = await run_pager(stub, poll_interval=0.001, poll_timeout=0.003)

    assert transitions[-1] == ("failed", "failed", "pager_timeout")
    assert stub.hook_calls == 1


async def test_missing_run_id_reports_specific_failure():
    stub = HappyRobotStub([], hook_body={"data": {}})

    transitions = await run_pager(stub)

    assert transitions == [("running", "queued", None), ("failed", "failed", "missing_run_id")]


async def test_l5_payload_keeps_level_and_uses_defensive_fallbacks():
    stub = HappyRobotStub(
        [[({"status": "completed"}, {"failure_reason": "invalid number"}, None)]]
    )

    await run_pager(stub, level=5, intent="", action_taken="")

    assert stub.payloads[0]["nivel_gravedad"] == "5"
    assert stub.payloads[0]["tipo_emergencia"] == (
        "critical agent activity (level 5, run incident-1)"
    )
    assert stub.payloads[0]["pautas"] == voice_pautas("Escalated level 5 response")


def test_pager_watch_imports_under_system_python_without_backend_package():
    system_python = Path("/usr/bin/python3")
    if not system_python.exists():
        pytest.skip("host-only smoke: /usr/bin/python3 is unavailable")
    host_repo_root = Path(__file__).resolve().parents[4]
    script = host_repo_root / "scripts" / "pager_watch.py"
    if not script.is_file():
        pytest.skip(
            f"host-only smoke: repo script is unavailable at {script}"
        )

    result = subprocess.run(
        [str(system_python), str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--run-id" in result.stdout


def test_pager_import_does_not_load_service_or_config():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.actions.pager; "
                "assert 'app.actions.service' not in sys.modules; "
                "assert 'app.config' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


async def test_default_http_client_timeout_survives_slow_hooks():
    pager = HappyRobotPager(
        hook_url="https://platform.happyrobot.ai/hooks/page",
        api_key="secret",
        api_base="https://platform.happyrobot.ai/api/v2",
        phone="+34600000000",
        name="Guli",
        poll_interval=0.001,
        poll_timeout=1,
    )
    client = pager._client_factory()
    try:
        timeout = client.timeout
        assert timeout.connect is None or timeout.connect >= 30
        assert timeout.read is None or timeout.read >= 30
    finally:
        await client.aclose()


async def test_journal_replay_keeps_final_call_status_separate(tmp_path):
    journal = ActionJournal(tmp_path)
    journal.append(
        ActionTransition(
            incident_id="incident",
            level=4,
            action_id="incident:page:l4",
            name="page_oncall",
            ladder_level=None,
            mode="real",
            status="ok",
            call_status="hung_up",
        )
    )

    state = journal.state("incident")

    assert state.pager_status == "ok"
    assert state.call_status == "hung_up"
