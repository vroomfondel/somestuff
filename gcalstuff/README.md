# gcalstuff

CLI tool for creating Google Calendar events with day-view confirmation.

## What it does

`gcal_event.py` authenticates via OAuth2 Desktop flow, displays all existing events for the target day across all user calendars, and requires explicit confirmation before creating the new event.

- OAuth2 authentication with automatic token caching and refresh
- Fetches events from all calendars (skips holiday/week-number calendars)
- Shows a formatted day view so you can spot conflicts before confirming

## Setup

1. Create OAuth2 Desktop credentials in the [Google Cloud Console](https://console.cloud.google.com/apis/credentials) with the Calendar API enabled
2. Download `credentials.json` to `~/.config/gcal/credentials.json`
3. On first run the browser-based OAuth flow will create `~/.config/gcal/token.json`

## Usage

```bash
python gcal_event.py "Meeting" 2025-07-15 14:00
python gcal_event.py "Lunch" 2025-07-15 12:00 -d 90 --description "Team lunch"
```

| Option | Description |
|---|---|
| `title` | Event title (positional) |
| `date` | Event date as `YYYY-MM-DD` (positional) |
| `start_time` | Start time as `HH:MM` (positional) |
| `-d`, `--duration` | Duration in minutes (default: 60) |
| `--description` | Optional event description |
| `--calendar-id` | Target calendar ID (default: `primary`) |
| `--credentials` | Path to OAuth2 credentials.json |
| `--token` | Path to cached token.json |
| `--timezone` | IANA timezone (default: `Europe/Berlin`) |

## Docker usage

The initial OAuth flow opens a browser, so run it on the host first to create the token:

```bash
python -m gcalstuff.gcal_event "Test" 2025-01-01 12:00
```

After that, mount the `~/.config/gcal/` directory into the container (read‑write, since the token gets refreshed on each run):

```bash
docker run --rm -it \
  -v $(pwd)/config.local.yaml:/app/config.local.yaml:ro \
  -v ~/.config/gcal:/home/pythonuser/.config/gcal \
  xomoxcc/somestuff:latest \
  python -m gcalstuff.gcal_event "Meeting" 2025-07-15 14:00
```

The mount target uses `/home/pythonuser` because the container runs as that user (UID 1200). If the token write fails with a permission error, `chown 1200:1200 ~/.config/gcal` on the host.