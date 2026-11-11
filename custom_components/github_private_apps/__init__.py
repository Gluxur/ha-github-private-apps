"""GitHub Private Apps: install Supervisor apps from private GitHub repositories.

The Supervisor stores each repository by URL and derives every app slug
from a hash of that URL. Putting a token in the URL therefore ties the app
to the token: rotating it means uninstalling the app. This integration
registers a token-free URL pointing at a local git relay instead; the relay
adds the current GitHub credential when it forwards requests to GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_LOGIN,
    CONF_RELAY_SECRET,
    CONF_REPOSITORIES,
    CONF_TOKEN,
    DOMAIN,
    REGISTRY_HOST,
    RELAY_HOST,
    RELAY_PORT,
    SUPERVISOR_NETWORK,
)
from .github import GitHubAuthError, GitHubClient, GitHubError
from .relay import GitRelay
from .supervisor import SupervisorClient, SupervisorError

_LOGGER = logging.getLogger(__name__)


@dataclass
class RuntimeData:
    """Objects living for the duration of a config entry."""

    relay: GitRelay
    supervisor: SupervisorClient


type GitHubPrivateAppsConfigEntry = ConfigEntry[RuntimeData]


def relay_url(secret: str, full_name: str) -> str:
    """Return the token-free URL registered in the Supervisor for a repo."""
    return f"http://{RELAY_HOST}:{RELAY_PORT}/{secret}/{full_name}.git"


async def async_setup_entry(hass: HomeAssistant, entry: GitHubPrivateAppsConfigEntry) -> bool:
    """Start the relay and make the Supervisor match the configuration."""
    session = async_get_clientsession(hass)

    try:
        supervisor = SupervisorClient(session)
    except SupervisorError as err:
        raise ConfigEntryNotReady(str(err)) from err

    try:
        await GitHubClient(session, entry.data[CONF_TOKEN]).user()
    except GitHubAuthError as err:
        raise ConfigEntryAuthFailed("GitHub access was revoked") from err
    except GitHubError as err:
        raise ConfigEntryNotReady(str(err)) from err

    reauth_started = False

    @callback
    def on_auth_failure(full_name: str, status: int) -> None:
        nonlocal reauth_started
        _LOGGER.warning("GitHub refused access to %s (HTTP %s)", full_name, status)
        if status == 401 and not reauth_started:
            reauth_started = True
            entry.async_start_reauth(hass)

    def credentials(full_name: str) -> tuple[str, str] | None:
        configured = {name.lower() for name in entry.options.get(CONF_REPOSITORIES, [])}
        if full_name not in configured:
            return None
        return entry.data[CONF_LOGIN], entry.data[CONF_TOKEN]

    relay = GitRelay(
        session=session,
        secret=entry.data[CONF_RELAY_SECRET],
        credentials=credentials,
        allowed_networks=[SUPERVISOR_NETWORK],
        on_auth_failure=on_auth_failure,  # relay handlers run in the event loop
    )
    try:
        await relay.start(RELAY_HOST, RELAY_PORT)
    except OSError as err:
        raise ConfigEntryNotReady(
            f"Cannot listen on {RELAY_HOST}:{RELAY_PORT}: {err}"
        ) from err

    entry.runtime_data = RuntimeData(relay=relay, supervisor=supervisor)

    try:
        await _sync_registry(hass, entry, supervisor)
        await _sync_repositories(entry, supervisor)
    except SupervisorError as err:
        await relay.stop()
        raise ConfigEntryNotReady(f"Supervisor: {err}") from err

    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GitHubPrivateAppsConfigEntry) -> bool:
    """Stop the relay."""
    await entry.runtime_data.relay.stop()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: GitHubPrivateAppsConfigEntry) -> None:
    """Remove the relay repositories from the Supervisor store.

    Without the integration nothing answers on their URL any more. A
    repository with installed apps is kept: removing it would orphan them.
    """
    try:
        supervisor = SupervisorClient(async_get_clientsession(hass))
        installed = {a.get("repository") for a in await supervisor.installed_addons()}
        secret = entry.data[CONF_RELAY_SECRET]
        for full_name in entry.options.get(CONF_REPOSITORIES, []):
            slug = await find_store_slug(supervisor, relay_url(secret, full_name))
            if slug and slug not in installed:
                await supervisor.remove_repository(slug)
            elif slug:
                _LOGGER.warning(
                    "Kept %s in the app store: apps from it are still installed", full_name
                )
    except SupervisorError as err:
        _LOGGER.warning("Could not clean up the app store: %s", err)

    ir.async_delete_issue(hass, DOMAIN, "registry_conflict")
    try:
        await _remove_registry(entry, SupervisorClient(async_get_clientsession(hass)))
    except SupervisorError as err:
        _LOGGER.warning("Could not remove the %s credential: %s", REGISTRY_HOST, err)


async def _remove_registry(entry: GitHubPrivateAppsConfigEntry, supervisor: SupervisorClient) -> None:
    """Remove the ghcr.io credential if it is the one this entry set.

    Leaving it would keep the GitHub token in the Supervisor after the
    integration is gone. A credential for another user is not ours to remove.
    """
    current = (await supervisor.registries()).get(REGISTRY_HOST)
    if current and current.get("username", "").lower() == entry.data[CONF_LOGIN].lower():
        await supervisor.remove_registry(REGISTRY_HOST)


async def _async_reload(hass: HomeAssistant, entry: GitHubPrivateAppsConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _sync_registry(
    hass: HomeAssistant, entry: GitHubPrivateAppsConfigEntry, supervisor: SupervisorClient
) -> None:
    """Point the ghcr.io credential at the current token.

    An existing ghcr.io credential for another user is left alone and
    reported, since the Supervisor keeps one credential per registry host.
    """
    login: str = entry.data[CONF_LOGIN]
    current = (await supervisor.registries()).get(REGISTRY_HOST)
    current_user = (current or {}).get("username", "")
    if current and current_user.lower() != login.lower():
        ir.async_create_issue(
            hass,
            DOMAIN,
            "registry_conflict",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="registry_conflict",
            translation_placeholders={"registry": REGISTRY_HOST, "user": current_user, "login": login},
        )
        return
    ir.async_delete_issue(hass, DOMAIN, "registry_conflict")
    await supervisor.set_registry(REGISTRY_HOST, login, entry.data[CONF_TOKEN])


async def _sync_repositories(entry: GitHubPrivateAppsConfigEntry, supervisor: SupervisorClient) -> None:
    """Register every configured repository in the Supervisor store."""
    secret = entry.data[CONF_RELAY_SECRET]
    known = {repo.get("source", "").lower() for repo in await supervisor.repositories()}
    for full_name in entry.options.get(CONF_REPOSITORIES, []):
        url = relay_url(secret, full_name)
        if url.lower() not in known:
            _LOGGER.info("Adding %s to the Supervisor store", full_name)
            await supervisor.add_repository(url)


async def find_store_slug(supervisor: SupervisorClient, url: str) -> str | None:
    """Return the Supervisor slug of the repository registered with url."""
    for repo in await supervisor.repositories():
        if repo.get("source", "").lower() == url.lower():
            return repo.get("slug")
    return None
