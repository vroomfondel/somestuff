# uptimekumastuff

Provision, migrate and back up [Uptime Kuma](https://github.com/louislam/uptime-kuma) 2.x
declaratively and idempotently instead of clicking monitors together in the web UI: a direct
Socket.IO client with fresh reads and idempotent upserts, a YAML-driven apply CLI and a full
export/import tool. None of these tools monitor anything; they only create configuration, the
monitoring itself is done entirely by Kuma.

## Modules

- **`uptimekuma_client.py`** — `KumaClient`, a direct Socket.IO client with guaranteed-fresh
  reads and idempotent `upsert_monitor` / `upsert_notification`. The library everything else
  builds on.
- **`uptimekuma_apply.py`** — applies a partial desired state (monitors and notifications) from a
  YAML file against an existing instance; re-runnable, with `--check` (dry run) and `--prune`.
- **`uptimekuma_simpleapi.py`** — `SimpleKumaApi`, full export/import of a whole instance
  (monitors, notifications, tags, status pages) into a preferably **empty** target.
- **`uptimekuma_monitor.py`** — an Ansible module wrapping `KumaClient`. It is available in the
  repository only, not in the installed package (see Ansible below).

Logging (loguru, with an intercept for stdlib `logging` from `uptime_kuma_api` and
`python-socketio`), the startup banner and the credentials lookup are shared via
`somestuff.uptimekumastuff`.

## Installation

```bash
pip install somestuff-uptimekumastuff
```

`somestuff` is a PEP 420 namespace; the package is imported as
`somestuff.uptimekumastuff` and shares the namespace with the other `somestuff-*`
distributions.

## Why not just `uptime-kuma-api`?

The library (1.2.1, last released 2023, declares support up to Kuma 1.23.2) **reads** fine
against Kuma 2.x but **cannot write** — and its reads lie after your own writes. All verified
against the 2.4.0 server code:

| Call                                                                     | Behaviour against 2.4.0                               |
|--------------------------------------------------------------------------|-------------------------------------------------------|
| `login`, `get_monitors`, `get_notifications`, `get_settings`, `get_tags` | work                                                  |
| `add_monitor()`                                                          | **`NOT NULL constraint failed: monitor.conditions`**  |
| `delete_monitor()`                                                       | **`"monitor does not exist"`** — although it exists   |
| `get_status_page()`                                                      | **`KeyError: 'incident'`** — the field is gone in 2.x |

**`add_monitor`:** Kuma 2.x added the column `conditions NOT NULL DEFAULT '[]'` (migration
`2024-08-24-0000-conditions.js`). The library doesn't know the field, so the server writes an
explicit **NULL** — and explicit NULL beats the column default.

**`delete_monitor`:** the library reads a **cache**, not the server:

```python
# uptime_kuma_api/api.py, get_monitors()
# TODO: replace with getMonitorList?
r = list(self._get_event_data(Event.MONITOR_LIST).values())
```

The snapshot comes from the login push and is only passively updated. `getMonitorList` — the
server event that forces a fresh push — is **never** called; the `TODO` is the author's own
admission. For idempotency and check-mode that's disqualifying: the state you read back **must**
be real. Hence `KumaClient`, which calls `getMonitorList` and waits for the resulting push.

> `python-socketio` is **not a new dependency** — `uptime-kuma-api` builds on it itself. Going
> direct *removes* the unmaintained layer.

**There is no write REST API in 2.x** ([issue #7151](https://github.com/louislam/uptime-kuma/issues/7151)
is closed). REST is read-only: badges, status pages, `/metrics`, push URLs. Everything that
writes goes over Socket.IO.

`uptimekuma_simpleapi.py` deliberately **still** uses the library for reads (where it works) and
`_call()` for writes. It is verified correct and only ever writes into an empty instance without
reading in between, so the cache problem never arises there.

## Credentials

Both CLIs read the login from `--username` / `--password` (`uptimekuma-simpleapi` only), real
environment variables, or a `uptimekuma.local.env` file (dotenv syntax):

```
UPTIME_KUMA_USERNAME=admin
UPTIME_KUMA_PASSWORD=...
UPTIME_KUMA_URL=https://uptimekuma.example.lan
```

Precedence: command-line options, then real environment variables, then `uptimekuma.local.env`.
The file is looked for **first in the current working directory**, then in the installed package
directory — the first hit wins, and variables never overwrite ones already set. A per-deployment
file can therefore sit next to the state YAML it belongs to. Keep it out of version control.

`UPTIME_KUMA_URL` is the fallback target, so `--url` can be omitted. Precedence for the URL is
`--url` → `url:` in the state YAML → `UPTIME_KUMA_URL`, so an ambient default never silently
redirects a run at a different instance than the file names.

> **The Socket.IO login accepts username+password only.** API keys (`uk1_`/`uk2_`/`uk3_`) work
> exclusively for HTTP basic auth on `/metrics` — never for the socket. The login is also
> rate-limited to 20 attempts per minute (`server/rate-limiter.js`), which surfaces as
> `KumaError: login: Too frequently, try again later.` — the limiter runs with
> `fireImmediately: true` and answers with `ok: false` rather than leaving you hanging.

A **timeout** (`login: no response within 30s`) is something else: no ack at all. The handlers in
`server.js` have no try/catch, so an exception inside one (DB hiccup) swallows the callback; a
dropped connection looks identical from the client side. Both are transient — retry.

## Usage

Console scripts and module invocation are equivalent:

```bash
uptimekuma-apply -f kuma_state.local.yml --check
python3 -m somestuff.uptimekumastuff.uptimekuma_apply -f kuma_state.local.yml --check

uptimekuma-simpleapi export --url https://uptimekuma.example.lan --out state.local.json
python3 -m somestuff.uptimekumastuff.uptimekuma_simpleapi export --out state.local.json
```

### uptimekuma-apply — YAML in, idempotent apply

Applies a desired state against an existing instance — re-runnable, with topological ordering
(nested groups) and full state awareness (so no check-mode ordering problem). Options: `-f/--file`
(required), `--url`, `--check`, `--prune`, `-v/--verbose`.

```bash
# dry run — show what would change, write nothing
uptimekuma-apply -f kuma_state.local.yml --check

# apply
uptimekuma-apply -f kuma_state.local.yml

# apply and delete monitors the YAML doesn't declare
uptimekuma-apply -f kuma_state.local.yml --prune
```

```yaml
url: https://uptimekuma.example.lan

notifications:
  - name: Mein Gotify Alarm (1)
    type: gotify
    gotifyserverurl: https://gotify.example.org
    gotifyapplicationToken: "..."
    gotifyPriority: 8

monitors:
  - name: outdoormesh
    type: group
  - name: "outdoormesh :: pi1"
    type: ping
    parent: outdoormesh                # name, not ID
    notifications: [Mein Gotify Alarm (1)]  # names, not IDs
    hostname: 192.0.2.51
    active: true
```

References go by **name**, not ID — IDs differ per instance and wouldn't be declarative. Field
names are Kuma's (camelCase: `mqttTopic`, `retryInterval`), so an export from
`uptimekuma-simpleapi` drops in without translation, apart from the ID references described below.

Output masks secrets (`mqttPassword=<secret>`). The YAML itself holds plaintext secrets —
**don't commit it**; name it `*.local.*` or keep it out of version control.

### uptimekuma-simpleapi — backup and migration

Subcommands `export` and `import`; both take `--url`, `--username`, `--password`, `-v/--verbose`.

```bash
# pull the current state
uptimekuma-simpleapi export --url https://uptimekuma.example.lan --out state.local.json

# ... as YAML, for readable diffs between two exports
uptimekuma-simpleapi export --url https://uptimekuma.example.lan --out state.local.yml

# see what an import would create — touches nothing, needs no credentials
uptimekuma-simpleapi import --url http://127.0.0.1:3001 --in-file state.local.json --dry-run

# write it into the target instance, monitors inactive
uptimekuma-simpleapi import --url http://127.0.0.1:3001 --in-file state.local.json --paused \
  --username admin --password '…'
```

The export format follows the extension of `--out` (`.yml`/`.yaml` → YAML, otherwise JSON);
`--format json|yaml` forces it. `import` swallows both — JSON is parsed as JSON, everything else
as YAML. That order matters: PyYAML implements YAML 1.1 and resolves a few JSON scalars
differently (`1e5` becomes a string, not a number), so a JSON file belongs in the JSON parser.

> The YAML here is **not** an `uptimekuma-apply` state file. Both use Kuma's field names, but
> the export references by ID (`parent: 9`, `notificationIDList: [1, 3]`) while apply references
> by name — and `resolve_parent` passes an `int` through unchanged, so the IDs would be taken as
> already-resolved and silently point at whatever carries that ID on the target.

Verified: prod → fresh instance → re-export → field-compare = **identical**. `--paused` creates
every monitor inactive: no checks, no alarms — **mandatory for test instances**, otherwise the
clone starts probing production targets and paging people the moment the import finishes.

The import remaps every foreign ID (notification, tag, parent monitor) from the source to the
newly assigned target IDs, and creates monitors parents-before-children so nested groups survive.
It targets an **empty** instance — there is no merge or update, objects are always created new,
so re-running against a populated instance duplicates. (For idempotent updates use
`uptimekuma-apply`.)

## Library

```python
from somestuff.uptimekumastuff.uptimekuma_client import KumaClient

with KumaClient("https://uptimekuma.example.lan") as c:
    c.login(user, pw)
    mons = c.monitors()                       # fresh from the server, no cache
    r = c.upsert_monitor({"name": "x", "type": "push"})
    print(r["changed"], r["object_id"], r["diff"])
```

`upsert_monitor` / `upsert_notification` return:

```python
{"changed": bool, "object_id": int | None, "created": bool,
 "diff": {"field": {"before": ..., "after": ...}},
 "not_applicable": {...}}   # optional
```

Full-instance export/import via `SimpleKumaApi`:

```python
from somestuff.uptimekumastuff.uptimekuma_simpleapi import SimpleKumaApi

api = SimpleKumaApi("https://uptimekuma.example.lan", "admin", "…")
try:
    state = api.export_state()
finally:
    api.close()

print(len(state["monitors"]), "monitors")
```

## Ansible module

`uptimekuma_monitor.py` is an Ansible module wrapping `KumaClient` for idempotent, per-monitor
management from a playbook — `state: present`/`absent`, full `--check` support, and
`changed`/`diff`/`not_applicable` reporting. Use it when Kuma provisioning is one step in a larger
Ansible run; for a standalone declarative file, `uptimekuma-apply` is the better fit.

The module is **not part of the installed package**. Get it from the repository:
https://github.com/vroomfondel/somestuff/tree/main/somestuff/uptimekumastuff/

**Wiring it into Ansible.** The module imports its client via
`from ansible.module_utils.uptimekuma_client import KumaClient, KumaError`, so two files have to
be reachable from your Ansible config's `library` and `module_utils` paths:

| Ansible path                        | source                                                               |
|-------------------------------------|----------------------------------------------------------------------|
| `library/uptimekuma_monitor.py`     | `uptimekuma_monitor.py` from the repository                          |
| `module_utils/uptimekuma_client.py` | `uptimekuma_client.py`, from the installed package or the repository |

Symlink or copy the files into those directories. The installed `uptimekuma_client.py` lives in
the `somestuff/uptimekumastuff/` directory of your Python environment's site-packages. Keep the
client and the module from the same release.

**Runtime requirements** — all consequences of talking to Kuma over Socket.IO:

- It runs on the **control node**, so `python-socketio` is needed *there*, not on the targets.
  Use `hosts: localhost` or `delegate_to: localhost`.
- `python-socketio` must be importable by the interpreter Ansible uses. If it only lives in a
  virtualenv, point `ansible_python_interpreter` at that venv's `python`
  (`-e ansible_python_interpreter=/path/to/.venv/bin/python`, or set it for localhost in the
  inventory) — otherwise the module import fails.

```yaml
- name: Provision Uptime-Kuma monitors
  hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - name: Create group
      uptimekuma_monitor:
        url: https://uptimekuma.example.lan
        username: "{{ uptimekuma_admin_user }}"
        password: "{{ uptimekuma_admin_password }}"
        name: outdoormesh
        type: group

    - name: MQTT check in the group
      uptimekuma_monitor:
        url: https://uptimekuma.example.lan
        username: "{{ uptimekuma_admin_user }}"
        password: "{{ uptimekuma_admin_password }}"
        name: husqvarna/automower/pongs
        type: mqtt
        parent: outdoormesh                     # name, not ID
        notifications: [My Gotify Alarm (1)]     # names, not IDs
        hostname: mosquitto.mosquitto.svc.cluster.local
        port: 1883
        mqtt_topic: husqvarna/automower/pongs
        active: true
```

Like `uptimekuma-apply`, references go by **name** (`parent`, `notifications`). Options are
snake_case (`retry_interval`, `mqtt_topic`); the ~100 rarer Kuma fields aren't mapped
individually — pass them camelCase under `extra:`. `state: absent` deletes by name. Full option
docs: `ansible-doc -M /path/to/library uptimekuma_monitor`.

> **check_mode caveat:** against an empty instance, a task whose `parent`/`notifications` only a
> *prior task of the same run* would create fails under `--check` — the reference doesn't exist
> yet. `uptimekuma-apply` avoids this because it knows the whole desired state up front.

## Pitfalls (all verified against the server code)

- **`weight` is set only on creation.** `editMonitor` doesn't read it — a later change would be a
  silent no-op. `KumaClient` reports it as `not_applicable` and leaves `changed` false, so a run
  doesn't keep reporting a change that never lands.
- **`active` does not go through `editMonitor`,** but through `pauseMonitor`/`resumeMonitor`. The
  client handles this internally. `add` with `active: false` also prevents the server from calling
  `startMonitor()`.
- **Monitor names are not unique in Kuma.** Since `name` is the idempotency key here, `KumaClient`
  aborts on ambiguity instead of silently hitting the wrong monitor.
- **`notificationIDList` is asymmetric:** the server *returns* `{"1": true}` (dict, string keys)
  but *expects* `{1: True}` on write, and the library turns reads into `[1]`. The client
  normalises to `[1]` and converts back on write. Working with it raw: sending a list `[1, 3]`
  links the **indices** 0 and 1, not the IDs — the server iterates with `for (let id in ...)`.
- **`applyExisting` is not state,** it's a one-shot UI trigger. Left `true` against a populated
  instance, Kuma attaches the notification to **every** monitor and destroys the exact links. All
  tools here force it to `false`. Old production DBs still carry `true`.
- **11 monitor fields are dropped on import** (`DERIVED_MONITOR_FIELDS`): Kuma returns 115 fields
  but the `monitor` table has only 111 columns; the rest is derived server-side or lives in its
  own table. All other fields pass through untouched, so unknown/new fields need no code change.
- **`add` requires `conditions`** (NOT NULL); `uptimekuma-apply` supplies it via
  `CREATE_DEFAULTS`.

## Testing with a throwaway instance

```bash
podman run -d --name kuma-test -p 3001:3001 \
  -e UPTIME_KUMA_DB_TYPE=sqlite docker.io/louislam/uptime-kuma:2
```

`UPTIME_KUMA_DB_TYPE=sqlite` is **mandatory**: without it 2.x boots into an interactive DB-setup
screen and never starts Socket.IO ("Waiting for user action…").

Then create the first user:

```python
from somestuff.uptimekumastuff.uptimekuma_client import KumaClient

with KumaClient("http://127.0.0.1:3001") as c:
    if c.need_setup():
        c.setup("admin", "testpass123!!")
```

**Readiness:** the port is open *before* the socket handlers are registered (migrations still
running). Don't poll the port — poll for a `needSetup` response.

Clean up with `podman rm -f kuma-test` — after an import the instance holds real secrets.

## Notes

- Requires: `loguru`, `typer`, `python-socketio[client]`, `uptime-kuma-api` (only for the reads in
  `SimpleKumaApi`), `PyYAML`, `python-dotenv`, `tabulate`. Targets Uptime Kuma 2.x.
- Licensed under the LGPL, see `LICENSE.md` in the repository root.
