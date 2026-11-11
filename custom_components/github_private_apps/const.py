"""Constants for GitHub Private Apps."""

from __future__ import annotations

from ipaddress import ip_network
from typing import Final

DOMAIN: Final = "github_private_apps"

# GitHub OAuth App used for the device flow. Device flow needs no client
# secret, so the client id is public. Override it in the config flow if you
# run your own OAuth App.
DEFAULT_CLIENT_ID: Final = "Ov23liJ2Q33iEOLQMHvD"

GITHUB_SCOPES: Final = "repo read:packages"
GITHUB_DEVICE_CODE_URL: Final = "https://github.com/login/device/code"
GITHUB_TOKEN_URL: Final = "https://github.com/login/oauth/access_token"
GITHUB_API: Final = "https://api.github.com"
GITHUB_GIT: Final = "https://github.com"

REGISTRY_HOST: Final = "ghcr.io"

# Git relay. The relay URL is what the Supervisor stores, and the Supervisor
# derives the repository slug from it: host and port must never change once
# repositories are registered, or every app slug changes.
RELAY_HOST: Final = "172.30.32.1"
RELAY_PORT: Final = 48123
SUPERVISOR_NETWORK: Final = ip_network("172.30.32.0/23")

CONF_CLIENT_ID: Final = "client_id"
CONF_TOKEN: Final = "token"
CONF_LOGIN: Final = "login"
CONF_USER_ID: Final = "user_id"
CONF_REPOSITORIES: Final = "repositories"
CONF_RELAY_SECRET: Final = "relay_secret"
# GitHub login last written to the ghcr.io registry credential. Lets the
# entry recognise its own credential after a GitHub username change.
CONF_REGISTRY_LOGIN: Final = "registry_login"
