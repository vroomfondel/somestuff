# flickrdownloaderstuff

Docker/Podman wrapper for backing up Flickr photo libraries using [`flickr_download`](https://github.com/beaufour/flickr-download) with X11-based OAuth authentication.

## What it does

`flickr-docker.sh` builds a container image with `flickr_download`, Chromium, Firefox ESR, and ExifTool, then runs it with X11 forwarding so the OAuth browser login works on the host display. Downloads are saved with JSON metadata and EXIF data intact.

`flickr_download` is installed from GitHub (not PyPI) to pick up an unreleased fix ([#166](https://github.com/beaufour/flickr-download/issues/166)) that gracefully skips photos when the requested size is unavailable instead of crashing the entire download.

- Builds an inline Dockerfile based on `python:3.14-slim` with all X11/browser dependencies
- Handles `xauth` cookie forwarding (supports both X11 and XWayland)
- Auto-detects Docker or Podman and adjusts runtime flags accordingly
- Interactive first-run setup prompts for Flickr API key and secret

## Prerequisites

- Docker or Podman (auto-detected)
- X11 display with `xauth` installed
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

Example output when a rate limit is hit:

```
[WARN] Rate limit hit (#1), suspending for 60s...
[INFO] Resuming download...
[WARN] Rate limit hit (#2), suspending for 120s...
[INFO] Resuming download...
```

This applies to `download` and `album` commands in both in-container and host modes. Interactive commands (`auth`, `shell`, `list`) are not wrapped.

## Integrated mode (somestuff container)

`flickr_download` and ExifTool are also installed in the main `xomoxcc/somestuff` Docker image. `flickr-docker.sh` auto-detects when it runs inside the container (via `FLICKR_HOME` or container marker files) and calls `flickr_download` directly — no nested container build/run needed.

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

## Podman notes

When Podman is detected the script automatically adds:

- `--userns=keep-id` for correct X11 access with the host UID
- `--security-opt label=disable` for SELinux compatibility
- Config is mounted to `/home/poduser` instead of `/root` and `HOME` is set accordingly
