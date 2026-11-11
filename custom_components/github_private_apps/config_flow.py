"""Config and options flows for GitHub Private Apps."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import os
import secrets
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import relay_url, store_slugs
from .const import (
    CONF_CLIENT_ID,
    CONF_LOGIN,
    CONF_RELAY_SECRET,
    CONF_REPOSITORIES,
    CONF_TOKEN,
    CONF_USER_ID,
    DEFAULT_CLIENT_ID,
    DOMAIN,
    READONLY_DOCS_URL,
)
from .github import DeviceCode, GitHubAuthError, GitHubClient, GitHubDeviceFlow, GitHubError
from .supervisor import SupervisorClient, SupervisorError


def _repo_selector(options: list[str]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST)
    )


async def _check_selection(
    client: GitHubClient, names: list[str]
) -> tuple[list[str], dict[str, str], str]:
    """Check that every selected repository is an app repository.

    Returns (repositories, form errors, comma-separated invalid names).
    """
    checks = await asyncio.gather(
        *(client.is_app_repository(n) for n in names), return_exceptions=True
    )
    if any(isinstance(c, GitHubAuthError) for c in checks):
        return [], {"base": "invalid_auth"}, ""
    for check in checks:
        if isinstance(check, GitHubError):
            return [], {"base": "cannot_connect"}, ""
        if isinstance(check, BaseException):
            raise check
    bad = [n for n, ok in zip(names, checks, strict=True) if not ok]
    if bad:
        return [], {CONF_REPOSITORIES: "not_app_repository"}, ", ".join(bad)
    return names, {}, ""


class GitHubPrivateAppsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in to GitHub with the device flow, then pick repositories."""

    VERSION = 1

    def __init__(self) -> None:
        self._client_id: str = DEFAULT_CLIENT_ID
        self._device: DeviceCode | None = None
        self._login_task: asyncio.Task[str] | None = None
        self._token: str | None = None
        self._user: dict[str, Any] = {}
        self._candidates: list[str] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GitHubPrivateAppsOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not os.environ.get("SUPERVISOR"):
            return self.async_abort(reason="no_supervisor")
        return self.async_show_menu(
            step_id="user",
            menu_options=["device", "token"],
            description_placeholders={"docs_url": READONLY_DOCS_URL},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._client_id = entry_data.get(CONF_CLIENT_ID) or DEFAULT_CLIENT_ID
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reauth_confirm",
            menu_options=["device", "token"],
            description_placeholders={"docs_url": READONLY_DOCS_URL},
        )

    async def async_step_client_id(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Only used when no OAuth App Client ID is built in."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._client_id = user_input[CONF_CLIENT_ID].strip()
            if self._client_id:
                return await self.async_step_device()
            errors[CONF_CLIENT_ID] = "client_id_required"
        return self.async_show_form(
            step_id="client_id",
            data_schema=vol.Schema({vol.Required(CONF_CLIENT_ID): TextSelector()}),
            errors=errors,
        )

    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Sign in with an existing token (e.g. `gh auth token`)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            try:
                await GitHubClient(async_get_clientsession(self.hass), token).user()
            except GitHubAuthError:
                errors[CONF_TOKEN] = "invalid_token"
            except GitHubError:
                errors["base"] = "cannot_connect"
            else:
                self._token = token
                self._client_id = ""
                return await self.async_step_signed_in()
        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema(
                {vol.Required(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
            ),
            errors=errors,
            description_placeholders={"docs_url": READONLY_DOCS_URL},
        )

    async def _wait_for_token(self) -> str:
        assert self._device is not None
        flow = GitHubDeviceFlow(async_get_clientsession(self.hass), self._client_id)
        interval = self._device.interval
        deadline = asyncio.get_running_loop().time() + self._device.expires_in
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(interval)
            try:
                token, error = await flow.poll(self._device.device_code)
            except GitHubError:
                continue  # transient network or GitHub error: keep polling until expiry
            if token:
                return token
            if error == "slow_down":
                interval += 5
            elif error != "authorization_pending":
                raise GitHubError(error or "device flow failed")
        raise GitHubError("expired_token")

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._client_id:
            return await self.async_step_client_id()
        if self._device is None:
            try:
                self._device = await GitHubDeviceFlow(
                    async_get_clientsession(self.hass), self._client_id
                ).start()
            except GitHubError:
                return self.async_abort(reason="device_flow_failed")
        if self._login_task is None:
            self._login_task = self.hass.async_create_task(self._wait_for_token())
        if not self._login_task.done():
            return self.async_show_progress(
                step_id="device",
                progress_action="wait_for_device",
                description_placeholders={
                    "url": self._device.verification_uri,
                    "code": self._device.user_code,
                },
                progress_task=self._login_task,
            )
        try:
            self._token = self._login_task.result()
        except GitHubError:
            return self.async_show_progress_done(next_step_id="login_failed")
        return self.async_show_progress_done(next_step_id="signed_in")

    async def async_step_login_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_abort(reason="login_failed")

    async def async_step_signed_in(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._token is not None
        client = GitHubClient(async_get_clientsession(self.hass), self._token)
        try:
            self._user = await client.user()
        except GitHubError:
            return self.async_abort(reason="login_failed")
        await self.async_set_unique_id(str(self._user["id"]))

        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            entry = self._get_reauth_entry()
            # Keep the relay secret: it is part of every registered URL.
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    CONF_TOKEN: self._token,
                    CONF_LOGIN: self._user["login"],
                    CONF_CLIENT_ID: self._client_id,
                },
            )

        self._abort_if_unique_id_configured()
        try:
            self._candidates = await client.private_repositories()
        except GitHubError:
            return self.async_abort(reason="cannot_list")
        return await self.async_step_repositories()

    async def async_step_repositories(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"invalid": ""}
        if user_input is not None:
            chosen: list[str] = user_input.get(CONF_REPOSITORIES, [])
            client = GitHubClient(async_get_clientsession(self.hass), self._token or "")
            good, errors, placeholders["invalid"] = await _check_selection(client, chosen)
            if errors.get("base") == "invalid_auth":
                # No entry exists yet, so there is nothing to reauthenticate.
                return self.async_abort(reason="login_failed")
            if not errors:
                return self.async_create_entry(
                    title=self._user["login"],
                    data={
                        CONF_CLIENT_ID: self._client_id,
                        CONF_TOKEN: self._token,
                        CONF_LOGIN: self._user["login"],
                        CONF_USER_ID: self._user["id"],
                        CONF_RELAY_SECRET: secrets.token_urlsafe(24),
                    },
                    options={CONF_REPOSITORIES: good},
                )
        return self.async_show_form(
            step_id="repositories",
            data_schema=vol.Schema(
                {vol.Optional(CONF_REPOSITORIES, default=[]): _repo_selector(self._candidates)}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )


class GitHubPrivateAppsOptionsFlow(OptionsFlow):
    """Add or remove repositories."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["add", "remove"])

    def _abort_reauth(self) -> ConfigFlowResult:
        """GitHub rejected the saved token: ask the user to sign in again."""
        self.config_entry.async_start_reauth(self.hass)
        return self.async_abort(reason="reauth_required")

    @property
    def _configured(self) -> list[str]:
        return list(self.config_entry.options.get(CONF_REPOSITORIES, []))

    async def async_step_add(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        session = async_get_clientsession(self.hass)
        client = GitHubClient(session, self.config_entry.data[CONF_TOKEN])
        errors: dict[str, str] = {}
        placeholders = {"invalid": ""}

        if user_input is not None:
            chosen: list[str] = user_input.get(CONF_REPOSITORIES, [])
            good, errors, placeholders["invalid"] = await _check_selection(client, chosen)
            if errors.get("base") == "invalid_auth":
                return self._abort_reauth()
            if not errors:
                # Saving the options reloads the entry, which registers the
                # new repositories in the Supervisor through the relay.
                return self.async_create_entry(
                    data={CONF_REPOSITORIES: sorted({*self._configured, *good})}
                )

        try:
            available = await client.private_repositories()
        except GitHubAuthError:
            return self._abort_reauth()
        except GitHubError:
            return self.async_abort(reason="cannot_list")
        configured = {n.lower() for n in self._configured}
        candidates = [n for n in available if n.lower() not in configured]
        if not candidates:
            return self.async_abort(reason="nothing_to_add")
        return self.async_show_form(
            step_id="add",
            data_schema=vol.Schema(
                {vol.Optional(CONF_REPOSITORIES, default=[]): _repo_selector(candidates)}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_remove(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._configured:
            return self.async_abort(reason="nothing_to_remove")
        errors: dict[str, str] = {}
        placeholders = {"in_use": ""}

        if user_input is not None:
            chosen: list[str] = user_input.get(CONF_REPOSITORIES, [])
            try:
                supervisor = SupervisorClient(async_get_clientsession(self.hass))
                installed = await supervisor.installed_addons()
                secret = self.config_entry.data[CONF_RELAY_SECRET]
                store = await store_slugs(supervisor)
                slugs = {n: store.get(relay_url(secret, n).lower()) for n in chosen}
                in_use = [
                    n
                    for n, slug in slugs.items()
                    if slug and any(a.get("repository") == slug for a in installed)
                ]
                if in_use:
                    errors[CONF_REPOSITORIES] = "in_use"
                    placeholders["in_use"] = ", ".join(in_use)
                else:
                    for slug in slugs.values():
                        if slug:
                            await supervisor.remove_repository(slug)
                    remaining = [n for n in self._configured if n not in chosen]
                    return self.async_create_entry(data={CONF_REPOSITORIES: remaining})
            except SupervisorError:
                errors["base"] = "supervisor_error"

        return self.async_show_form(
            step_id="remove",
            data_schema=vol.Schema(
                {vol.Optional(CONF_REPOSITORIES, default=[]): _repo_selector(self._configured)}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )
