# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration (Marketplace/HACS, domain `github_private_apps`) that lets the Supervisor install apps (add-ons) from private GitHub repositories without putting a token in the repository URL. Requires a Supervisor (HA OS / Supervised), HA 2025.1+.

## Commands

```bash
pip install -r requirements_test.txt   # pytest, pytest-aiohttp, aiohttp — no homeassistant
pytest                                 # all tests (asyncio_mode = auto, see pytest.ini)
pytest tests/test_relay.py::test_rejections   # single test
```

CI (`.github/workflows/validate.yml`) runs HACS validation, hassfest and pytest on Python 3.13. Hassfest checks `manifest.json` and translations. As a custom integration there is no `strings.json` (the Marketplace guide says not to ship one): `translations/en.json` is the source, keep `translations/fr.json` in sync when adding error/abort keys.

Home Assistant is **not** installed in the test environment. Only `relay.py` is tested: the test loads it by file path (`importlib`), bypassing the package `__init__.py` which imports `homeassistant`. Keep `relay.py` free of Home Assistant and relative imports. Other modules can only be syntax-checked locally (`python -m py_compile`).

## Architecture

The core idea: the Supervisor derives every app slug from a hash of the repository URL. A URL with an embedded token changes when the token changes, which forces uninstalling apps. So the integration registers a fixed, token-free URL pointing at a git relay it runs itself:

```
Supervisor --git--> http://172.30.32.1:48123/<relay_secret>/owner/repo.git  (relay.py, inside HA Core)
                    relay adds Basic auth (login, token) --> https://github.com/owner/repo.git
```

- `relay.py` — standalone aiohttp server. Only serves `info/refs?service=git-upload-pack` (GET) and `git-upload-pack` (POST); push is refused. Checks peer is in `172.30.32.0/23`, compares the URL secret with `hmac.compare_digest`, and asks a `credentials(full_name)` callback, which returns `None` for repos not configured. Upstream 401 triggers `on_auth_failure` → reauth.
- `__init__.py` — entry setup: validates the token (`ConfigEntryAuthFailed` on 401), starts the relay, then syncs the Supervisor: sets the `ghcr.io` registry credential (`_sync_registry`) and adds missing store repositories (`_sync_repositories`). Options changes reload the entry, which re-syncs. `async_remove_entry` removes store repos (unless apps from them are still installed) and the ghcr.io credential it owns; if any app remains installed the credential is kept and a persistent notification says so.
- `config_flow.py` — device flow (background `_login_task` polling, shown with `async_show_progress`) or pasted token, then repository selection (must contain `repository.yaml|yml|json`). Reauth keeps `relay_secret`. The options flow adds/removes repos; removal is refused while apps from the repo are installed.
- `github.py` — device flow + small REST client. Errors surface as `GitHubError` / `GitHubAuthError` (401).
- `supervisor.py` — direct REST calls to the Supervisor using the `SUPERVISOR` / `SUPERVISOR_TOKEN` env vars (deliberately not the `hassio` integration internals). Errors surface as `SupervisorError`.

## Invariants (do not break)

- `RELAY_HOST`, `RELAY_PORT`, the URL format in `relay_url()` and an entry's `relay_secret` are part of every registered URL, so of every app slug. Changing any of them for existing installs changes app slugs.
- The Supervisor keeps one credential per registry host. A `ghcr.io` credential belonging to another user is never overwritten or removed; a `registry_conflict` repair issue is raised instead.
- Tokens are never logged; log `type(err).__name__` rather than exception text for network errors.
- Single config entry (`single_config_entry: true`), unique id = GitHub user id.
