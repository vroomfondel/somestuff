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
```

## Architecture Overview

This is a collection of Python utilities for IoT data aggregation, weather monitoring, and DNS automation. The codebase follows a modular structure where each subdirectory is a self-contained Python package.

### Configuration System

- **config.py**: Pydantic-based settings loader using `pydantic-settings`
- **config.yaml**: Default configuration (sample values, committed to repo)
- **config.local.yaml**: Local overrides for secrets (not committed, merged at runtime via `Helper.update_deep()`)
- Environment variables can override config using `SOMESTUFF_` prefix or nested delimiter `__`

### Key Modules

| Module                       | Purpose                                                     |
|------------------------------|-------------------------------------------------------------|
| `dhcpstuff/dhcp_discover.py` | DHCP Discover sender with PXE/Proxy DHCP support            |
| `dnsstuff/pcbwaydnsstuff.py` | SPF record crawler → ipset updater for SMTP allowlisting    |
| `ecowittstuff/ecowittapi.py` | Ecowitt weather station API client (typed with Pydantic)    |
| `gcalstuff/gcal_event.py`   | Google Calendar event creation CLI (OAuth2)                 |
| `hydromailstuff/hydromail.py`| Assembles status emails from MQTT/Netatmo data              |
| `k3shelperstuff/`           | K3s kubeconfig credential sync via SSH                      |
| `llmstuff/`                  | LLM API helpers (Google Gemini, Anthropic, Ollama OCR)      |
| `netatmostuff/lnetatmo.py`   | Netatmo weather data client                                 |
| `sipstuff/sip_caller.py`    | SIP caller — phone calls with WAV playback or piper TTS via PJSUA2 |
| `dinogame/`                  | Grid pathfinding visualization (A* experiments)             |
| `scripts/`                   | Build helper scripts (`include.sh`, `update_badge.py`)      |

### Standalone Docker Images

These subdirectories contain independent Docker image builds with their own `build.sh`:

| Directory                  | Purpose                                              |
|----------------------------|------------------------------------------------------|
| `tangstuff/`               | Tang server for LUKS/Clevis network-bound encryption |
| `mosquitto-2.1/`           | Mosquitto 2.1 MQTT broker with dynamic security      |
| `python314jit/`            | Python 3.14 base image with JIT support              |
| `python314pandasmultiarch/`| Python 3.14 base image with pandas (multi-arch)      |

### Shared Utilities

- **Helper.py**: JSON serialization (`ComplexEncoder`), deep dict merging
- **mqttstuff**: External package (PyPI: `mqttstuff`) for MQTT topic management
- **reputils**: External package (PyPI: `reputils`) for repository utilities
- **docker-config/**: Private directory containing Docker registry credentials for `build.sh`

### Docker Image

- Base: `python:3.14-slim-trixie`
- Multi-stage build: stage 1 compiles PJSIP with Python bindings, stage 2 creates a Python 3.12 venv for piper-tts at `/opt/piper-venv` (piper-phonemize lacks Python 3.14 wheels), stage 3 assembles the final image
- `sipstuff/tts.py` calls the piper CLI via subprocess from the 3.12 venv; model downloads use `piper.download_voices` via the venv's Python
- Runs as non-root user `pythonuser` (UID 1200)
- Multi-arch: linux/amd64, linux/arm64
- Entry point uses `tini` for proper signal handling
- `PYTHONPATH=/app` is set; all modules copied to `/app/`

### sipstuff / PJSUA2 Notes

- **Multi-homed hosts**: `sip_caller.py` auto-detects the local IP via `_local_address_for()` (UDP connect to SIP server, no data sent) and binds both SIP and RTP transports to it. Without this, PJSIP picks the wrong interface and causes one-way audio.
- **PJSUA2 Python bindings quirks**: `EpConfig` uses `medConfig` (not `mediaConfig`); `AccountConfig` uses `mediaConfig`. `MediaConfig` has no `transportConfig`, but `AccountMediaConfig.transportConfig` exists and controls per-call RTP socket binding.
- **WAV player uses loop mode**: `AudioMediaPlayer.createPlayer()` without `PJMEDIA_FILE_NO_LOOP` keeps the conference port alive for clean `stopTransmit()` teardown. With `NO_LOOP` the player auto-detaches at EOF, causing `PJ_EINVAL` on any subsequent port operation.
- **Orphaned player pattern**: `stop_wav()` moves the player reference into `SipCaller._orphaned_players` instead of destroying it immediately (CPython refcounting triggers the C++ destructor which races with conference bridge teardown). Players are cleared in `SipCaller.stop()` before `libDestroy()`.
- **Known harmless warnings**: `PJSIP_ETPNOTSUITABLE` on INVITE (transport selection retry) and `conference.c Remove port failed` on hangup (port already invalidated by call teardown) are cosmetic and do not affect functionality.
- **NAT traversal**: Optional STUN/ICE/TURN support via `NatConfig` in `sipconfig.py`. STUN servers are configured at endpoint level (`EpConfig.uaConfig.stunServer`); ICE, TURN, and UDP keepalives at account level (`AccountConfig.natConfig.*`). `_local_address_for()` still runs for local interface binding regardless of NAT config.
- **Static NAT / `publicAddress`**: For K3s pods or SNAT scenarios where auto-detection returns an unreachable IP (pod IP) and STUN returns the wrong IP (WAN IP), `NatConfig.public_address` sets `TransportConfig.publicAddress` on both SIP and RTP transports. PJSIP then advertises this IP in SDP `c=` and Contact headers while the socket stays bound to the local (pod) IP. Requires stateful SNAT (conntrack) so replies are translated back.

## Code Style

- Black formatter with 120 character line length
- Mypy for static type checking (excludes .venv, tests)
- Pre-commit hooks: yaml validation, black --check, mypy, gitleaks
- Secret scanning: `.gitleaks.toml` extends the default gitleaks ruleset; allowlists `.venv`, `.mypy_cache`, `.idea`, `__pycache__`, `*local*` files, and `docker-config`
- Loguru for logging (configured in config.py with custom format)
