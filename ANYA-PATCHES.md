# ANYA-PATCHES

This branch holds the Anya-specific setup for the Hermes agent.

## Branch rules

- `anya-patches` is the working branch. Make all changes on this branch.
- `main` is a read-only mirror of `NousResearch/hermes-agent`. Do not commit to `main`.
- The fork is `openclaw-anya-wego/hermes-agent`.

## Install

| Item | Value |
| --- | --- |
| Host | `anyaminimac` (Mac mini M4, user `wegoaiteam`) |
| Directory | `/Users/wegoaiteam/hermes-agent` |
| Version | Hermes Agent v0.20.0 (2026.8.3) |
| Python | 3.11.15 in `venv/` |
| OpenAI SDK | 2.24.0 |
| Skills | 79 bundled |

To install again, run `./setup-hermes.sh` from the repository root.

### Python version constraint

The mini has Python 3.14.6. Hermes requires Python `>=3.11,<3.14`.
The setup script makes a Python 3.11 virtual environment in `venv/`.
Do not run Hermes with the system Python. The build of `pydantic-core` fails on Python 3.14.

## Memory: use Anya's Honcho

Do not install a second Honcho instance. Anya runs Honcho on this same host.
A second instance costs about 2.6 GB of RAM. It also puts two derivers on one database.

Set these values instead:

```
HONCHO_BASE_URL=http://localhost:8000
workspace_id: openclaw
```

The Honcho server runs with `AUTH_USE_AUTH=false`. A token is not necessary. Leave it empty.

Hermes reads `HONCHO_BASE_URL` in `plugins/memory/honcho/client.py`.
The key `honcho.base_url` in `config.yaml` has the same effect.
The command `hermes memory setup` asks for these values.

Anya's Honcho listens on port 8000 inside Colima. Colima forwards the port to the Mac.
The address `http://localhost:8000` works from a native install.

## Wiki access

Anya's wiki is a markdown vault at `/Users/wegoaiteam/.openclaw/wiki/main`.

Read the files directly. Do not write to the vault.
The OpenClaw tool `wiki_apply` keeps the link graph and the claim-ownership index.
A direct write makes these indexes incorrect.

## Do not use Docker on this host

The file `docker-compose.yml` sets `network_mode: host` on both services.
Colima runs a Linux virtual machine. Under Colima, `host` means the guest network.
The dashboard port is then unreachable from macOS. Install natively instead.

## Status

The install is complete. Hermes is not configured. Hermes is not running.

Remaining steps:

1. Add a model API key to `.env`.
2. Set the Honcho values above.
3. Start Hermes.

### RAM constraint

The mini has 16 GB of RAM. The box is oversubscribed.
Hermes needs about 400 MB to 700 MB for two Python processes.
Free memory before you start Hermes. A reboot clears the swap and the accumulated leaks.
