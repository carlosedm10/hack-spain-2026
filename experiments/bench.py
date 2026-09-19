from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from app.classification import jev, watcher
from app.classification.models import Level, Verdict
from app.config import settings
from app.events import normalize_event, redact_event
from app.graph import graph
from app.monitor import monitor
from app.runs import log

try:
    from . import chains
except ImportError:
    import chains

HERE = Path(__file__).parent
_LEVEL_NAMES = ("benign", "mild", "moderate", "severe", "critical", "catastrophic")
LEVELS = [f"level_{i}_{n}" for i, n in enumerate(_LEVEL_NAMES)]

MEMORY_CHAINS = ["attack", "benign", "ambiguous", "subtle_exfil", "slow_burn",
                 "interleaved", "reordered", "long_benign"]
TAIL_CHAINS = {"attack", "slow_burn", "interleaved", "ambiguous", "subtle_exfil"}
PIPELINE_CHAINS = ["long_benign_60", "dense_attack_40", "early_attack_50", "slow_burn_50",
                   "sparse_1in10_50", "ambiguous_40", "subtle_long_50"]
MODELS = ["qwen3.6", "gemma4", "deepseek-v4-flash"]
REPS = 2
RETRIES = 3
BACKOFF_S = 3.0


async def _measure(op: Callable[[], Awaitable[Any]]) -> tuple[Any, float, Exception | None]:
    for attempt in range(RETRIES):
        t0 = time.perf_counter()
        try:
            result = await op()
            return result, time.perf_counter() - t0, None
        except Exception as e:  # noqa: BLE001
            if attempt == RETRIES - 1:
                return None, time.perf_counter() - t0, e
            await asyncio.sleep(BACKOFF_S)
    raise AssertionError("unreachable")


def _reset() -> None:
    graph.clear()
    monitor.clear()


def _payload(run_id: str, event: dict[str, Any]):
    normalized = redact_event(normalize_event(run_id, event))
    return normalized, normalized.model_dump(mode="json")


def _state(run_id: str, event: dict[str, Any], monitor_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "prior_level": int(graph.level(run_id)),
        "short_term": log.tail(run_id, settings.short_term_n),
        "long_term": graph.key_nodes(run_id),
        "event": event,
        "monitor": monitor_context,
    }


def begin_event(run_id: str, event: dict[str, Any]):
    """Normalize, tape, and prepare monitor signals without committing history."""
    normalized, payload = _payload(run_id, event)
    log.append(run_id, payload)
    prepared = monitor.prepare(normalized)
    return normalized, prepared, _state(run_id, payload, prepared.context)


def commit_prefix(run_id: str, event: dict[str, Any]) -> None:
    """Advance Sentinel/drift history for a window prefix without calling jev."""
    normalized, prepared, _ = begin_event(run_id, event)
    monitor.finalize(
        normalized,
        Verdict(level=Level.NONE, confidence=1.0),
        Level.NONE,
        prepared,
    )


