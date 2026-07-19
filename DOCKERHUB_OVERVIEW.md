[![mypy and pytests](https://github.com/vroomfondel/somestuff/actions/workflows/mypynpytests.yml/badge.svg)](https://github.com/vroomfondel/somestuff/actions/workflows/mypynpytests.yml)
[![BuildAndPushMultiarch](https://github.com/vroomfondel/somestuff/actions/workflows/buildmultiarchandpush.yml/badge.svg)](https://github.com/vroomfondel/somestuff/actions/workflows/buildmultiarchandpush.yml)
![Cumulative Clones](https://img.shields.io/endpoint?logo=github&url=https://gist.githubusercontent.com/vroomfondel/22c802be25a8241e81e98a28d00c6036/raw/somestuff_clone_count.json)

[![Gemini_Generated_Image_somestuff_64yrkh64yrkh64yr_250x250.png](https://raw.githubusercontent.com/vroomfondel/somestuff/main/Gemini_Generated_Image_somestuff_64yrkh64yrkh64yr_250x250.png)](https://github.com/vroomfondel/somestuff)

# somestuff

Various small but useful Python utilities, experiments, and scripts collected in one place. Some are stand‑alone command‑line tools, others are small libraries or demo apps. A Docker image is provided to bundle the collection for easy, reproducible execution on multiple CPU architectures.

Quick links:
- Project Docker image: built from the repo‑root `Dockerfile` and published via multi‑arch builds
- CI: mypy + pytest, and a multi‑arch Docker build/push workflow (see badges above)

Contents overview (Python packages/modules):
- `dinogame`: pathfinding/visualization playground inspired by “The Farmer Was Replaced”
- `dnsstuff`: SPF resolution helper and ipset updater for allow‑listing email senders (e.g. pcbway.com)
- `ecowittstuff`: simple client/types for Ecowitt weather station API
- `gcalstuff`: CLI tool for creating Google Calendar events with day‑view confirmation
- `hydromailstuff`: assemble and send "hydro"/weather summary emails, pulling data from MQTT/Netatmo
- `k3shelperstuff`: K3s kubeconfig credential synchronization utility
- `llmstuff`: helpers for working with LLM APIs and local OCR
- `mqttstuff`: tiny MQTT wrapper utility
- `dhcpstuff`: DHCP discover tool and diagnostic script for unwanted DHCP on Linux
- `netatmostuff`: Netatmo data fetch helper and deployment example
- `oepnvstuff`: GTFS‑Realtime coverage checker — validates whether an open GTFS‑RT feed actually carries real‑time data for configurable lines/stations, with watch mode, departure board and MQTT publishing
- `mqttwebstuff`: live web view onto arbitrary MQTT streams — mountable mapper plugins (filter/map per message), server‑side last‑value cache, SSE push to an htmx/PicoCSS frontend; ships an oepnv departure‑board plugin and a generic view (indented topic tree by default, flat JSON cards via `--view flat`)
- `ucmstuff`: monitor and control a Grandstream UCM6204 IP‑PBX — real‑time call events over WebSocket plus request/response control via the HTTPS API
- `uptimekumastuff`: provision, migrate and back up Uptime Kuma 2.x declaratively — idempotent YAML apply, Ansible module, full export/import, direct Socket.IO client
- Root helpers: `Helper.py`, configs (`config.yaml`, `config.py`, optional `config.local.yaml`), scripts
- Moved to standalone repos: `sipstuff` ([vroomfondel/sipstuff](https://github.com/vroomfondel/sipstuff)), `flickrdownloaderstuff` ([vroomfondel/flickrtoimmich](https://github.com/vroomfondel/flickrtoimmich))
- External packages: `mqttstuff` and `reputils` (via PyPI)

Standalone Docker image sub‑projects (each with own `Dockerfile` and `build.sh`):
- `tangstuff`: Tang server for LUKS/Clevis network‑bound disk encryption
- `mosquitto-2.1`: Mosquitto 2.1 MQTT broker with dynamic security
- `python314jit`: Python 3.14 base image with JIT support
- `python314pandasmultiarch`: Python 3.14 base image with pandas (multi‑arch)
- `nfs-subdir-external-provisioner`: Kubernetes NFS provisioner (external submodule with local overlay)


## Getting started

Local prerequisites:
- Python 3.14+ recommended (repo and Docker image currently use `python:3.14-slim-trixie`)
- `pip` and a virtualenv of your choice

Install dependencies:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Configuration:
- `config.yaml` contains sample/defaults. Do not commit secrets. If needed, create `config.local.yaml` to override locally (kept out of the image by default). Some modules read credentials/API keys from here (e.g. Ecowitt, Netatmo, MQTT, email settings). Adjust as needed before running modules that require it.

Run tests and type checks:
```bash
pytest -q
mypy .
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
```bash
python -m dinogame.dinogame
```
This saves an animated GIF (via pillow) to your desktop by default; see code comments to tweak world size and frame count.


### dnsstuff (spf_ipset_updater)
Script to crawl SPF records (including nested `include:` chains) for one or more domains, resolve them to IPv4 ranges/addresses, then update an `ipset`. The ipset can be used by your MTA/firewall to allowlist specific SMTP sources. Originally motivated by receiving mails from pcbway.com while using country‑based IP blocks.

- CLI usage:
```bash
python -m dnsstuff.spf_ipset_updater [--ipset-name NAME] [--ipset-type TYPE] [--dry-run] [domain ...]
```
- Behavior:
  - Logs resolved IPv4s for each domain and combined total.
  - If running as root (`UID 0`), updates/swap‑refreshes the target ipset (default: `smtpallowlist`) using `pyroute2.ipset`.
  - If not root, it only reports and skips the ipset update step.
- Dependencies: `dnspython`, `pyroute2`.
- Usefulness: automate building an SMTP allowlist from domains you trust, keeping it fresh even with complex SPF chains.


### ecowittstuff
Small client types and functions to call the Ecowitt API and parse real‑time weather data into typed models.

- Main bits: `ecowittstuff/ecowittapi.py` defines `pydantic` models (e.g. `WeatherStationResponse`) and `get_realtime_data()`.
- Config required: `ecowitt.application_key`, `ecowitt.api_key`, `ecowitt.mac`, URLs in `config.yaml`.
- Example:
```bash
python -c "from ecowittstuff.ecowittapi import get_realtime_data; print(get_realtime_data())"
```
- Usefulness: convenient, typed access to your Ecowitt station’s real‑time metrics for dashboards, alerts, and reports.


### gcalstuff
CLI tool for creating Google Calendar events via OAuth2 Desktop flow. Shows all existing events for the target day across all user calendars and requires explicit confirmation before creating the new event.

- Entrypoint: `gcalstuff/gcal_event.py`
- Setup: download OAuth2 Desktop credentials from Google Cloud Console to `~/.config/gcal/credentials.json`. On first run the browser‑based OAuth flow creates `~/.config/gcal/token.json`.
- CLI usage:
```bash
python -m gcalstuff.gcal_event "Meeting" 2025-07-15 14:00
python -m gcalstuff.gcal_event "Lunch" 2025-07-15 12:00 -d 90 --description "Team lunch"
```
- Dependencies: `google-auth`, `google-auth-oauthlib`, `google-api-python-client`.
- Docker: the initial OAuth flow opens a browser, so run it on the host first. After that, mount `~/.config/gcal` into the container (read‑write, since the token gets refreshed).
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


### mqttstuff
Minimal MQTT wrapper(s) and helpers.

- File: `mqttstuff/mosquittomqttwrapper.py`.
- Config: `mqtt.*` and `mqtt_topics.*` in `config.yaml` (topics include metadata such as subscribe flag and default metadata).
- Usefulness: standardize how topics are named and read/published across scripts.
- **⚠️ Warning:** this is now an external module (https://github.com/vroomfondel/mqttstuff) and included as library via pypi (https://pypi.org/project/mqttstuff/)


### netatmostuff
Read Netatmo measurements and provide a deployment example.

- Files: `netatmostuff/lnetatmo.py`, deployment example `somestuff_netatmo_deployment.yml`.
- Config: `netatmo.username/password/client_id/client_secret` (and optional module IDs) in `config.yaml`.
- Usefulness: sourcing outdoor temperature, rainfall, pressure, etc., for dashboards or mail digests.


### k3shelperstuff
K3s kubeconfig credential synchronization utility. Fetches the kubeconfig from a remote K3s server via SSH, compares user credentials and cluster CA data against the local `~/.kube/config`, and interactively updates any differences.

- Entrypoint: `k3shelperstuff/update_local_k3s_keys.py`
- CLI usage:
```bash
python -m k3shelperstuff.update_local_k3s_keys
python -m k3shelperstuff.update_local_k3s_keys -H myserver -c my-k3s-context
```
- Remote host and context are auto‑detected from the current‑context in the local kubeconfig if not provided.
- Docker: mount `~/.kube` into the container (read‑write, since the script updates the local kubeconfig). The script SSHs to the remote host, so `~/.ssh` must also be accessible.
- Usefulness: keep local kubeconfig credentials in sync with a remote K3s server after certificate rotation.


### ucmstuff
Monitor and control a **Grandstream UCM6204** IP‑PBX from Python. The UCM exposes two independent interfaces with different session models, and this module talks to both:

- **WebSocket** (`/websockify`): the UCM *pushes* real‑time events (`ActiveCallStatus`, `ExtensionStatus`, `PbxStatus`, …). Handled by `UCMEventClient` (auto‑reconnect + heartbeat), authenticated with a **web** user.
- **HTTPS API** (`/api`): request/response *control* and queries (`acceptCall`, `refuseCall`, `Hangup`, `dialExtension`, CDR, …). Handled by `UCM6204`, authenticated with an **API** user. `UCM6204Rest` adds a named, typed method for every HTTPS‑API action (trunks, routes, IVRs, queues, paging, accounts, users, dialing/transfer, …).

To both monitor and control calls you run both clients together (the "coordinated" setup); `TrunkCallRouter` / `IncomingCall` route incoming calls on a trunk to a caller‑based branch.

- Files: `ucmstuff/ucm6204_api.py` (core + Typer CLI + `/healthz` server for K8s probes), `ucm6204_api_rest.py` (full typed API), `example_router.py`.
- CLI usage (repeatable `--trunk`; omit for monitor‑only):
```bash
python -m ucmstuff.ucm6204_api \
  --host ucm.example.lan --port 8089 \
  --web-user webuser --web-password '…' \
  --api-user cdrapi   --api-password '…' \
  --trunk MyTrunk
```
- Dependencies: `requests`, `typer`, `websocket-client`.
- Deployment: `somestuff_ucm6204_deployment.yml` — one pod = events + control, **outbound‑only** to the UCM (no service/ingress), `/healthz` on port 8070 backs the liveness/readiness probes. Config with real credentials goes in `*.local.*` (gitignored); commit only the `.example` variants.
- Note: both interfaces negotiate a weak Diffie‑Hellman group, so the clients lower the OpenSSL security level (`@SECLEVEL=1`) automatically. Real‑time events use the WebSocket model — the `url` report‑push parameter in Grandstream's older PDF guide is a dead legacy field on current firmware.
- Usefulness: build call‑routing / screening / monitoring automation on top of a UCM6204 (e.g. auto‑accept known callers, refuse spam, react to PBX status).


### uptimekumastuff
Provision, migrate and back up **Uptime Kuma 2.x** declaratively and idempotently — instead of clicking monitors together in the web UI. None of these tools *monitor* anything; they only create configuration, the monitoring itself is done by Kuma.

- `uptimekuma_client.py`: `KumaClient`, a direct Socket.IO client with guaranteed‑fresh reads and idempotent `upsert_monitor`/`upsert_notification` — the library everything else builds on. (The PyPI `uptime-kuma-api` library cannot *write* against Kuma 2.x and its reads come from a stale cache; see `uptimekumastuff/README.md` for the verified details.)
- `uptimekuma_apply.py`: applies a desired state from a YAML file against an existing instance — re‑runnable, references by *name* instead of ID, with `--check` (dry run) and `--prune`.
- `uptimekuma_monitor.py`: Ansible module wrapping `KumaClient` for idempotent per‑monitor management from a playbook (`state: present`/`absent`, full check‑mode support).
- `uptimekuma_simpleapi.py`: `SimpleKumaApi`, full export/import of a whole instance (monitors, notifications, tags, status pages) into a preferably **empty** target — for backup and migration.

CLI usage:
```bash
# dry run - show what would change, write nothing
python -m uptimekumastuff.uptimekuma_apply -f kuma_state.local.yml --check

# backup the current state / import it into a fresh instance (monitors paused)
python -m uptimekumastuff.uptimekuma_simpleapi export --url https://kuma.example.lan --out state.local.json
python -m uptimekumastuff.uptimekuma_simpleapi import --url http://127.0.0.1:3001 --in-file state.local.json --paused
```
- Credentials: `uptimekuma.local.env` (gitignored), real env vars, or `--username`/`--password`. The Socket.IO login accepts username+password only — API keys work exclusively for HTTP basic auth on `/metrics`.
- Dependencies: `python-socketio`, `python-dotenv`, `typer`, `PyYAML`, `uptime-kuma-api` (reads only).
- Usefulness: reproducible, version‑controllable Kuma configuration (YAML/Ansible) and safe full‑instance backup/migration.


### oepnvstuff
Validate whether an open **GTFS‑Realtime** feed actually carries real‑time data (delays / actual times) for specific transit lines at a specific station — open‑data aggregators like gtfs.de merge whatever agencies deliver, and for many lines that is *schedule only*. One‑shot check or `--watch` poll loop (GTFS‑RT is polled HTTP, with ETag/If‑Modified‑Since conditional requests and staleness detection via `FeedHeader.timestamp`), with pluggable `on_*` handlers and an optional MQTT bridge (per‑line status, departure board, alerts and stale events — all published non‑retained).

- Entrypoint: `oepnvstuff/check_realtime.py` (Typer CLI); library core in `monitor.py` (`RealtimeMonitor`), `gtfs_static.py`, `gtfs_realtime.py`, `mqtt_bridge.py`.
- CLI usage:
```bash
python -m oepnvstuff.check_realtime                       # one-shot report + exit code
python -m oepnvstuff.check_realtime --watch --mqtt        # poll loop, publish to MQTT
python -m oepnvstuff.check_realtime --lines "195,295" --station Ellerbek --show-stops
```
- Every option is also an `OEPNV_*` environment variable (CLI > env > `oepnv.local.env`), so the container is fully configurable per Deployment env.
- The static feed (~250 MB zip) is cached with persisted HTTP validators — repeat runs cost a `304` round‑trip, no re‑download.
- Exit codes: `0` realtime found, `2` schedule but no realtime (or stale abort), `3` line/stop not in schedule, `1` technical error.
- Deployment: `somestuff_oepnv_deployment.yml` + `somestuff-oepnv-secret.example.yaml` — watch mode with `OEPNV_STOP_ON_STALE=1` (exit 2 → restart) as self‑healing, cache on a volume, SIGTERM handled for clean shutdown.
- Dependencies: `requests`, `gtfs-realtime-bindings`, `typer`, `python-dotenv`, `mqttstuff` (only for the bridge), `loguru`, `tabulate`.
- Usefulness: know whether open‑data realtime is good enough for your stop *before* building dashboards/alerts on it — and if it is, get it onto MQTT continuously.


### mqttwebstuff
A small live web view onto arbitrary **MQTT** streams: the server subscribes to the broker, runs every message through a *mapper plugin* (a plain Python file — in Kubernetes simply mounted via ConfigMap) and pushes rendered HTML fragments to all connected browsers via **Server‑Sent Events**. Frontend is htmx (+ SSE extension) on PicoCSS — no frontend build or framework, all assets vendored (no CDN egress from the pod).

- Core: `serve.py` (Typer CLI), `hub.py` (last‑value cache + SSE fan‑out), `plugin_api.py` (the `ViewEvent` plugin contract), `webapp.py` (FastAPI: `/`, `/stream`, `/healthz`).
- The server‑side **last‑value cache** makes non‑retained live streams browsable: a freshly opened tab gets the full current board server‑rendered, then only deltas over SSE. Per‑item TTLs let vanished topics disappear from the board.
- Plugins decide filter/panel/grouping/template per message (one message may map to several board items) and may bring their own Jinja2 templates (`*.html.j2`, plugin dir is searched before the built‑ins). Without a plugin, a generic mode shows any topic tree as an **indented hierarchical tree** (branch rows per level, scalars inline, JSON collapsible) — or as flat JSON cards with `--view flat`.
- CLI usage:
```bash
# oepnv departure board (reference plugin, ships in the image)
python -m mqttwebstuff.serve --mapper mqttwebstuff/plugins/oepnv_view.py --mqtt-host broker.example.org

# ad-hoc: peek into any topic tree generically (indented tree, newest payload per topic wins)
python -m mqttwebstuff.serve --mapper "" --topics 'nodered/#' --item-ttl 0 --mqtt-host broker.example.org
```
- Every option is also an `MQTTWEB_*` environment variable (CLI > env > `mqttweb.local.env`); MQTT TLS options (CA, mTLS, insecure) mirror oepnvstuff's.
- Deployment: `somestuff_mqttweb_deployment.yml` (+ `somestuff-mqttweb-secret.example.yaml`) with health probes; for SSE behind nginx‑ingress disable proxy buffering and raise the read timeout (noted in the manifest).
- Dependencies: `fastapi`, `uvicorn`, `jinja2`, `mqttstuff`, `typer`, `python-dotenv`, `loguru`, `tabulate`.
- Usefulness: a zero‑maintenance live dashboard for any MQTT topic tree — and with a ~50‑line plugin file, a purpose‑built board (like the oepnv departure board) without touching the core.


### sipstuff (moved)
The SIP caller (phone calls with WAV playback or piper TTS via PJSUA2, call recording, speech‑to‑text) has been moved to a standalone repository:
**[github.com/vroomfondel/sipstuff](https://github.com/vroomfondel/sipstuff)**

With it, the PJSIP/piper build stages left this image — it is a plain single‑stage Python image again (see the Docker section below).


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

There is a single Docker image defined by the repo‑root `Dockerfile`. It is a plain **single‑stage** build on `python:3.14-slim-trixie` (the PJSIP/piper build stages left together with `sipstuff`, which is now a standalone repo):
- Installs system tools: `htop`, `procps`, `vim`, `tini`, `bind9-dnsutils`, `ipset`, `git`, `exiftool`, `iputils-ping`.
- Sets the `de_DE.UTF-8` locale and the `Europe/Berlin` timezone.
- Installs Python deps via `requirements.txt`.
- Creates a non‑root user (`pythonuser`, UID 1200, configurable via build args `UID`, `GID`, `UNAME`) and runs as that user.
- Copies the packages into `/app` and sets `PYTHONPATH=/app`.
- Accepts build‑time metadata args and exports them as envs: `GITHUB_REF`, `GITHUB_SHA`, `BUILDTIME`.
- Entrypoint is `tini --`, default `CMD` tails the log (adjust for your workload).

Why this is useful:
- Reproducible environment across machines/architectures.
- Non‑root execution by default improves container security.
- Multi‑arch support (amd64 + arm64) for running on laptops, servers, and SBCs alike.

### Local build (simple)
```bash
docker build \
  --build-arg buildtime="$(date +'%Y-%m-%d %H:%M:%S %Z')" \
  -t xomoxcc/somestuff:python-3.14-slim-trixie \
  .
```

Run interactively (example):
```bash
docker run --rm -it \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  xomoxcc/somestuff:python-3.14-slim-trixie \
  python -m dnsstuff.spf_ipset_updater pcbway.com
```

### Local build and multi‑arch push via buildx
This repo includes a helper script `build.sh` that:
- Logs in (expects `DOCKER_TOKENUSER` and `DOCKER_TOKEN` in env on first run)
- Ensures a `buildx` builder exists (and installs binfmt/qemu if needed)
- Builds locally and also performs a `docker buildx build --platform linux/amd64,linux/arm64 --push` with tags
- Writes local build logs to `docker_build_local.log`

Usage:
```bash
./build.sh            # multi‑arch build & push
./build.sh onlylocal  # local build only (no push)
```

The script also sets `DOCKER_CONFIG` to the bundled `docker-config/` directory so the builder state is isolated per‑repo. The primary tag is `xomoxcc/somestuff:python-3.14-slim-trixie` and an additional `:latest` tag is automatically added if missing.

### GitHub Actions
- `.github/workflows/buildmultiarchandpush.yml` builds and pushes multi‑arch images on CI.
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