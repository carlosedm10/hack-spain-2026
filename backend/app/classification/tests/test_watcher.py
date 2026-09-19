from __future__ import annotations

import json

import httpx
import pytest
from httpx import AsyncClient, MockTransport

from app.classification import WatcherVerdict, review
from app.classification.watcher import SYSTEM, parse_verdict
from app.config import settings


@pytest.fixture(autouse=True)
def helmcode_config(monkeypatch):
    monkeypatch.setattr(settings, "helmcode_api_key", "helm-key")
    monkeypatch.setattr(settings, "supervisor_model", "deepseek-v4-flash")


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


async def test_review_posts_helmcode_url_model_and_messages():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200, json=_reply('{"escalate": true, "suspected_level": 4, "note": "exfil"}')
        )

    ac = AsyncClient(transport=MockTransport(handler), base_url="https://api.helmcode.com")
    verdict = await review(ac, {"short_term": [{"event": "file_read"}]})

    assert str(captured[0].url) == f"{settings.helmcode_base_url}/chat/completions"
    body = json.loads(captured[0].content)
    assert body["model"] == "deepseek-v4-flash"
    assert body["messages"][0] == {"role": "system", "content": SYSTEM}
    assert body["messages"][1]["role"] == "user"
    assert json.loads(body["messages"][1]["content"]) == {"short_term": [{"event": "file_read"}]}
    assert captured[0].headers["Authorization"] == "Bearer helm-key"
    assert verdict == WatcherVerdict(escalate=True, suspected_level=4, note="exfil")


async def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(settings, "helmcode_api_key", "")
    ac = AsyncClient(transport=MockTransport(lambda r: httpx.Response(200)), base_url="x")
    with pytest.raises(RuntimeError, match="HELMCODE_API_KEY"):
        await review(ac, {})


async def test_unparseable_reply_never_escalates():
    ac = AsyncClient(
        transport=MockTransport(lambda r: httpx.Response(200, json=_reply("no json here"))),
        base_url="x",
    )
    verdict = await review(ac, {})
    assert verdict.escalate is False
    assert verdict.suspected_level is None


@pytest.mark.parametrize(
    ("suspected_level", "escalate"),
    [(1, False), (2, False), (3, True), (4, True), (5, True)],
)
async def test_escalate_iff_suspected_level_ge_3(suspected_level, escalate):
    content = json.dumps({"escalate": escalate, "suspected_level": suspected_level, "note": "n"})
    ac = AsyncClient(
        transport=MockTransport(lambda r: httpx.Response(200, json=_reply(content))),
        base_url="x",
    )
    verdict = await review(ac, {})
    assert verdict.escalate is escalate
    assert verdict.suspected_level == suspected_level


def test_parse_verdict_clean_json():
    d, ok = parse_verdict('{"escalate": false, "suspected_level": 2, "note": "ok"}')
    assert ok is True
    assert d == {"escalate": False, "suspected_level": 2, "note": "ok"}


def test_parse_verdict_json_fence():
    text = '```json\n{"escalate": true, "suspected_level": 3, "note": "creds"}\n```'
    d, ok = parse_verdict(text)
    assert ok is True
    assert d["suspected_level"] == 3


def test_parse_verdict_prose_wrapped_json():
    text = 'The verdict is {"escalate": true, "suspected_level": 5, "note": "breach"} — act now.'
    d, ok = parse_verdict(text)
    assert ok is True
    assert d["suspected_level"] == 5


def test_parse_verdict_garbage():
    d, ok = parse_verdict("I cannot help with that.")
    assert d is None
    assert ok is False


def test_parse_verdict_rejects_out_of_range_level():
    d, ok = parse_verdict('{"escalate": true, "suspected_level": 9, "note": "x"}')
    assert ok is False
    assert d["suspected_level"] == 9


def test_parse_verdict_rejects_missing_escalate():
    d, ok = parse_verdict('{"suspected_level": 3}')
    assert ok is False
    assert d == {"suspected_level": 3}
