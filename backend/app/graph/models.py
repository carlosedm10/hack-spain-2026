from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.classification.models import Level


@dataclass
class Node:
    id: str
    neighbors: list[Node] = field(default_factory=list)
    threshold: float = 0.0
    tool: Any | None = None
    run_id: str | None = None
    run_ids: set[str] = field(default_factory=set)
    visit_count: int = 1
    signature: str | None = None
    level: Level = Level.NONE
    intent: str | None = None
    event: dict[str, Any] | None = None
    action_id: str | None = None
    created_at: datetime | None = None
