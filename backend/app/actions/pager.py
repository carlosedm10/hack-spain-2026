from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.actions.call_status import CallResult, derive_api_base, map_call
from app.actions.types import PagerTransition

HOOK_TIMEOUT = httpx.Timeout(30.0)
HOLD_THE_LINE = (
    " Speak slowly and clearly. Pause after every sentence."
    " Keep this person on the line for at least one minute."
    " Do not hang up until they confirm they understood."
    " If they stay silent, wait five seconds and repeat the status once."
)


def _default_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HOOK_TIMEOUT)


def voice_pautas(action_taken: str) -> str:
    return f"{action_taken.rstrip('.')}." + HOLD_THE_LINE


class PagerError(RuntimeError):
    def __init__(self, message: str, error_code: str = "happyrobot_error") -> None:
        super().__init__(message)
        self.error_code = error_code


class HappyRobotPager:
    def __init__(
        self,
        *,
        hook_url: str,
        api_key: str,
        api_base: str | None,
        phone: str,
        name: str,
        poll_interval: float,
        poll_timeout: float,
        client: httpx.AsyncClient | None = None,
        client_factory: Callable[[], httpx.AsyncClient] = _default_http_client,
    ) -> None:
        self._hook_url = hook_url
        self._api_key = api_key
        self._api_base = derive_api_base(hook_url, api_base)
        self._phone = phone
        self._name = name
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._client = client
        self._client_factory = client_factory

    async def page(
        self,
        level: int,
        incident_id: str,
        intent: str,
        action_taken: str,
        transition: PagerTransition,
    ) -> None:
        try:
            self._validate()
            await transition("running", call_status="queued")
            client = self._client or self._client_factory()
            owns_client = self._client is None
            try:
                run_id = await self._start_call(
                    client,
                    level,
                    incident_id,
                    intent or "critical agent activity",
                    action_taken or f"Escalated level {level} response",
                    0,
                )
                result = await self._poll_call(client, run_id, transition)
                await self._report_terminal(result, transition)
            finally:
                if owns_client:
                    await client.aclose()
        except PagerError as exc:
            await transition(
                "failed",
                detail=str(exc),
                error_code=exc.error_code,
                call_status="failed",
            )
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            await transition(
                "failed",
                detail=str(exc),
                error_code="happyrobot_error",
                call_status="failed",
            )

    def _validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("hook URL", self._hook_url),
                ("API key", self._api_key),
                ("phone", self._phone),
            )
            if not value
        ]
        if missing:
            raise PagerError(
                f"Missing HappyRobot configuration: {', '.join(missing)}",
                "pager_configuration",
            )
        if self._poll_interval <= 0 or self._poll_timeout <= 0:
            raise PagerError("Invalid HappyRobot polling configuration", "pager_configuration")

    async def _start_call(
        self,
        client: httpx.AsyncClient,
        level: int,
        incident_id: str,
        intent: str,
        action_taken: str,
        call_attempt: int,
    ) -> str:
        payload = {
            "tipo_emergencia": f"{intent} (level {level}, run {incident_id})",
            "pautas": voice_pautas(action_taken),
            "nivel_gravedad": str(level),
            "nombre_contacto": self._name,
            "telefono": self._phone,
            "nodos": (
                f"{(intent or 'actividad peligrosa').replace('_', ' ')}. "
                f"Nivel {level}. {action_taken.rstrip('.')}."
            ),
        }
        response = await self._request(
            client,
            "POST",
            self._hook_url,
            json=payload,
            request_headers={
                "Idempotency-Key": self._idempotency_key(
                    incident_id, level, call_attempt
                )
            },
        )
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        run_id = (
            body.get("run_id") or body.get("id")
            if isinstance(body, dict)
            else None
        )
        if not run_id and isinstance(data, dict):
            run_id = data.get("run_id") or data.get("id")
        if not run_id:
            raise PagerError("HappyRobot response did not include run_id", "missing_run_id")
        return str(run_id)

    async def _poll_call(
        self,
        client: httpx.AsyncClient,
        run_id: str,
        transition: PagerTransition,
    ) -> CallResult:
        deadline = time.monotonic() + self._poll_timeout
        previous_status = "queued"
        while time.monotonic() < deadline:
            run = self._data(
                (await self._request(client, "GET", f"{self._api_base}/runs/{run_id}")).json()
            )
            nodes_payload = (
                await self._request(client, "GET", f"{self._api_base}/runs/{run_id}/nodes")
            ).json()
            output = await self._call_output(client, run_id, nodes_payload)
            sessions_payload = (
                await self._request(client, "GET", f"{self._api_base}/runs/{run_id}/sessions")
            ).json()
            sessions = self._items(sessions_payload)
            session = sessions[0] if sessions else None
            result = map_call(run, session, output)
            if result.call_status in {"queued", "ringing", "answered"}:
                if result.call_status != previous_status:
                    await transition("running", call_status=result.call_status)
                    previous_status = result.call_status
                if self._poll_interval:
                    await asyncio.sleep(self._poll_interval)
                continue
            return result
        raise PagerError("HappyRobot call polling timed out", "pager_timeout")

    async def _call_output(
        self,
        client: httpx.AsyncClient,
        run_id: str,
        nodes_payload: Any,
    ) -> dict[str, Any] | None:
        for node in self._items(nodes_payload):
            name = str(node.get("name") or "").lower()
            if "llamada" not in name and node.get("node_type") != "action":
                continue
            output_id = node.get("output_id")
            if not output_id:
                continue
            payload = (
                await self._request(
                    client,
                    "GET",
                    f"{self._api_base}/runs/{run_id}/outputs/{output_id}",
                )
            ).json()
            output = self._data(payload)
            if isinstance(output.get("data"), dict):
                output = output["data"]
            if any(
                key in output
                for key in ("sip_code", "call_end_event", "session_id", "failure_reason")
            ):
                return output
        return None

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        request_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        headers.update(request_headers or {})
        for attempt in range(2):
            response = await client.request(method, url, headers=headers, **kwargs)
            if response.status_code < 500 or attempt == 1:
                break
        if response.status_code >= 500:
            raise PagerError(
                f"HappyRobot returned HTTP {response.status_code}",
                "happyrobot_5xx",
            )
        if response.is_error:
            raise PagerError(
                f"HappyRobot returned HTTP {response.status_code}",
                "happyrobot_http_error",
            )
        return response

    @staticmethod
    def _idempotency_key(incident_id: str, level: int, call_attempt: int) -> str:
        material = f"hackspain-pager:{incident_id}:{level}:{call_attempt}".encode()
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    async def _report_terminal(result: CallResult, transition: PagerTransition) -> None:
        if result.call_status == "hung_up":
            await transition("ok", call_status="hung_up")
            return
        error_code = result.error_code or (
            "no_pickup" if result.call_status == "no_pickup" else "happyrobot_call_failed"
        )
        await transition(
            "failed",
            error_code=error_code,
            call_status=result.call_status,
        )

    @staticmethod
    def _data(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        data = payload.get("data")
        return data if isinstance(data, dict) and "status" in data else payload

    @staticmethod
    def _items(payload: Any) -> list[dict[str, Any]]:
        data = payload.get("data") if isinstance(payload, dict) else None
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
