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

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_LOGIN,
    CONF_REGISTRY_LOGIN,
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
    The ghcr.io credential is removed too, unless such apps remain: they may
    still need it to pull their private images.
    """
    ir.async_delete_issue(hass, DOMAIN, "registry_conflict")
    try:
        supervisor = SupervisorClient(async_get_clientsession(hass))
    except SupervisorError as err:
        _LOGGER.warning("Could not clean up the Supervisor: %s", err)
        return

    kept: list[str] = []
    try:
        installed = {a.get("repository") for a in await supervisor.installed_addons()}
        secret = entry.data[CONF_RELAY_SECRET]
        slugs = await store_slugs(supervisor)
        for full_name in entry.options.get(CONF_REPOSITORIES, []):
            slug = slugs.get(relay_url(secret, full_name).lower())
            if slug and slug in installed:
                kept.append(full_name)
            elif slug:
                await supervisor.remove_repository(slug)
    except SupervisorError as err:
        _LOGGER.warning("Could not clean up the app store: %s", err)
        return  # unknown state: keep the credential rather than break an app

    if kept:
        # The integration is gone, so a repair issue would have no owner:
        # tell the user with a notification that the token is still stored.
        persistent_notification.async_create(
            hass,
            f"Apps from {', '.join(kept)} are still installed, so these repositories and the "
            f"{REGISTRY_HOST} credential (your GitHub token) were kept in the Supervisor. "
            f"Uninstall those apps, then remove the repositories in Settings > Apps > Store > "
            f"Repositories and the {REGISTRY_HOST} entry in Registries.",
            title="GitHub Private Apps: GitHub token kept",
            notification_id=f"{DOMAIN}_kept_credential",
        )
        return
    try:
        current = (await supervisor.registries()).get(REGISTRY_HOST)
        if current and _owns_registry(entry, current):
            await supervisor.remove_registry(REGISTRY_HOST)
    except SupervisorError as err:
        _LOGGER.warning("Could not remove the %s credential: %s", REGISTRY_HOST, err)


def _owns_registry(entry: GitHubPrivateAppsConfigEntry, credential: dict) -> bool:
    """True if the registry credential is the one this entry set.

    A credential for another user is not ours to replace or remove. The login
    last written is remembered so a GitHub username change is still ours.
    """
    user = str(credential.get("username") or "").lower()
    ours = {entry.data[CONF_LOGIN].lower()}
    if previous := entry.data.get(CONF_REGISTRY_LOGIN):
        ours.add(previous.lower())
    return user in ours


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
    if current and not _owns_registry(entry, current):
        current_user = str(current.get("username") or "")
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
    if entry.data.get(CONF_REGISTRY_LOGIN) != login:
        # Runs before the update listener is added: does not reload the entry.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REGISTRY_LOGIN: login}
        )


async def _sync_repositories(entry: GitHubPrivateAppsConfigEntry, supervisor: SupervisorClient) -> None:
    """Register every configured repository in the Supervisor store."""
    secret = entry.data[CONF_RELAY_SECRET]
    known = set(await store_slugs(supervisor))
    for full_name in entry.options.get(CONF_REPOSITORIES, []):
        url = relay_url(secret, full_name)
        if url.lower() not in known:
            _LOGGER.info("Adding %s to the Supervisor store", full_name)
            await supervisor.add_repository(url)


async def store_slugs(supervisor: SupervisorClient) -> dict[str, str]:
    """Return {lowercased source URL: slug} for the store repositories."""
    return {
        str(repo.get("source") or "").lower(): repo["slug"]
        for repo in await supervisor.repositories()
        if repo.get("slug")
    }
