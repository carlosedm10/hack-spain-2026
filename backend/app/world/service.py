from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any
from uuid import uuid4


class DemoWorld:
    """Small stateful logistics world used only by the live demo harness."""

    def __init__(self) -> None:
        self._memory: dict[str, dict[str, Any]] = {}
        self._bookings: dict[str, dict[str, Any]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def clear(self) -> None:
        with self._lock:
            self._memory.clear()
            self._bookings.clear()
            self._jobs.clear()

    def execute(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if tool == "memory_write":
                key = str(args["key"])
                previous = deepcopy(self._memory.get(key))
                self._memory[key] = {
                    "value": args["value"],
                    "provenance": args.get("provenance", "agent"),
                }
                return {"key": key, "previous": previous}
            if tool == "book_load":
                load_id = str(args["load_id"])
                booking_id = str(args.get("booking_id") or f"B-{uuid4().hex[:8]}")
                self._bookings[booking_id] = {
                    "booking_id": booking_id,
                    "load_id": load_id,
                    "status": "booked",
                }
                return {"booking_id": booking_id, "load_id": load_id}
            if tool == "schedule_callback":
                job_id = str(args.get("job_id") or f"J-{uuid4().hex[:8]}")
                self._jobs[job_id] = {
                    "job_id": job_id,
                    "counterparty": args["counterparty"],
                    "status": "scheduled",
                }
                return {"job_id": job_id}
            raise ValueError(f"unknown demo tool: {tool}")

    def compensate(self, template: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if template == "tombstone_memory":
                key = str(params["key"])
                previous = params.get("previous")
                if previous is None:
                    self._memory.pop(key, None)
                else:
                    self._memory[key] = deepcopy(previous)
                return {"key": key, "state": "restored"}
            if template == "cancel_booking":
                booking_id = str(params["booking_id"])
                booking = self._bookings.get(booking_id)
                if booking is not None:
                    booking["status"] = "cancelled"
                return {"booking_id": booking_id, "state": "cancelled"}
            if template == "unschedule":
                job_id = str(params["job_id"])
                job = self._jobs.get(job_id)
                if job is not None:
                    job["status"] = "cancelled"
                return {"job_id": job_id, "state": "cancelled"}
            if template in {"restore_file", "remove_tool"}:
                return {"state": "requires_host_playbook"}
            raise ValueError(f"unknown counter template: {template}")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "memory": deepcopy(self._memory),
                "bookings": deepcopy(self._bookings),
                "jobs": deepcopy(self._jobs),
            }


world = DemoWorld()
