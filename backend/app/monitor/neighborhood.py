from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.events import MonitorEvent

RUN_WINDOW = 100

_MEMORY_KEY_FIELDS = ("memory_key", "memory_keys", "entity_id", "entity_ids", "memory_id")


def memory_keys(event: MonitorEvent) -> set[str]:
    return _memory_keys(event)


def linked_history(
    event: MonitorEvent,
    histories_by_run: Mapping[str, Sequence[MonitorEvent]],
) -> list[MonitorEvent]:
    """Return the current run window plus other-run events that share a real link.

    Other runs are not included merely because they live in the same process.
    A link is: non-empty target, derived_from/caused_by either way, same
    agent+target, shared memory/entity ids, or same tool+target.
    """
    current_run = list(histories_by_run.get(event.run_id, []))[-RUN_WINDOW:]
    seeds = [*current_run, event]
    targets = {item.target for item in seeds if item.target}
    agent_targets = {(item.agent, item.target) for item in seeds if item.agent and item.target}
    tool_targets = {(item.tool, item.target) for item in seeds if item.tool and item.target}
    ids = _id_closure(seeds)
    memory_keys = set().union(*(_memory_keys(item) for item in seeds)) if seeds else set()

    neighborhood = list(current_run)
    seen = {item.id for item in neighborhood}
    for run_id, run_history in histories_by_run.items():
        if run_id == event.run_id:
            continue
        for item in list(run_history)[-RUN_WINDOW:]:
            if item.id in seen:
                continue
            if _is_linked(item, ids, targets, agent_targets, tool_targets, memory_keys):
                neighborhood.append(item)
                seen.add(item.id)
    return neighborhood


def _id_closure(events: Sequence[MonitorEvent]) -> set[str]:
    ids: set[str] = set()
    for item in events:
        ids.add(item.id)
        ids.update(item.derived_from)
        ids.update(item.caused_by)
    return ids


def _memory_keys(event: MonitorEvent) -> set[str]:
    keys: set[str] = set()
    for container in (event.metadata, event.args):
        for field in _MEMORY_KEY_FIELDS:
            _collect_ids(keys, container.get(field))
    return keys


def _collect_ids(into: set[str], value: object) -> None:
    if isinstance(value, str) and value:
        into.add(value)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, str) and item:
                into.add(item)


def _is_linked(
    item: MonitorEvent,
    ids: set[str],
    targets: set[str],
    agent_targets: set[tuple[str | None, str | None]],
    tool_targets: set[tuple[str | None, str | None]],
    memory_keys: set[str],
) -> bool:
    if item.target and item.target in targets:
        return True
    if item.agent and item.target and (item.agent, item.target) in agent_targets:
        return True
    if item.tool and item.target and (item.tool, item.target) in tool_targets:
        return True
    if item.id in ids:
        return True
    if any(ref in ids for ref in item.derived_from) or any(ref in ids for ref in item.caused_by):
        return True
    return bool(memory_keys and _memory_keys(item) & memory_keys)
