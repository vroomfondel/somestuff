# uptimekumastuff

Export and re-import the **complete state** of an [Uptime Kuma](https://github.com/louislam/uptime-kuma)
instance (monitors, notifications, tags, status pages) over its Socket.IO API —
for migrations, clones and backups.

## What it does

`uptimekuma_simpleapi.py` provides `SimpleKumaApi`, a thin shell around the
`uptime-kuma-api` library, plus a Typer CLI with two commands:

| Command  | Purpose                                                                     |
|----------|-----------------------------------------------------------------------------|
| `export` | Pull monitors, notifications, tags and status pages into one JSON file      |
| `import` | Write such a JSON export into a (preferably empty) target instance          |

The import remaps every foreign ID (notification, tag, parent monitor) from the
source instance to the newly assigned IDs in the target, and creates monitors
parents-before-children so nested groups survive the move.

## Why not just `uptime-kuma-api`?

The library (1.2.1, last released 2023) **reads** fine against Kuma 2.x but
cannot **write**:

- `add_monitor()` → `NOT NULL constraint failed: monitor.conditions`. Kuma 2.x
  added `conditions NOT NULL DEFAULT '[]'`; the library doesn't know the field,
  so the server writes an explicit NULL — and explicit NULL beats the default.
- `delete_monitor()` → `monitor does not exist`, because it validates against a
  stale local cache.
- `get_status_page()` → `KeyError 'incident'`; that field is gone in 2.x.

So reads go through the library where they work, and writes go straight through
`_call()`. **Verify every write run with a FRESH session — the session caches lie.**

## Setup

Credentials come from `uptimekuma.local.env` (gitignored), real environment
variables (these win), or `--username` / `--password`:

```bash
cp uptimekumastuff/uptimekuma.env.example uptimekumastuff/uptimekuma.local.env
$EDITOR uptimekumastuff/uptimekuma.local.env
```

The Socket.IO login does **not** accept API keys — use a real user account.

## Usage

```bash
# pull the current state
python3 -m uptimekumastuff.uptimekuma_simpleapi export \
  --url https://uptimekuma.example.lan --out state.local.json

# see what an import would create — touches nothing, needs no credentials
python3 -m uptimekumastuff.uptimekuma_simpleapi import \
  --url http://127.0.0.1:3001 --in-file state.local.json --dry-run

# write it into the target instance, monitors inactive
python3 -m uptimekumastuff.uptimekuma_simpleapi import \
  --url http://127.0.0.1:3001 --in-file state.local.json --paused \
  --username admin --password '…'
```

`--paused` creates every monitor inactive: no checks, no alarms. Strongly
recommended for test instances — otherwise the clone starts probing production
targets and paging people the moment the import finishes.

### Library

```python
from uptimekumastuff.uptimekuma_simpleapi import SimpleKumaApi

api = SimpleKumaApi("https://uptimekuma.example.lan", "admin", "…")
try:
    state = api.export_state()
finally:
    api.close()

print(len(state["monitors"]), "monitors")
```

## Notes and pitfalls

- **The export contains plaintext secrets** — MQTT passwords, Telegram bot
  tokens, Gotify tokens, the SMTP password. Name the file `*.local.*`
  (gitignored) and keep it out of Git.
- **`applyExisting` is forced to False** on import. It is not state but a
  one-shot UI trigger ("attach to all existing monitors"); left True against a
  populated instance it would attach the notification to *every* monitor and
  overwrite the exact links. Old production DBs still carry True there.
- **`notificationIDList` must be a dict**, not a list. The server does
  `for (let id in notificationIDList)` and tests for truthy — a list `[1, 3]`
  would iterate its *indices* `0, 1` and link the wrong notifications.
- **11 monitor fields are dropped on import** (`DERIVED_MONITOR_FIELDS`): Kuma
  returns 115 fields but the `monitor` table has only 111 columns; the rest is
  derived server-side or lives in its own table. All other fields are passed
  through untouched, so unknown/new fields need no code change here.
- **Import targets an empty instance.** There is no merge or update — objects
  are always created new. Re-running against a populated instance duplicates.
- Requires: `typer`, `python-dotenv`, `uptime-kuma-api`, `loguru`, `tabulate`.
