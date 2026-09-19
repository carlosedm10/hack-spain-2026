from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.world import world

router = APIRouter()


class ToolCall(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)


@router.post("/{run_id}/tools/{tool}")
def execute_tool(run_id: str, tool: str, call: ToolCall) -> dict[str, Any]:
    try:
        result = world.execute(tool, call.args)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"run_id": run_id, "tool": tool, "result": result}


@router.get("/state")
def get_world_state() -> dict[str, Any]:
    return world.snapshot()
