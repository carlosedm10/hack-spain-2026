from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.evals import benchmarks
from app.evals.replay import (
    catalog,
    conversation_script,
    default_lab_selection,
    find_cluster,
    find_trace,
    new_run_id,
    paced_replay,
)

router = APIRouter()
_STATIC = Path(__file__).resolve().parent / "static"


class ReplayIn(BaseModel):
    cluster_id: str | None = None
    trace_id: str | None = None
    delay_ms: int = Field(default=800, ge=0, le=10_000)


@router.get("/clusters")
def list_clusters() -> dict[str, Any]:
    default_cluster, default_trace = default_lab_selection()
    return {
        "clusters": catalog(),
        "default_cluster_id": default_cluster,
        "default_trace_id": default_trace,
    }


@router.get("/script")
def get_script(
    cluster_id: str = Query(...),
    trace_id: str = Query(...),
) -> dict[str, Any]:
    try:
        cluster = find_cluster(cluster_id)
        trace = find_trace(cluster, trace_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown cluster or trace") from None
    return conversation_script(cluster, trace)


@router.post("/replay")
async def start_replay(body: ReplayIn, background: BackgroundTasks) -> dict[str, Any]:
    cluster_id = body.cluster_id
    trace_id = body.trace_id
    if cluster_id is None or trace_id is None:
        cluster_id, trace_id = default_lab_selection()
    try:
        cluster = find_cluster(cluster_id)
        trace = find_trace(cluster, trace_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown cluster or trace") from None

    run_id = new_run_id(cluster.id, trace.id)

    async def _run() -> None:
        async with httpx.AsyncClient() as client:
            await paced_replay(
                run_id,
                cluster,
                trace,
                delay_ms=body.delay_ms,
                client=client,
            )

    background.add_task(_run)
    return {
        "run_id": run_id,
        "cluster_id": cluster.id,
        "trace_id": trace.id,
        "event_count": len(trace.events),
        "stream_url": f"/api/runs/{run_id}/stream",
        "oracle": {
            "label": trace.oracle.label.value,
            "expected_level": trace.oracle.expected_level,
            "classification_checkpoint": trace.oracle.classification_checkpoint,
        },
        "script": conversation_script(cluster, trace),
    }


@router.get("/benchmarks")
def get_benchmarks() -> dict[str, Any]:
    return benchmarks.load_report()


@router.post("/benchmarks")
async def start_benchmarks(
    repeats: int = Query(default=3, ge=1, le=5),
    smoke: bool = Query(default=False),
    hpo: bool = Query(default=False),
    corpus: str = Query(default="sentinel"),
) -> dict[str, Any]:
    status = await benchmarks.start_job(
        repeats=repeats,
        smoke=smoke,
        hpo=hpo,
        corpus=corpus,
    )
    return {"status": status, "report": benchmarks.load_report()}


def _page(name: str) -> FileResponse:
    path = _STATIC / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"{name} not found")
    media = "text/css" if name.endswith(".css") else "text/html"
    return FileResponse(path, media_type=media)


def lab_page() -> FileResponse:
    return _page("lab.html")


def lab_conversation_page() -> FileResponse:
    return _page("conversation.html")


def lab_benchmarks_page() -> FileResponse:
    return _page("benchmarks.html")


def lab_inspector_page() -> FileResponse:
    return _page("inspector.html")


def lab_css() -> FileResponse:
    return _page("lab.css")
