from __future__ import annotations

from typing import Any

import httpx

from app.classification import jev, watcher
from app.classification.models import Level, Verdict
from app.config import settings

_UNSURE_STREAKS: dict[str, int] = {}
_WATCHER_INVOCATIONS = 0


def reset() -> None:
    _UNSURE_STREAKS.clear()


def watcher_invocations() -> int:
    return _WATCHER_INVOCATIONS


def _record_watcher_contribution() -> None:
    global _WATCHER_INVOCATIONS
    _WATCHER_INVOCATIONS += 1


async def evaluate(
    client: httpx.AsyncClient,
    run_id: str,
    event: dict[str, Any],
    *,
    monitor_context: dict[str, Any] | None = None,
) -> Verdict:
    from app.graph import graph

    prior = graph.level(run_id)
    state = _state(run_id, event, prior)
    if monitor_context is not None:
        state["monitor"] = monitor_context
    verdict = await _classify(client, state)
    if verdict.degraded:
        return Verdict(
            level=prior,
            confidence=0.0,
            degraded=True,
            degraded_reason=verdict.degraded_reason,
        )
    if verdict.confidence < settings.watcher_tau:
        _UNSURE_STREAKS[run_id] = _UNSURE_STREAKS.get(run_id, 0) + 1
        if _UNSURE_STREAKS[run_id] >= settings.watcher_persistence:
            _UNSURE_STREAKS.pop(run_id, None)
            note = await _watcher_note(client, state, verdict)
            if note is not None:
                _record_watcher_contribution()
                verdict = await _classify(client, {**state, "watcher_note": note})
                if verdict.degraded:
                    return Verdict(
                        level=prior,
                        confidence=0.0,
                        degraded=True,
                        degraded_reason=verdict.degraded_reason,
                    )
    else:
        _UNSURE_STREAKS.pop(run_id, None)
    return verdict


def _state(run_id: str, event: dict[str, Any], prior: Level) -> dict[str, Any]:
    from app.graph import graph

    return {
        "run_id": run_id,
        "prior_level": int(prior),
        "short_term": _tape(run_id),
        "long_term": graph.key_nodes(run_id),
        "event": event,
    }


def _tape(run_id: str) -> list[dict[str, Any]]:
    from app.runs import log

    return log.tail(run_id, settings.short_term_n)


async def _classify(client: httpx.AsyncClient, state: dict[str, Any]) -> Verdict:
    try:
        return await jev.classify(client, state)
    except Exception as exc:  # noqa: BLE001
        reason = "no_key" if "TYPESAFE_API_KEY" in str(exc) else "error"
        return Verdict(level=Level.NONE, confidence=0.0, degraded=True, degraded_reason=reason)


async def _watcher_note(
    client: httpx.AsyncClient, state: dict[str, Any], verdict: Verdict
) -> str | None:
    try:
        review = await watcher.review(
            client,
            jev.serialize_state(
                {
                    "run_id": state["run_id"],
                    "prior_level": state["prior_level"],
                    "short_term": state["short_term"],
                    "long_term": state["long_term"],
                    "event": state["event"],
                    "jev": {"level": int(verdict.level), "confidence": verdict.confidence},
                }
            ),
        )
    except Exception:  # noqa: BLE001
        return None
    return review.note
