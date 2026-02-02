[![Docker Pulls](https://img.shields.io/docker/pulls/xomoxcc/flickr-download?logo=docker)](https://hub.docker.com/r/xomoxcc/flickr-download/tags)

# flickrdownloaderstuff

Docker/Podman wrapper for backing up Flickr photo libraries using [`flickr_download`](https://github.com/beaufour/flickr-download) with browser-based OAuth authentication (X11, domain socket, or D-Bus).

## What it does

`flickr-docker.sh` builds a container image with `flickr_download`, Chromium, Firefox ESR, and ExifTool, then runs it with X11 forwarding so the OAuth browser login works on the host display. Downloads are saved with JSON metadata and EXIF data intact.

`flickr_download` is installed from GitHub (not PyPI) to pick up an unreleased fix ([#166](https://github.com/beaufour/flickr-download/issues/166)) that gracefully skips photos when the requested size is unavailable instead of crashing the entire download.

Photos with an unknown taken date (`"0000-00-00 00:00:00"` from Flickr) crash `dateutil.parser.parse()`. The Dockerfile patches `flickr_download` with `sed` at build time. In integrated mode (where `flickr_download` is pip-installed and the filesystem is read-only) `run_direct()` monkeypatches `set_file_time` at runtime to skip these dates instead.

- Builds a Dockerfile based on `python:3.14-slim` with all X11/browser dependencies
- Handles `xauth` cookie forwarding (supports both X11 and XWayland)
- Auto-detects Docker or Podman and adjusts runtime flags accordingly
- Interactive first-run setup prompts for Flickr API key and secret
## Prerequisites

- Docker or Podman (auto-detected)
- **One of:** X11 display + `xauth`, OR domain socket mode (`USE_DSOCKET`), OR D-Bus mode (`USE_DBUS`) — see [Browser modes](#browser-modes)
- Flickr API key from <https://www.flickr.com/services/apps/create/>

## Setup

```bash
# 1. Build the container image
./flickr-docker.sh build

# 2. Authenticate (opens browser for OAuth, prompts for API key on first run)
./flickr-docker.sh auth
```

API credentials are stored in `flickr-config/.flickr_download`, the OAuth token in `flickr-config/.flickr_token`.

## Usage

```bash
./flickr-docker.sh <command> [options]
```

| Command | Description |
|---|---|
| `build` | Build the container image |
| `auth` | Authenticate via OAuth (opens browser) |
| `download <user>` | Download all albums for a Flickr user |
| `album <id>` | Download a single album by ID |
| `list <user>` | List albums for a Flickr user |
| `shell` | Open an interactive shell inside the container |
| `test-browser [url]` | Test X11 forwarding by opening a browser |
| `info` | Show system/config diagnostics |
| `clean` | Remove the container image and temp files |

The `BROWSER` environment variable selects which browser to use inside the container (default: `chrome`, options: `chrome`, `chromium`, `firefox`):

```bash
BROWSER=firefox ./flickr-docker.sh auth
```

## Browser modes

The script supports three modes for the OAuth browser flow on Linux. Mac/Windows always print the URL to the terminal for manual opening.

| Mode | Env var | How it works |
|---|---|---|
| X11 (default on Linux) | — | Forwards X11 display into the container; browser opens inside the container and renders on the host |
| Domain socket | `USE_DSOCKET=true` | Host-side Python listener on a Unix socket; container sends the URL, host opens it with `xdg-open`. No X11 needed |
| D-Bus portal | `USE_DBUS=true` | Mounts the host D-Bus session socket; container calls the XDG Desktop Portal `OpenURI` method via `gdbus`. Podman recommended (Docker may fail D-Bus auth due to UID mismatch) |

The modes are **mutually exclusive** — setting both `USE_DSOCKET` and `USE_DBUS` is an error.

Related environment variables:

| Variable | Default | Description |
|---|---|---|
| `USE_DSOCKET` | `false` | Enable domain socket mode |
| `DSOCKET_PATH` | `/tmp/.flickr-open-url.sock` | Host-side socket path |
| `USE_DBUS` | `false` | Enable D-Bus portal mode |

## Docker image files

Scripts copied into the container image at build time:

| File | Container path | Description |
|---|---|---|
| `flickr-docker.sh` | `/usr/local/bin/flickr-docker.sh` | Main wrapper script (download, auth, album management) |
| `flickr-download-wrapper.py` | `/usr/local/bin/flickr-download-wrapper.py` | Rate-limit backoff wrapper around `flickr_download` |
| `flickr-list-albums.py` | `/usr/local/bin/flickr-list-albums.py` | Album listing with photo/video counts |
| `url-opener` | `/usr/local/bin/url-opener` | Forwards browser-open requests to the host via a Unix socket (`USE_DSOCKET` mode) |
| `url-dbus-opener` | `/usr/local/bin/url-dbus-opener` | Opens a URL on the host via XDG Desktop Portal D-Bus (`USE_DBUS` mode) |
| `entrypoint.sh` | `/entrypoint.sh` | Container entrypoint; routes `shell` to bash, everything else to `flickr-docker.sh` |

## Data directories

| Directory | Contents |
|---|---|
| `flickr-backup/` | Downloaded photos and JSON metadata |
| `flickr-config/` | API credentials and OAuth token |
| `flickr-cache/` | API response cache for resumable downloads |

These directories are created next to the script and are **not** removed by `clean`.

## Rate-limit backoff

`flickr_download` has no built-in retry for Flickr API `429 Too Many Requests` responses -- it logs an error, skips the photo, and continues immediately, which keeps hitting the rate limit and skips many photos.

The wrapper detects `HTTP Error 429` in the output and responds by sending `SIGSTOP` to freeze the `flickr_download` process, sleeping with increasing backoff, then sending `SIGCONT` to resume. The backoff resets after any successful (non-429) output line.

| Variable | Default | Description |
|---|---|---|
| `BACKOFF_BASE` | `60` | Base wait in seconds; multiplied by consecutive 429 count |
| `BACKOFF_MAX` | `600` | Cap on the wait time |
| `BACKOFF_EXIT_ON_429` | `false` | Exit immediately (code 42) instead of sleeping; useful for CI / Kubernetes Jobs |

Example output when a rate limit is hit:

```
[WARN] Rate limit hit (#1), suspending for 60s...
[INFO] Resuming download...
[WARN] Rate limit hit (#2), suspending for 120s...
[INFO] Resuming download...
```

This applies to `download` and `album` commands in both in-container and host modes. Interactive commands (`auth`, `shell`, `list`) are not wrapped.

## Integrated mode (somestuff container)

`flickr_download` and ExifTool are also installed in the main `xomoxcc/somestuff` Docker image. `flickr-docker.sh` auto-detects when it runs inside the container (via `FLICKR_HOME`, container marker files, or `KUBERNETES_SERVICE_HOST`) and calls `flickr_download` directly — no nested container build/run needed. This means the script also works as an entrypoint in Kubernetes Jobs without special flags.

**Auth** works inside the container — the OAuth URL is printed to stdout so you can open it in a browser on the host. Alternatively, authenticate on the host before starting the container:

```bash
cd flickrdownloaderstuff && ./flickr-docker.sh auth
```

**Download** inside the container:

```bash
make dstart
# inside the container:
/app/flickrdownloaderstuff/flickr-docker.sh download USERNAME
/app/flickrdownloaderstuff/flickr-docker.sh list USERNAME
/app/flickrdownloaderstuff/flickr-docker.sh album 72157622764287329
```

Commands that manage the nested image (`build`, `clean`, `shell`) are no-ops with an informational message.

Directory mapping (`make dstart`, only mounted when the host directory exists):

| Host path | Container path |
|---|---|
| `flickrdownloaderstuff/flickr-config/` | `/home/pythonuser/.flickr-config` (ro) |
| `flickrdownloaderstuff/flickr-backup/` | `/home/pythonuser/flickr-backup` |
| `flickrdownloaderstuff/flickr-cache/` | `/home/pythonuser/flickr-cache` |

## Kubernetes deployment

`kubectlstuff_flickr_downloader.yml` is an Ansible playbook (applied via `ansible-playbook kubectlstuff.yml --tags flickr_downloader`) that deploys per-user Flickr download Jobs and an operator that restarts them after rate-limit failures.

**What it creates:**

- A `flickr-downloader` namespace
- One Kubernetes `Job` per user (`flickr-downloader-<user>`), each running the container image with `BACKOFF_EXIT_ON_429=true` so the Job exits on rate limits instead of sleeping
- A `flickr-operator` Deployment that watches those Jobs; after a configurable delay (default 1 hour) it deletes and recreates failed Jobs

**Ansible variables** (defined in `roles/kubectlstuff/defaults/main.yml`):

| Variable | Description |
|---|---|
| `flickr_users` | List of Flickr usernames to back up |
| `flickr_host_path_prefix` | Base path on the host for per-user config/backup/cache directories |
| `flickr_download_image` | Container image to use for download Jobs |
| `flickr_dockerconfigjson` | Base64-encoded Docker registry credentials |
| `flickr_operator_check_interval` | Seconds between operator check loops (default `60`) |
| `flickr_operator_restart_delay` | Seconds to wait after a Job fails before restarting it (default `3600`) |

**Volume mounts** per Job (hostPath):

| Host path | Container path |
|---|---|
| `<prefix>/<user>/flickr-config` | `/home/poduser` |
| `<prefix>/<user>/flickr-backup` | `/home/poduser/flickr-backup` |
| `<prefix>/<user>/flickr-cache` | `/home/poduser/flickr-cache` |

## Immich upload

`upload.sh` uploads downloaded Flickr photos and videos to an [Immich](https://immich.app/) instance, creating one Immich album per Flickr album directory. It uses `@immich/cli` (installed at runtime via npm).

**Container detection** uses the same markers as `flickr-docker.sh` (`/.dockerenv`, `/run/.containerenv`, `KUBERNETES_SERVICE_HOST`). When running inside a container, the script executes `@immich/cli` directly. On the host it spins up a `node:lts-alpine` Podman container with the photo directory mounted read-only.

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `IMMICH_INSTANCE_URL` | — | Immich server URL (required in host/podman mode) |
| `IMMICH_API_KEY` | — | Immich API key (required in host/podman mode) |
| `DATA_DIR` | `$(pwd)/flickr-backup` (in-container) / `/data` (podman) | Directory containing album subdirectories |

**Usage — host mode** (launches a Podman container automatically):

```bash
IMMICH_INSTANCE_URL=https://immich.example.com IMMICH_API_KEY=secret ./upload.sh
```

**Usage — inside a container** (e.g. via `make dstart`):

```bash
IMMICH_INSTANCE_URL=https://immich.example.com IMMICH_API_KEY=secret \
  /app/flickrdownloaderstuff/upload.sh
```

Supported file types: `.jpg`, `.png`, `.mp4`.

## Podman notes

When Podman is detected the script automatically adds:

- `--userns=keep-id` for correct X11 access with the host UID
- `--security-opt label=disable` for SELinux compatibility
- Config is mounted to `/home/poduser` instead of `/root` and `HOME` is set accordingly

Running the published image directly (without the wrapper script):

```bash
podman run --rm -it --userns=keep-id \
  -e "HOME=/home/poduser" \
  -v "$(pwd)/flickr-config:/home/poduser" \
  -v "$(pwd)/flickr-backup:/home/poduser/flickr-backup" \
  -v "$(pwd)/flickr-cache:/home/poduser/flickr-cache" \
  docker.io/xomoxcc/flickr-download:latest list <username>
```

## Publishing

Multi-arch build and push to Docker Hub (amd64 + arm64):

```bash
./build_multiarch.sh              # build and push to Docker Hub
./build_multiarch.sh onlylocal    # local build only (no push)
```

Docker Hub credentials are read from `scripts/include.local.sh` (via `scripts/include.sh`). The primary tag is `xomoxcc/flickr-download:python-3.14-slim` and an additional `:latest` tag is automatically added.
