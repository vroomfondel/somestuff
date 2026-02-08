[![mypy and pytests](https://github.com/vroomfondel/somestuff/actions/workflows/mypynpytests.yml/badge.svg)](https://github.com/vroomfondel/somestuff/actions/workflows/mypynpytests.yml)
[![BuildAndPushMultiarch](https://github.com/vroomfondel/somestuff/actions/workflows/buildmultiarchandpush.yml/badge.svg)](https://github.com/vroomfondel/somestuff/actions/workflows/buildmultiarchandpush.yml)
![Cumulative Clones](https://img.shields.io/endpoint?logo=github&url=https://gist.githubusercontent.com/vroomfondel/22c802be25a8241e81e98a28d00c6036/raw/somestuff_clone_count.json)
[![Docker Pulls](https://img.shields.io/docker/pulls/xomoxcc/somestuff?logo=docker)](https://hub.docker.com/r/xomoxcc/somestuff/tags)

[![Gemini_Generated_Image_somestuff_64yrkh64yrkh64yr_250x250.png](Gemini_Generated_Image_somestuff_64yrkh64yrkh64yr_250x250.png)](https://hub.docker.com/r/xomoxcc/somestuff/tags)

# somestuff

Various small but useful Python utilities, experiments, and scripts collected in one place. Some are stand‑alone command‑line tools, others are small libraries or demo apps. A Docker image is provided to bundle the collection for easy, reproducible execution on multiple CPU architectures.

Quick links:
- Project Docker image: built from the repo‑root `Dockerfile` and published via multi‑arch builds
- CI: mypy + pytest, and a multi‑arch Docker build/push workflow (see badges above)

Contents overview (Python packages/modules):
- `dinogame`: pathfinding/visualization playground inspired by "The Farmer Was Replaced"
- `dnsstuff`: SPF resolution helper and ipset updater for allow‑listing email senders (e.g. pcbway.com)
- `ecowittstuff`: simple client/types for Ecowitt weather station API
- `gcalstuff`: CLI tool for creating Google Calendar events with day‑view confirmation
- `hydromailstuff`: assemble and send "hydro"/weather summary emails, pulling data from MQTT/Netatmo
- `k3shelperstuff`: K3s kubeconfig credential synchronization utility
- `llmstuff`: helpers for working with LLM APIs and local OCR
- `dhcpstuff`: DHCP discover tool and diagnostic script for unwanted DHCP on Linux
- `netatmostuff`: Netatmo data fetch helper and deployment example
- `sipstuff`: SIP caller — place phone calls and play WAV files via PJSUA2
- Root helpers: `Helper.py`, configs (`config.yaml`, `config.py`, optional `config.local.yaml`), scripts
- External packages: `mqttstuff` and `reputils` (via PyPI)

Standalone Docker image sub‑projects (each with own `Dockerfile` and `build.sh`):
- `tangstuff`: Tang server for LUKS/Clevis network‑bound disk encryption
- `mosquitto-2.1`: Mosquitto 2.1 MQTT broker with dynamic security
- `python314jit`: Python 3.14 base image with JIT support
- `python314pandasmultiarch`: Python 3.14 base image with pandas (multi‑arch)
- `nfs-subdir-external-provisioner`: Kubernetes NFS provisioner (external submodule with local overlay)


## Getting started

Local prerequisites:
- Python 3.14+ recommended (repo and Docker image currently use `python:3.14-trixie`)
- `pip` and a virtualenv of your choice

Install dependencies:
```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Configuration:
- `config.yaml` contains sample/defaults. Do not commit secrets. If needed, create `config.local.yaml` to override locally (kept out of the image by default). Some modules read credentials/API keys from here (e.g. Ecowitt, Netatmo, MQTT, email settings). Adjust as needed before running modules that require it.

Run tests and type checks:
```
pytest -q
mypy .
```

Secret scanning with [gitleaks](https://github.com/gitleaks/gitleaks):
- Gitleaks runs as a pre‑commit hook (see `.pre-commit-config.yaml`) and catches secrets before they are committed.
- The repo‑level `.gitleaks.toml` extends the default ruleset (`[extend] useDefault = true`) and allowlists paths that are expected to contain local‑only or generated content (`.venv`, `.mypy_cache`, `.idea`, `__pycache__`, `*local*` files, `docker-config`).
- To scan the entire repo on demand:
```
make gitleaks
# or directly:
gitleaks dir . -v
```


## Modules and their usefulness

### dhcpstuff
DHCP discovery and diagnostic tools. Includes a Python DHCP Discover sender that displays all responses (including Proxy DHCP/PXE boot servers) and a bash diagnostic script for finding unwanted DHCP on physical interfaces (Ubuntu Server).

- CLI usage (requires root):
```bash
sudo python -m dhcpstuff -i eth0 -a efi64
```
- Flags: `-i` interface, `-t` timeout, `-m` MAC, `-a` PXE architecture (`bios`, `efi64`, `efi64-http`).
- The bash script `diagnose-dhcp.sh` checks cloud-init, netplan, systemd-networkd, NetworkManager, kernel cmdline, leases, and hooks for unwanted DHCP sources.
- No external dependencies (stdlib only).
- Usefulness: quickly identify all DHCP/PXE servers on a network segment, or diagnose why a Linux host is unexpectedly obtaining a DHCP lease.


### dinogame
Pathfinding toy project and visualization to prototype search/planning strategies on a grid world, loosely inspired by “The Farmer Was Replaced”. Useful to experiment with A* heuristics and safe‑move constraints while visualizing planning vs execution.

- Entrypoint example: `dinogame/dinogame.py` contains a `main()` that renders a GIF of a planning/execution sequence.
- Requirements: `matplotlib`, `numpy` (`matplotlib` is commented out in `requirements.txt` by default; enable it to use this module).
- Run locally:
```
python -m dinogame.dinogame
```
This saves an animated GIF (via pillow) to your desktop by default; see code comments to tweak world size and frame count.


### dnsstuff (pcbwaydnsstuff)
Script to crawl SPF records (including nested `include:` chains) for one or more domains, resolve them to IPv4 ranges/addresses, then update an `ipset`. The ipset can be used by your MTA/firewall to allowlist specific SMTP sources. Originally motivated by receiving mails from pcbway.com while using country‑based IP blocks.

- CLI usage:
```
python -m dnsstuff.pcbwaydnsstuff pcbway.com mail-notify.pcbway.com
```
- Behavior:
  - Prints resolved IPv4s to stdout for each domain and combined total.
  - If running as root (`UID 0`), updates/swap‑refreshes an ipset named `smtpallowlist` (configurable in code) using `pyroute2.ipset`.
  - If not root, it only reports and skips the ipset update step.
- Dependencies: `dnspython`, `pyroute2`.
- Usefulness: automate building an SMTP allowlist from domains you trust, keeping it fresh even with complex SPF chains.


### ecowittstuff
Small client types and functions to call the Ecowitt API and parse real‑time weather data into typed models.

- Main bits: `ecowittstuff/ecowittapi.py` defines `pydantic` models (e.g. `WeatherStationResponse`) and `get_realtime_data()`.
- Config required: `ecowitt.application_key`, `ecowitt.api_key`, `ecowitt.mac`, URLs in `config.yaml`.
- Example:
```
python -c "from ecowittstuff.ecowittapi import get_realtime_data; print(get_realtime_data())"
```
- Usefulness: convenient, typed access to your Ecowitt station’s real‑time metrics for dashboards, alerts, and reports.


### gcalstuff
CLI tool for creating Google Calendar events via OAuth2 Desktop flow. Shows all existing events for the target day across all user calendars and requires explicit confirmation before creating the new event.

- Entrypoint: `gcalstuff/gcal_event.py`
- Setup: download OAuth2 Desktop credentials from Google Cloud Console to `~/.config/gcal/credentials.json`. On first run the browser‑based OAuth flow creates `~/.config/gcal/token.json`.
- CLI usage:
```
python -m gcalstuff.gcal_event "Meeting" 2025-07-15 14:00
python -m gcalstuff.gcal_event "Lunch" 2025-07-15 12:00 -d 90 --description "Team lunch"
```
- Dependencies: `google-auth`, `google-auth-oauthlib`, `google-api-python-client`.
- Docker: the initial OAuth flow opens a browser, so run it on the host first. After that, mount `~/.config/gcal` into the container (read‑write, since the token gets refreshed). The `dstart` Makefile target already includes this mount.
- Usefulness: quick calendar event creation from the terminal with conflict visibility.


### hydromailstuff
Compose and send a status email summarizing recent measurements (e.g., water level, rain totals, ambient data). Pulls latest values from MQTT topics and Netatmo.

- Entrypoint: `hydromailstuff/hydromail.py` → `do_main_stuff()`.
- Needs config: email settings, MQTT (`mqtt.*`), and Netatmo credentials under `netatmo.*` in `config.yaml`.
- There are example deployment/cron YAMLs in `hydromailstuff/somestuff_hydromail_cronjob.yml`.
- Usefulness: periodic email digests for environmental monitoring.


### llmstuff
Helper utilities to interact with LLM providers or local OCR pipelines.

- Files: `llmstuff/llmhelper.py`, `llmbatchhelper.py`, `ollamadeepseekocr.py`.
- Config: keys under `google.*` and `anthropic.*` in `config.yaml` if you want to call those providers.
- Usefulness: quick building blocks when experimenting with LLMs and OCR.


### mqttstuff (external package)
Minimal MQTT wrapper(s) and helpers. **This is now an external package** — see [GitHub](https://github.com/vroomfondel/mqttstuff) and [PyPI](https://pypi.org/project/mqttstuff/).

- Config: `mqtt.*` and `mqtt_topics.*` in `config.yaml` (topics include metadata such as subscribe flag and default metadata).
- Usefulness: standardize how topics are named and read/published across scripts.


### netatmostuff
Read Netatmo measurements and provide a deployment example.

- Files: `netatmostuff/lnetatmo.py`, deployment example `somestuff_netatmo_deployment.yml`.
- Config: `netatmo.username/password/client_id/client_secret` (and optional module IDs) in `config.yaml`.
- Usefulness: sourcing outdoor temperature, rainfall, pressure, etc., for dashboards or mail digests.


### k3shelperstuff
K3s kubeconfig credential synchronization utility. Fetches the kubeconfig from a remote K3s server via SSH, compares user credentials and cluster CA data against the local `~/.kube/config`, and interactively updates any differences.

- Entrypoint: `k3shelperstuff/update_local_k3s_keys.py`
- CLI usage:
```
python -m k3shelperstuff.update_local_k3s_keys
python -m k3shelperstuff.update_local_k3s_keys -H myserver -c my-k3s-context
```
- Remote host and context are auto‑detected from the current‑context in the local kubeconfig if not provided.
- Docker: mount `~/.kube` into the container (read‑write, since the script updates the local kubeconfig). The `dstart` Makefile target already includes this mount. The script SSHs to the remote host, so `~/.ssh` must also be accessible.
- Usefulness: keep local kubeconfig credentials in sync with a remote K3s server after certificate rotation.


### sipstuff
SIP caller module using PJSUA2. Registers with a SIP/PBX server, dials a destination, plays a WAV file or TTS‑generated speech on answer, and hangs up. Designed for headless/container operation (null audio device, no sound card required). Supports UDP, TCP, and TLS transports with optional SRTP media encryption. Text‑to‑speech via [piper TTS](https://github.com/rhasspy/piper) — no pre‑recorded audio file needed.

- CLI usage:
```bash
# WAV playback
python -m sipstuff.cli call --server pbx.local --user 1000 --password secret --dest +491234567890 --wav alert.wav

# TTS (auto‑downloads voice model on first use)
python -m sipstuff.cli call --server pbx.local --user 1000 --password secret --dest +491234567890 \
    --text "Achtung! Wasserstand kritisch!" --tts-data-dir /data/piper
```
- Library usage:
```python
from sipstuff import make_sip_call
make_sip_call(server="pbx.local", user="1000", password="secret", destination="+49123", wav_file="alert.wav")
make_sip_call(server="pbx.local", user="1000", password="secret", destination="+49123", text="Server offline!")
```
- Config: YAML file, `SIP_*` environment variables, or direct arguments. Priority: overrides > env > YAML.
- Dependencies: `pjsua2` (PJSIP Python bindings, built from source in Docker), `pydantic`, `ruamel.yaml`, `loguru`.
- Docker: PJSIP is compiled in a multi‑stage build (stage 1); piper‑tts runs in a separate Python 3.12 venv (stage 2, because `piper-phonemize` has no 3.14 wheels). Both are copied into the final image.
- See `sipstuff/README.md` for full CLI flags, Docker examples, and library API.
- Usefulness: automated alert/notification calls from scripts, cron jobs, or monitoring systems.


### flickrdownloaderstuff (moved)
The Flickr photo backup functionality has been moved to a standalone repository:
**[github.com/vroomfondel/flickrtoimmich](https://github.com/vroomfondel/flickrtoimmich)**

The Docker image is still available at [Docker Hub: xomoxcc/flickr-download](https://hub.docker.com/r/xomoxcc/flickr-download/tags).


### Root helpers and configuration

- `Helper.py`: JSON helpers including a `ComplexEncoder` to serialize datetimes, UUIDs, and custom objects with `repr_json()`/`as_string()`.
- `config.py`: config loader/merger; use `config.local.yaml` to keep secrets out of VCS and override defaults.
- `scripts/update_badge.py`: helper used in CI to update the clones badge.
- `tests/`: basic test scaffold.


## Docker: build process, use, and usefulness

There is a single Docker image defined by the repo‑root `Dockerfile`. The image:
- Uses `python:3.14-slim-trixie` base (commented alternatives exist for 3.13/ PyPy).
- Multi‑stage build: stage 1 compiles PJSIP with Python bindings, stage 2 creates a Python 3.12 venv for piper‑tts (needed because `piper-phonemize` lacks 3.14 wheels), stage 3 assembles the final image.
- Installs a few system tools (`htop`, `dnsutils`, `tini`, `ffmpeg`, etc.) and Python deps via `requirements.txt`.
- Creates a non‑root user (`pythonuser`, configurable via build args `UID`, `GID`, `UNAME`).
- Copies the packages into `/app` and sets `PYTHONPATH` accordingly.
- Accepts build‑time metadata args and exports them as envs: `GITHUB_REF`, `GITHUB_SHA`, `BUILDTIME`.
- Entrypoint is `tini --`, default `CMD` tails the log (adjust for your workload).

Why this is useful:
- Reproducible environment across machines/architectures.
- Non‑root execution by default improves container security.
- Multi‑arch support (amd64 + arm64) for running on laptops, servers, and SBCs alike.

### Local build (simple)
```
docker build \
  --build-arg buildtime="$(date +'%Y-%m-%d %H:%M:%S %Z')" \
  -t xomoxcc/somestuff:python-3.14-slim-trixie \
  .
```

Run interactively (example):
```
docker run --rm -it \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  xomoxcc/somestuff:python-3.14-slim-trixie \
  python -m dnsstuff.pcbwaydnsstuff pcbway.com
```

### Local build and multi‑arch push via buildx
This repo includes a helper script `build.sh` that:
- Logs in (expects `DOCKER_TOKENUSER` and `DOCKER_TOKEN` in env on first run)
- Ensures a `buildx` builder exists (and installs binfmt/qemu if needed)
- Builds locally and also performs a `docker buildx build --platform linux/amd64,linux/arm64 --push` with tags
- Writes local build logs to `docker_build_local.log`

Usage:
```
./build.sh            # multi‑arch build & push
./build.sh onlylocal  # local build only (no push)
```

The script also sets `DOCKER_CONFIG` to the bundled `docker-config/` directory so the builder state is isolated per‑repo. The primary tag is `xomoxcc/somestuff:python-3.14-slim-trixie` and an additional `:latest` tag is automatically added if missing.

### Check Docker Hub token permissions
Verify which permissions (pull, push, delete) a Docker Hub token has for all repositories in a namespace:
```
make check-dockerhub-token
```
This uses `DOCKER_TOKENUSER` and `DOCKER_TOKEN` from `scripts/include.sh` (overridden by `scripts/include.local.sh`). Additional namespaces defined in the `DOCKERHUB_NAMESPACES` bash array are passed automatically. The script can also be called directly:
```
python3 scripts/check_dockerhub_token.py <username> <token> -n <extra-namespace> -n <another>
```

### Update Docker Hub READMEs
To update the Docker Hub repository descriptions from the `DOCKERHUB_OVERVIEW.md` files:
```
make update-all-dockerhub-readmes
```
This updates all Docker Hub repos (somestuff, python314-jit, pythonpandasmultiarch, mosquitto, tang) using the credentials from `docker-config/config.json`.

### GitHub Actions
- `.github/workflows/buildmultiarchandpush.yml` builds and pushes multi‑arch images. Triggers automatically after a successful mypy/pytest run (only rebuilds sub‑project images when their directory changed). Can also be triggered manually via **Actions → BuildAndPushMultiarch → Run workflow**, where checkboxes let you select which images to build (main somestuff, mosquitto, tang, python314‑jit, pythonpandasmultiarch).
- `.github/workflows/mypynpytests.yml` runs mypy + pytest.
- `.github/workflows/checkblack.yml` checks code style.
- `.github/workflows/update-clone-badge.yml` updates the clones badge.


## Standalone Docker Image Sub‑projects

Each sub‑project has its own `Dockerfile`, `build.sh`, and `README.md`:

### python314jit
Python 3.14 base image with JIT support for experimenting with Python 3.14's new features and performance.
- [Docker Hub](https://hub.docker.com/r/xomoxcc/python314-jit/tags)
- See `python314jit/README.md` for details.

### python314pandasmultiarch
Python 3.14 base image with pandas pre‑installed, built for multi‑arch (amd64 + arm64).
- [Docker Hub](https://hub.docker.com/r/xomoxcc/pythonpandasmultiarch/tags)
- See `python314pandasmultiarch/README.md` for details.

### mosquitto-2.1
Mosquitto 2.1 MQTT broker with dynamic security plugin, multi‑arch build.
- [Docker Hub](https://hub.docker.com/r/xomoxcc/mosquitto/tags)
- See `mosquitto-2.1/README.md` for details.

### tangstuff
Tang server for LUKS/Clevis network‑bound disk encryption (NBDE), enabling automatic disk unlock on trusted networks.
- [Docker Hub](https://hub.docker.com/r/xomoxcc/tang/tags)
- See `tangstuff/README.md` for details.

### nfs-subdir-external-provisioner
Kubernetes NFS external provisioner (git submodule from upstream). Local modifications are stored in `overlays/nfs-subdir-external-provisioner/` and applied at build time.
- Build: `make build-nfs` (applies overlay, then builds)
- See upstream repo for documentation.

## Versioning
This is a living collection; no strict semantic versioning. Expect occasional breaking changes. A rough, humorous version might be “-0.42”.


## License
This project is licensed under the LGPL where applicable/possible — see [LICENSE.md](LICENSE.md). Some files/parts may use other licenses: [MIT](LICENSEMIT.md) | [GPL](LICENSEGPL.md) | [LGPL](LICENSELGPL.md). Always check per‑file headers/comments.


## Authors
- Repo owner (primary author)
- Additional attributions are noted inline in code comments


## Acknowledgments
- Inspirations and snippets are referenced in code comments where appropriate.


## ⚠️ Note

This is a development/experimental project. For production use, review security settings, customize configurations, and test thoroughly in your environment. Provided "as is" without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose and noninfringement. In no event shall the authors or copyright holders be liable for any claim, damages or other liability, whether in an action of contract, tort or otherwise, arising from, out of or in connection with the software or the use or other dealings in the software. Use at your own risk.