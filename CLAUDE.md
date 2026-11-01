# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build and Development Commands

```bash
# Setup virtual environment and install dependencies
make install            # Creates .venv and installs requirements-dev.txt

# Testing and linting
pytest .                # Run all tests
pytest tests/test_base.py::test_true  # Run single test
make tcheck             # Type check with mypy (specific modules only)
make lint               # Format code with black (line length 120)
make isort              # Sort imports

# Pre-commit checks (runs black --check, mypy, and gitleaks)
make commit-checks      # Run pre-commit on all files
make gitleaks           # Run gitleaks secret scan on all files
make prepare            # Run tests + commit-checks

# Docker build
./build.sh              # Multi-arch build and push to Docker Hub
./build.sh onlylocal    # Local build only (no push)
make build              # Update git submodules and run build.sh
make dstart             # Run container interactively (mounts config.local.yaml, ~/.config/gcal, ~/.kube, ~/.ssh)

# PyPI distributions (one per package, see packaging/<pkg>.toml)
make distcheck-<pkg>    # Stage into build/<pkg>/, build sdist + wheel with hatch, run scripts/check_dist_secrets.py
make pypibuild-<pkg>    # Same as distcheck-<pkg>
make pypipush-<pkg>     # Upload; needs PYPI_* in scripts/include.local.sh, only upload.pypi.org or test.pypi.org are accepted
make dist-all           # distcheck for every publishable package
```

## Architecture Overview

This is a collection of Python utilities for IoT data aggregation, weather monitoring, and DNS automation. The codebase follows a modular structure where each subdirectory is a self-contained Python package.

### Namespace Layout

All packages live under `somestuff/<pkg>/`. `somestuff` is a PEP 420 namespace package: there is no `somestuff/__init__.py` and none may ever be added (one distribution shipping it would hide every other `somestuff.*` package). Imports are `from somestuff.<pkg> import ...`, entry points `python3 -m somestuff.<pkg>.<module>`. Publishable packages carry `__version__` in their `__init__.py` (hatch reads it) and get a `packaging/<pkg>.toml` that becomes the `pyproject.toml` of the staged distribution `somestuff-<pkg>`. The shared settings loader lives in `somestuff/corestuff/` (`config.py`, `helper.py`, `config.yaml` sample) and is its own distribution `somestuff-corestuff`; packages that need it (`ecowittstuff`, `netatmostuff`, `hydromailstuff`, `llmstuff`) declare it as a dependency, never bundle it. `config.local.yaml` stays outside every wheel. The root `pyproject.toml` carries `Private :: Do Not Upload`.

### Configuration System

- **somestuff/corestuff/config.py**: Pydantic-based settings loader using `pydantic-settings`; nothing is read at import, `get_settings()` (or the lazy `settings` proxy) loads on first use
- **somestuff/corestuff/config.yaml**: Default configuration (sample values, committed, shipped in the wheel); must validate on its own
- **config.local.yaml**: Local overrides for secrets (not committed), searched in the CWD, then `/app`; deep-merged via `helper.update_deep()`
- `SOMESTUFF_CONFIG_PATH`, `SOMESTUFF_CONFIG_LOCAL_PATH`, `SOMESTUFF_CONFIG_DIR_PATH` override the lookup; nested env vars (`MQTT__HOST`) override values

### Key Modules

