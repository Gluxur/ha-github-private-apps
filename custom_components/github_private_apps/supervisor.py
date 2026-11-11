"""Minimal Supervisor REST client.

Talks to the Supervisor with the credentials Home Assistant Core already
has (SUPERVISOR / SUPERVISOR_TOKEN environment variables), so it does not
depend on internal helpers of the hassio integration that change between
Core releases.
"""

from __future__ import annotations

import os
from typing import Any

from aiohttp import ClientSession, ClientTimeout


class SupervisorError(Exception):
    """Supervisor API call failed."""


class SupervisorClient:
    """Calls the few Supervisor endpoints this integration needs."""

    def __init__(self, session: ClientSession) -> None:
        host = os.environ.get("SUPERVISOR")
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not host or not token:
            raise SupervisorError("No Supervisor available")
        self._base = f"http://{host}"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._session = session

    async def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        async with self._session.request(
            method,
            self._base + path,
            json=payload,
            headers=self._headers,
            timeout=ClientTimeout(total=300),
        ) as resp:
            body = await resp.json(content_type=None)
        if not isinstance(body, dict) or body.get("result") != "ok":
            message = body.get("message") if isinstance(body, dict) else None
            raise SupervisorError(message or f"{method} {path} failed")
        return body.get("data")

    async def repositories(self) -> list[dict[str, Any]]:
        """Return store repositories (slug, source, name...)."""
        return await self._call("get", "/store/repositories")

    async def add_repository(self, url: str) -> None:
        """Add a repository to the store (clones it)."""
        await self._call("post", "/store/repositories", {"repository": url})

    async def remove_repository(self, slug: str) -> None:
        """Remove a repository from the store."""
        await self._call("delete", f"/store/repositories/{slug}")

    async def reload_store(self) -> None:
        """Pull every repository and refresh available versions."""
        await self._call("post", "/store/reload")

    async def installed_addons(self) -> list[dict[str, Any]]:
        """Return installed apps (each has 'slug' and 'repository')."""
        data = await self._call("get", "/addons")
        return data.get("addons", [])

    async def registries(self) -> dict[str, dict[str, Any]]:
        """Return configured registries {host: {username}}."""
        data = await self._call("get", "/docker/registries")
        return data.get("registries", data) or {}

    async def set_registry(self, host: str, username: str, password: str) -> None:
        """Add or replace a registry credential."""
        await self._call(
            "post",
            "/docker/registries",
            {host: {"username": username, "password": password}},
        )