def window_state(run_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Shipped jev state for an independent window: tape + policy/drift/Sentinel."""
    _reset()
    if not events:
        return _state(run_id, {}, {"policy": {}, "drift": {}, "sentinel_findings": [],
                                   "policy_violations": []})
    for event in events[:-1]:
        commit_prefix(run_id, event)
    _, _, state = begin_event(run_id, events[-1])
    return state


def commit_classified(normalized, prepared, verdict: Verdict) -> None:
    prior = graph.level(normalized.run_id)
    if not verdict.degraded:
        graph.append(
            normalized.run_id,
            level=verdict.level,
            threshold=verdict.confidence,
            intent=verdict.intent,
            event=normalized.model_dump(mode="json"),
        )
    monitor.finalize(normalized, verdict, prior, prepared)


async def jev_ask(
    client: httpx.AsyncClient, run_id: str, events: list[dict]
) -> tuple[Any, float, Exception | None]:
    state = window_state(run_id, events)
    return await _measure(lambda: jev.classify(client, state))


async def watcher_ask(
    client: httpx.AsyncClient, state: dict[str, Any]
) -> tuple[Any, float, Exception | None]:
    return await _measure(lambda: watcher.review(client, jev.serialize_state(state)))


async def run_memory(client: httpx.AsyncClient, only: set[str]) -> list[dict]:
    rows = []
    for name in MEMORY_CHAINS:
        if only and name not in only:
            continue
        chain = chains.CHAINS[name]
        n = len(chain)
        for mode in ("lead", "tail"):
            if mode == "tail" and name not in TAIL_CHAINS:
                continue
            for k in range(1, n + 1):
                events = chain[:k] if mode == "lead" else chain[-k:]
                verdict, lat, err = await jev_ask(
                    client, f"bench:{name}:{mode}:{k}", events
                )
                row = {"chain": name, "mode": mode, "k": k, "n_events": n}
                if err is not None:
                    print(f"{name} {mode} k={k}: {err}")
                    rows.append(row)
                    continue
                if verdict.degraded:
                    raise RuntimeError(
                        f"jev degraded on {name} {mode} k={k}; refusing to overwrite artifacts"
                    )
                row.update(
                    latency_s=round(lat, 3),
                    choice=row_choice(verdict),
                    confidence=round(verdict.confidence, 4),
                    **{f"p_{lvl}": round(verdict.probabilities.get(lvl, 0), 4) for lvl in LEVELS},
                )
                rows.append(row)
                top2 = sorted(verdict.probabilities.items(), key=lambda kv: -kv[1])[:2]
                probs = "  ".join(f"{l}={p:.2f}" for l, p in top2)
                print(f"{name:12s} {mode} k={k:2d}  {row['choice']:22s} "
                      f"conf={verdict.confidence:.3f} | {probs}")
    return rows


async def run_pipeline(client: httpx.AsyncClient, only: set[str]) -> list[dict]:
    rows = []
    for name in PIPELINE_CHAINS:
        if only and name not in only:
            continue
        chain = chains.CHAINS[name]
        _reset()
        run_id = f"bench:{name}"
        for k, event in enumerate(chain, start=1):
            row = {"chain": name, "k": k, "n_events": len(chain)}
            normalized, prepared, state = begin_event(run_id, event)
            verdict, lat, err = await _measure(lambda s=state: jev.classify(client, s))
            if err is not None or verdict is None or verdict.degraded:
                if verdict is not None and verdict.degraded:
                    raise RuntimeError(
                        f"jev degraded on {name} k={k}; refusing to overwrite artifacts"
                    )
                row.update(jev_choice="", jev_conf="", jev_lat="", note=f"jev: {err}")
                monitor.finalize(
                    normalized, Verdict(level=Level.NONE, confidence=0.0),
                    graph.level(run_id), prepared,
                )
            else:
                row.update(jev_choice=row_choice(verdict),
                           jev_conf=round(verdict.confidence, 4), jev_lat=round(lat, 3))
                commit_classified(normalized, prepared, verdict)
            conf = row.get("jev_conf", "")
            if conf != "" and conf < settings.watcher_tau:
                ds, dlat, ds_err = await watcher_ask(client, state)
                if ds_err is not None or ds is None:
                    row.update(ds_level="", ds_escalate="", ds_ok=False, ds_lat="",
                               note=f"ds: {ds_err}")
                else:
                    row.update(ds_level=ds.suspected_level or "", ds_escalate=ds.escalate,
                               ds_ok=ds.suspected_level is not None, ds_lat=round(dlat, 3))
            rows.append(row)
            print(f"{name:16s} k={k:2d}  jev={row.get('jev_choice') or '-':22s} "
                  f"conf={row.get('jev_conf', '')!s:5} ds={row.get('ds_level', '-')!s:2}")
    return rows


async def run_models(client: httpx.AsyncClient, only: set[str]) -> list[dict]:
    rows = []
    for model in MODELS:
        if only and model not in only:
            continue
        settings.supervisor_model = model
        for rep in range(REPS):
            for name in chains.CHAINS:
                verdict, lat, err = await watcher_ask(
                    client, window_state(f"bench:models:{name}", chains.CHAINS[name])
                )
                row = {"model": model, "chain": name, "rep": rep}
                if err is not None:
                    row.update(latency_s="", tok_per_s="", json_ok=False,
                               verdict=str(err), esc_ok=False, lvl_ok=False, level="")
                else:
                    exp_esc, exp_lvl = chains.EXPECTED[name]
                    ok = verdict.suspected_level is not None
                    row.update(
                        latency_s=round(lat, 2),
                        json_ok=ok,
                        verdict=f"{verdict.escalate} L{verdict.suspected_level} {verdict.note}",
                        esc_ok=ok and verdict.escalate == exp_esc,
                        lvl_ok=ok and verdict.suspected_level in exp_lvl,
                        level=verdict.suspected_level if ok else "",
                    )
                rows.append(row)
                print(f"{model:18s} {name:13s} rep{rep} lvl={row.get('level', '')!s:2} "
                      f"json={row['json_ok']} esc_ok={row['esc_ok']} lvl_ok={row['lvl_ok']} "
                      f"{row['latency_s']}s")
    return rows


def row_choice(verdict: Any) -> str:
    return f"level_{verdict.level.value}_{_LEVEL_NAMES[verdict.level.value]}"


def _merge(old_path: Path, rows: list[dict], key_field: str, only: set[str]) -> list[dict]:
    if only and old_path.exists():
        old = [r for r in csv.DictReader(old_path.open()) if r[key_field] not in only]
        return old + rows
    return rows


def _write(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {path} ({len(rows)} rows)")


MEMORY_FIELDS = ["chain", "mode", "k", "n_events", "latency_s", "choice", "confidence",
                 *[f"p_{l}" for l in LEVELS]]
PIPELINE_FIELDS = ["chain", "k", "n_events", "jev_choice", "jev_conf", "jev_lat",
                   "ds_level", "ds_escalate", "ds_ok", "ds_lat", "note"]
MODEL_FIELDS = ["model", "chain", "rep", "latency_s", "json_ok", "verdict",
                "esc_ok", "lvl_ok", "level"]


async def main() -> None:
    parser = argparse.ArgumentParser(description="jev + watcher benchmarks via the shipped client")
    parser.add_argument("mode", choices=["memory", "pipeline", "models"], default="pipeline",
                        nargs="?")
    parser.add_argument("subset", nargs="*", help="chain or model names to (re-)run")
    args = parser.parse_args()
    only = set(args.subset)
    logging.basicConfig(level=logging.WARNING)
    settings.run_log_dir = tempfile.mkdtemp(prefix="bench-tape-")

    async with httpx.AsyncClient() as client:
        if args.mode == "memory":
            rows = _merge(HERE / "results.csv", await run_memory(client, only), "chain", only)
            _write(HERE / "results.csv", rows, MEMORY_FIELDS)
        elif args.mode == "pipeline":
            rows = _merge(HERE / "pipeline_results.csv", await run_pipeline(client, only),
                          "chain", only)
            _write(HERE / "pipeline_results.csv", rows, PIPELINE_FIELDS)
        else:
            rows = _merge(HERE / "helmcode_results.csv", await run_models(client, only),
                          "model", only)
            _write(HERE / "helmcode_results.csv", rows, MODEL_FIELDS)


if __name__ == "__main__":
    asyncio.run(main())
