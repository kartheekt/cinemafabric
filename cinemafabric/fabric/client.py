"""Async HTTP client for the fabric service, used by the studio and tools."""

from typing import Any

import httpx

from .types import Preference, TaskKind, TaskResult, Tier


class FabricError(RuntimeError):
    pass


class FabricClient:
    def __init__(self, base_url: str, token: str = ""):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(1800.0, connect=10.0),
                                         headers={"X-Fabric-Token": token} if token else None)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def run(
        self,
        kind: TaskKind,
        payload: dict[str, Any],
        *,
        tier: Tier = Tier.STANDARD,
        preference: Preference = Preference.LOCAL_FIRST,
        job_id: str | None = None,
        stage: str | None = None,
        label: str | None = None,
        cache: bool = True,
        max_job_usd: float | None = None,
        exclude_licences: list[str] | None = None,
    ) -> TaskResult:
        body = {
            "kind": kind.value,
            "payload": payload,
            "tier": tier.value,
            "preference": preference.value,
            "job_id": job_id,
            "stage": stage,
            "label": label,
            "cache": cache,
            "max_job_usd": max_job_usd,
            "exclude_licences": exclude_licences or [],
        }
        try:
            r = await self._client.post("/v1/tasks", json=body)
        except httpx.HTTPError as e:
            raise FabricError(f"fabric unreachable: {e.__class__.__name__}") from e
        if r.status_code != 200:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise FabricError(str(detail))
        return TaskResult.model_validate(r.json())

    async def get(self, path: str, **params) -> dict:
        try:
            r = await self._client.get(path, params=params, timeout=15.0)
        except httpx.TransportError:  # an idle connection can drop; a read is safe to repeat
            r = await self._client.get(path, params=params, timeout=15.0)
        r.raise_for_status()
        return r.json()

    async def post(self, path: str) -> dict:
        r = await self._client.post(path, timeout=15.0)
        r.raise_for_status()
        return r.json()

    async def call(self, method: str, path: str, json: dict | None = None) -> tuple[int, dict]:
        """Pass-through for admin endpoints: returns (status, body) instead of raising."""
        try:
            r = await self._client.request(method, path, json=json, timeout=30.0)
        except httpx.HTTPError as e:
            return 502, {"detail": f"fabric unreachable: {e.__class__.__name__}"}
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"detail": r.text}