| Module                                    | Purpose                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
|-------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `somestuff/broadlinkstuff/`               | Broadlink RM4 IR/RF blaster: device fleet, IR/RF learning, Coolix (Midea) climate codec, Typer CLI (`broadlinkhelper`; own `broadlink.yaml` + `broadlink.local.yaml`, not the repo-wide config) + `ac_api` (FastAPI in front of per-room `*_ac_*.sh` scripts, Typer CLI `broadlink-ac-api`, `AC_API_*` env, extra `api`)                                                                                                                                   |
| `somestuff/corestuff/`                    | Shared settings loader: `config.py` (lazy `get_settings()`/`settings` proxy, `config.yaml` sample + `config.local.yaml` from CWD or `/app`, `SOMESTUFF_CONFIG_*` env), `helper.py` (`ComplexEncoder`, `update_deep`), `configure_logging()`                                                                                                                                                                                                                |
| `somestuff/dhcpstuff/dhcp_discover.py`    | DHCP Discover sender with PXE/Proxy DHCP support                                                                                                                                                                                                                                                                                                                                                                                                           |
| `somestuff/dnsstuff/spf_ipset_updater.py` | SPF record crawler → ipset updater for SMTP allowlisting                                                                                                                                                                                                                                                                                                                                                                                                   |
| `somestuff/ecowittstuff/ecowittapi.py`    | Ecowitt weather station API client (typed with Pydantic)                                                                                                                                                                                                                                                                                                                                                                                                   |
| `somestuff/gcalstuff/gcal_event.py`       | Google Calendar event creation CLI (OAuth2)                                                                                                                                                                                                                                                                                                                                                                                                                |
| `somestuff/hydromailstuff/hydromail.py`   | Assembles status emails from MQTT/Netatmo data                                                                                                                                                                                                                                                                                                                                                                                                             |
| `somestuff/k3shelperstuff/`               | K3s kubeconfig credential sync via SSH (`update_local_k3s_keys`), user client certificates via the CSR API (`k8s_user_cert`, Typer CLI, `K8S_USER_CERT_*` env) + Keel image-drift checker (`keel_drift`, Typer CLI, `KEEL_*` env) + version-pin vs. upstream-release checker (`pin_drift`, Typer CLI, `pin_drift.yml`, `PIN_DRIFT_*` env) + `drift` (both as subcommands `pin`/`keel` of one Typer CLI; `DRIFT_PROG_NAME` sets the name shown in `--help`) |
| `somestuff/llmstuff/`                     | LLM API helpers (Google Gemini, Anthropic, Ollama OCR)                                                                                                                                                                                                                                                                                                                                                                                                     |
| `somestuff/netatmostuff/lnetatmo.py`      | Netatmo weather data client                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `sipstuff/`                               | **Moved** to [github.com/vroomfondel/sipstuff](https://github.com/vroomfondel/sipstuff)                                                                                                                                                                                                                                                                                                                                                                    |
| `somestuff/ucmstuff/`                     | Grandstream UCM6204 IP-PBX monitor/control: WebSocket events (`UCMEventClient`) + HTTPS-API control (`UCM6204`/`UCM6204Rest`), trunk call router, Typer CLI                                                                                                                                                                                                                                                                                                |
| `somestuff/uptimekumastuff/`              | Uptime-Kuma 2.x provisioning/migration: `KumaClient` (direct Socket.IO, fresh reads, idempotent upserts), `uptimekuma_apply` (YAML → idempotent apply, Typer CLI), `SimpleKumaApi` (full export/import, Typer CLI)                                                                                                                                                                                                                                         |
| `somestuff/oepnvstuff/`                   | GTFS-Realtime coverage checker: `RealtimeMonitor` (poll loop, staleness detection, pluggable `on_*` handlers), MQTT bridge, Typer CLI (`check_realtime`, `OEPNV_*` env)                                                                                                                                                                                                                                                                                    |
| `somestuff/mqttwebstuff/`                 | Live web view onto MQTT streams: mountable mapper plugins (`plugin_api.ViewEvent`), `ViewHub` (last-value cache + SSE fan-out), FastAPI + htmx/PicoCSS frontend, Typer CLI (`serve`, `MQTTWEB_*` env)                                                                                                                                                                                                                                                      |
| `packaging/`                              | One `<pkg>.toml` per PyPI distribution (`somestuff-<pkg>`), copied into `build/<pkg>/pyproject.toml` by `make stage-<pkg>`                                                                                                                                                                                                                                                                                                                                 |
| `scripts/`                                | Build helper scripts (`include.sh`, `update_badge.py`, `check_dist_secrets.py`: gate that refuses sdists/wheels with files outside the hatch allowlist or a `somestuff/__init__.py`)                                                                                                                                                                                                                                                                       |

### Standalone Docker Images

These subdirectories contain independent Docker image builds with their own `build.sh`:

| Directory                   | Purpose                                              |
|-----------------------------|------------------------------------------------------|
| `tangstuff/`                | Tang server for LUKS/Clevis network-bound encryption |
| `mosquitto-2.1/`            | Mosquitto 2.1 MQTT broker with dynamic security      |
| `python314jit/`             | Python 3.14 base image with JIT support              |
| `python314pandasmultiarch/` | Python 3.14 base image with pandas (multi-arch)      |
| `f2bapistuff/`              | fail2ban + remote ban API (`xomoxcc/f2b-api`)        |
| `postfixpgsqlstuff/`        | Postfix with pgsql maps + policyd-spf for k8s pods   |

### Shared Utilities

- **somestuff/corestuff/helper.py**: JSON serialization (`ComplexEncoder`), deep dict merging
- **mqttstuff**: External package (PyPI: `mqttstuff`) for MQTT topic management
- **reputils**: External package (PyPI: `reputils`) for repository utilities
- **docker-config/**: Private directory containing Docker registry credentials for `build.sh`

### Docker Image

- Base: `python:3.14-slim-trixie`
- Single-stage build (sipstuff/PJSIP/piper were removed — sipstuff is now a standalone repo)
- Runs as non-root user `pythonuser` (UID 1200)
- Multi-arch: linux/amd64, linux/arm64
- Entry point uses `tini` for proper signal handling
- `PYTHONPATH=/app` is set; `somestuff/` is copied to `/app/somestuff`; `config.local.yaml` is expected at `/app/config.local.yaml` (CWD)

## Code Style

- Black formatter with 120 character line length
- Mypy for static type checking (excludes .venv, tests)
- Pre-commit hooks: yaml validation, black --check, mypy, gitleaks
- Secret scanning: `.gitleaks.toml` extends the default gitleaks ruleset; allowlists `.venv`, `.mypy_cache`, `.idea`, `__pycache__`, `*local*` files, and `docker-config`
- Loguru for logging (each package's `configure_logging()` installs the shared format; nothing configures logging at import)

### Comments

- Default: no comment. A comment has to earn its line.
- **One line maximum** per comment, except a file header. No multi-line prose blocks.
- A comment may state a *constraint* (what breaks, which version, which upstream issue).
  It may NOT explain motivation, history, alternatives considered, or current deployment
  state. That belongs in the commit message, an issue, or a doc file.
- Banned openings: "This is how ...", "Without it ...", "We do/don't ...", "Note that ...",
  "The reason ...".
- No comment referring to volatile state (current version, current workload, current tag).
- No comments that restate what the line already says.
- No docstrings unless explicitly asked for.
- No section banners (`# ---- Helper ----`).
- No meta comments about the editing process itself ("newly added", "adjusted as requested").
- When in doubt: no comment.

## Markdown Tables

- Every Markdown table (README, CLAUDE.md, concept docs) is column-aligned: cells padded with spaces so all `|` line up, the separator row `|---|` spans the full column width, right-aligned columns (`---:`) are padded on the left.
- Width is display width: umlauts and emoji count as they are rendered.
- A cell gets shorter or longer: adjust the padding behind it so the closing `|` stays put. New content exceeds the column width: re-align the **whole** table, header and separator row included.
- New rows and new tables are written aligned as well. No compact `| a | b |` rows, no blind `sed` on table rows.
