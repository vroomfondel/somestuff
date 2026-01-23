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

# Pre-commit checks (runs black --check and mypy)
make commit-checks      # Run pre-commit on all files
make prepare            # Run tests + commit-checks

# Docker build
./build.sh              # Multi-arch build and push to Docker Hub
./build.sh onlylocal    # Local build only (no push)
make dstart             # Run container interactively with config.local.yaml mounted
```

## Architecture Overview

This is a collection of Python utilities for IoT data aggregation, weather monitoring, and DNS automation. The codebase follows a modular structure where each subdirectory is a self-contained Python package.

### Configuration System

- **config.py**: Pydantic-based settings loader using `pydantic-settings`
- **config.yaml**: Default configuration (sample values, committed to repo)
- **config.local.yaml**: Local overrides for secrets (not committed, merged at runtime via `Helper.update_deep()`)
- Environment variables can override config using `SOMESTUFF_` prefix or nested delimiter `__`

### Key Modules

| Module | Purpose |
|--------|---------|
| `dnsstuff/pcbwaydnsstuff.py` | SPF record crawler → ipset updater for SMTP allowlisting |
| `ecowittstuff/ecowittapi.py` | Ecowitt weather station API client (typed with Pydantic) |
| `hydromailstuff/hydromail.py` | Assembles status emails from MQTT/Netatmo data |
| `netatmostuff/lnetatmo.py` | Netatmo weather data client |
| `llmstuff/` | LLM API helpers (Google Gemini, Anthropic, Ollama OCR) |
| `dinogame/` | Grid pathfinding visualization (A* experiments) |

### Shared Utilities

- **Helper.py**: JSON serialization (`ComplexEncoder`), deep dict merging
- **mqttstuff**: External package (PyPI: `mqttstuff`) for MQTT topic management

### Docker Image

- Base: `python:3.14-slim-trixie`
- Runs as non-root user `pythonuser` (UID 1200)
- Multi-arch: linux/amd64, linux/arm64
- Entry point uses `tini` for proper signal handling
- `PYTHONPATH=/app` is set; all modules copied to `/app/`

## Code Style

- Black formatter with 120 character line length
- Mypy for static type checking (excludes .venv, tests)
- Pre-commit hooks: yaml validation, black --check, mypy
- Loguru for logging (configured in config.py with custom format)
