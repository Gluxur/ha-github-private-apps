# GitHub Private Apps for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/Gluxur/ha-github-private-apps/actions/workflows/validate.yml/badge.svg)](https://github.com/Gluxur/ha-github-private-apps/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Install Supervisor apps (add-ons) from **private GitHub repositories**, with private images on **ghcr.io**, and rotate the GitHub credential **without ever uninstalling an app**.

## Why

The Supervisor accepts a private repository only as `https://user:TOKEN@github.com/owner/repo`, and it derives every app slug from a hash of that URL. When the token expires or changes, the URL changes, the slug changes, and the app has to be uninstalled and reinstalled.

GitHub Private Apps registers a URL that never contains a credential:

```
Supervisor ──git──▶ http://172.30.32.1:48123/<secret>/owner/repo.git     (fixed, token-free)
                         │  git relay inside this integration adds the current GitHub credential
                         ▼
                    https://github.com/owner/repo.git
```

The URL never changes, so app slugs never change. Signing in to GitHub again only updates the credential the relay uses and the `ghcr.io` registry credential.

## Requirements

- Home Assistant OS or Supervised (a Supervisor), Home Assistant 2025.1 or newer. Home Assistant Container and Core have no app store and are not supported.
- A GitHub account with access to the private repositories.

## Access to GitHub

Two ways to sign in, chosen when adding the integration:

- **Sign in with a code (recommended).** The integration shows a link and a one-time code; you approve access on github.com. It uses the *Home Assistant GitHub Private Apps* OAuth App, so there is no token to create. Requested scopes: `repo` (read private repositories; GitHub has no narrower read-only scope for OAuth) and `read:packages` (pull private images from ghcr.io). The access does not expire unless you revoke it (GitHub → Settings → Applications).
- **Paste an existing token.** Any token with the `repo` and `read:packages` scopes, for example from GitHub CLI: `gh auth refresh -s read:packages` then `gh auth token`.

For repositories owned by an organization with OAuth App access restrictions, an org owner must approve the OAuth App.

## Install

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Gluxur&repository=ha-github-private-apps&category=integration)

Or: HACS → ⋮ → Custom repositories → add `https://github.com/Gluxur/ha-github-private-apps` as *Integration*, install **GitHub Private Apps**, restart Home Assistant.

Or copy `custom_components/github_private_apps` into `/config/custom_components/` and restart.

## Configure

Settings → Devices & services → **Add integration** → **GitHub Private Apps**:

1. Choose **Sign in with a code**, open the link shown, enter the code and approve (or paste a token).
2. Pick your private app repositories (repositories with a `repository.yaml` at the root).

The repositories appear in Settings → Apps → Store, and the `ghcr.io` registry credential is set to your GitHub sign-in.

Add or remove repositories later with **Configure** on the integration.

## Rotating / revoking access

If GitHub stops accepting the credential, Home Assistant shows a **re-authenticate** notification. Sign in again: apps stay installed and keep their configuration.

## Migrating an app installed from a token URL (one time)

Apps installed from `https://user:TOKEN@github.com/...` have a slug tied to that URL. Moving them to GitHub Private Apps changes their slug once:

1. Back up the app (Settings → System → Backups → partial backup with the app), and note its configuration.
2. Uninstall the app, then remove the old repository (Store → ⋮ → Repositories).
3. Add the repository through GitHub Private Apps.
4. Install the app again and restore its configuration.

After that, the slug never changes again.

## Security

- The relay listens on `172.30.32.1:48123` (the Supervisor network gateway), plain HTTP, and only answers requests from `172.30.32.0/23`.
- Every relay URL contains a random secret generated at setup, so other apps on the Supervisor network cannot read your repositories through the relay.
- The relay is read-only (`git-upload-pack`); pushes are refused.
- Only repositories configured in the integration are served.
- Tokens are stored in the Home Assistant config entry and are never logged.
- Removing the integration also removes the `ghcr.io` credential it set in the Supervisor, unless apps from its repositories are still installed (they may need it to pull their images); a notification then says so.
- The relay secret is visible to apps granted the Supervisor `manager` or `admin` role (they can list store repositories). Such apps already have far broader control over your system; only install apps you trust with those roles.
- Report vulnerabilities privately, see [SECURITY.md](SECURITY.md).

## Limitations

- The relay runs inside Home Assistant Core: while Core is stopped, the Supervisor cannot pull app repositories (no app updates appear). Installed apps keep running. If the Supervisor reports a repository error while Core was down, **do not remove the repository**; reload the store once Core is up.
- The Supervisor holds one credential per registry host. If `ghcr.io` already has a credential for a different user, GitHub Private Apps leaves it alone and raises a repair issue.
- GitHub only (repositories and ghcr.io). One GitHub account per installation.
- Do not change the relay host/port in the code once repositories are registered: the URL is part of every app slug.

## Uninstall

Remove the integration in Settings → Devices & services. Its repositories are removed from the app store, except those whose apps are still installed (uninstall those apps first if you want the repository gone). Then remove the integration from HACS.

## Disclaimer

This is a community project. It is not affiliated with or endorsed by GitHub or Nabu Casa / Home Assistant.

## Development

```bash
pip install -r requirements_test.txt
pytest
```
