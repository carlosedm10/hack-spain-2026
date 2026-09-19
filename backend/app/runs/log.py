from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.config import settings
from app.events import MonitorEvent

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_LOCKS: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


def _path(run_id: str) -> Path:
    return Path(settings.run_log_dir) / f"{_UNSAFE.sub('_', run_id)}.jsonl"


def append(run_id: str, event: dict[str, Any]) -> None:
    path = _path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def count(run_id: str) -> int:
    path = _path(run_id)
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def append_event(event: MonitorEvent) -> tuple[MonitorEvent, bool]:
    """Atomically assign a per-run sequence and append unless event.id already exists."""
    with _LOCKS[event.run_id]:
        existing = tail(event.run_id, 1_000_000)
        for row in existing:
            if row.get("id") == event.id:
                return MonitorEvent.model_validate(row), False
        stored = event.model_copy(update={"sequence": len(existing) + 1})
        append(stored.run_id, stored.model_dump(mode="json"))
        return stored, True


def tail(run_id: str, n: int) -> list[dict[str, Any]]:
    path = _path(run_id)
    if not path.exists() or n <= 0:
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[-n:] if line.strip()]


def list_runs() -> list[str]:
    root = Path(settings.run_log_dir)
    if not root.exists():
        return []
    return sorted(path.stem for path in root.glob("*.jsonl"))
