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

## Integrated mode (somestuff container)

`flickr_download` and ExifTool are also installed in the main `xomoxcc/somestuff` Docker image. This lets you run downloads directly inside the somestuff container without a nested container build.

**Auth** still requires a browser, so run it on the host first:

```bash
cd flickrdownloaderstuff && ./flickr-docker.sh auth
```

**Download** inside the container:

`flickr_download` looks for credentials in `$HOME`. The container sets `FLICKR_HOME` pointing to the mounted config directory, so prefix the command with `HOME=$FLICKR_HOME`:

```bash
make dstart
# inside the container:
cd ~/flickr-backup
HOME=$FLICKR_HOME flickr_download -t --download_user https://www.flickr.com/photos/USERNAME/ \
  --save_json --cache ~/flickr-cache/api_cache --metadata_store
```

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
