from __future__ import annotations

from dataclasses import dataclass, field

from app.events import IdentityState, MonitorEvent, Reversibility


@dataclass(frozen=True)
class ToolPolicy:
    effect: Reversibility = Reversibility.UNKNOWN
    requires_identity: IdentityState | None = None
    counter_action: str | None = None
    max_scope: int | None = None
    max_amount: float | None = None


@dataclass(frozen=True)
class AgentPolicy:
    version: str
    goal: str
    tools: dict[str, ToolPolicy] = field(default_factory=dict)
    forbidden_tools: frozenset[str] = frozenset()


DEFAULT_POLICY = AgentPolicy(
    version="demo-agent:v1",
    goal="Work only inside the assigned sandbox task.",
    tools={
        "read_file": ToolPolicy(effect=Reversibility.REVERSIBLE),
        "write_file": ToolPolicy(effect=Reversibility.COMPENSABLE, counter_action="restore_file"),
        "http_request": ToolPolicy(effect=Reversibility.IRREVERSIBLE),
        "shell": ToolPolicy(effect=Reversibility.UNKNOWN),
        "register_tool": ToolPolicy(effect=Reversibility.COMPENSABLE, counter_action="remove_tool"),
        "run_tool": ToolPolicy(effect=Reversibility.UNKNOWN),
        "assistant_message": ToolPolicy(effect=Reversibility.IRREVERSIBLE),
        "memory_write": ToolPolicy(
            effect=Reversibility.COMPENSABLE, counter_action="tombstone_memory"
        ),
        "book_load": ToolPolicy(
            effect=Reversibility.COMPENSABLE,
            requires_identity=IdentityState.VERIFIED,
            counter_action="cancel_booking",
            max_scope=1,
        ),
        "schedule_callback": ToolPolicy(
            effect=Reversibility.COMPENSABLE,
            counter_action="unschedule",
            max_scope=1,
        ),
    },
    forbidden_tools=frozenset({"docker", "raw_socket"}),
)


def policy_violations(event: MonitorEvent, policy: AgentPolicy = DEFAULT_POLICY) -> list[str]:
    if not event.tool:
        return []
    reasons: list[str] = []
    if event.tool in policy.forbidden_tools:
        reasons.append("forbidden_tool")
    tool_policy = policy.tools.get(event.tool)
    if tool_policy is None:
        reasons.append("unknown_tool")
        return reasons
    if (
        tool_policy.requires_identity is not None
        and event.identity_state != tool_policy.requires_identity
    ):
        reasons.append("identity_requirement_failed")
    if tool_policy.max_scope is not None and event.effect.scope > tool_policy.max_scope:
        reasons.append("scope_limit_exceeded")
    if (
        tool_policy.max_amount is not None
        and event.effect.amount is not None
        and event.effect.amount > tool_policy.max_amount
    ):
        reasons.append("amount_limit_exceeded")
    return reasons
