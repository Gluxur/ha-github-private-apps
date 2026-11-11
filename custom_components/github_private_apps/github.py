"""GitHub device flow and REST helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiohttp import ClientSession

from .const import GITHUB_API, GITHUB_DEVICE_CODE_URL, GITHUB_SCOPES, GITHUB_TOKEN_URL


class GitHubError(Exception):
    """GitHub call failed."""


class GitHubAuthError(GitHubError):
    """Token is invalid or revoked."""


@dataclass
class DeviceCode:
    """Device flow code shown to the user."""

    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


class GitHubDeviceFlow:
    """OAuth device flow (RFC 8628) against GitHub."""

    def __init__(self, session: ClientSession, client_id: str) -> None:
        self._session = session
        self._client_id = client_id

    async def start(self) -> DeviceCode:
        """Request a device and user code."""
        async with self._session.post(
            GITHUB_DEVICE_CODE_URL,
            data={"client_id": self._client_id, "scope": GITHUB_SCOPES},
            headers={"Accept": "application/json"},
        ) as resp:
            body = await resp.json(content_type=None)
        if "device_code" not in body:
            raise GitHubError(body.get("error_description") or body.get("error") or "device flow refused")
        return DeviceCode(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_uri=body["verification_uri"],
            interval=int(body.get("interval", 5)),
            expires_in=int(body.get("expires_in", 900)),
        )

    async def poll(self, device_code: str) -> tuple[str | None, str | None]:
        """Poll once. Returns (token, error). error is None or a GitHub error code."""
        async with self._session.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": self._client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
        ) as resp:
            body = await resp.json(content_type=None)
        return body.get("access_token"), body.get("error")


class GitHubClient:
    """Small authenticated GitHub REST client."""

    def __init__(self, session: ClientSession, token: str) -> None:
        self._session = session
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[int, Any]:
        async with self._session.get(GITHUB_API + path, headers=self._headers, params=params) as resp:
            if resp.status == 401:
                raise GitHubAuthError("GitHub token rejected")
            body = await resp.json(content_type=None) if resp.status != 204 else None
            return resp.status, body

    async def user(self) -> dict[str, Any]:
        """Return the authenticated user."""
        status, body = await self._get("/user")
        if status != 200:
            raise GitHubError(f"GET /user returned {status}")
        return body

    async def private_repositories(self) -> list[str]:
        """Return full names of private repositories the user can read."""
        names: list[str] = []
        page = 1
        while True:
            status, body = await self._get(
                "/user/repos",
                {
                    "visibility": "private",
                    "affiliation": "owner,collaborator,organization_member",
                    "sort": "full_name",
                    "per_page": 100,
                    "page": page,
                },
            )
            if status != 200:
                raise GitHubError(f"GET /user/repos returned {status}")
            names.extend(repo["full_name"] for repo in body)
            if len(body) < 100:
                return names
            page += 1

    async def is_app_repository(self, full_name: str) -> bool:
        """True if the repo has repository.yaml/yml/json at its root."""
        for name in ("repository.yaml", "repository.yml", "repository.json"):
            status, _ = await self._get(f"/repos/{full_name}/contents/{name}")
            if status == 200:
                return True
        return False
