# uptimekumastuff

Provision, migrate and back up **[Uptime Kuma](https://github.com/louislam/uptime-kuma) 2.x**
declaratively and idempotently — instead of clicking monitors together in the web UI.

> **None of these tools *monitor* anything.** They only create configuration; the monitoring
> itself is done entirely by Kuma.

## Which tool for what?

| I want to…                                              | Tool                                          |
|---------------------------------------------------------|-----------------------------------------------|
| Apply monitors **and** notifications from a YAML file   | **`uptimekuma_apply.py`** — `SimpleKumaApi`'s idempotent sibling |
| Manage monitors from an **Ansible** playbook            | **`uptimekuma_monitor.py`** (Ansible module)  |
| Back up / migrate the complete state to a new instance  | **`uptimekuma_simpleapi.py`** (`SimpleKumaApi`) |
| Write my own Python against Kuma                        | **`uptimekuma_client.py`** (`KumaClient`)     |

- **`uptimekuma_client.py`** — `KumaClient`, a direct Socket.IO client with guaranteed-fresh
  reads and idempotent `upsert_monitor` / `upsert_notification`. The library everything else
  builds on.
- **`uptimekuma_apply.py`** — applies a partial desired state from a YAML file against an
  existing instance, re-runnable, with `--check` (dry run) and `--prune`.
- **`uptimekuma_monitor.py`** — an Ansible module wrapping `KumaClient` for idempotent,
  per-monitor management from a playbook (`state: present`/`absent`, `--check` support).
- **`uptimekuma_simpleapi.py`** — `SimpleKumaApi`, full export/import of a whole instance
  (monitors, notifications, tags, status pages) into a preferably **empty** target.

## Why not just `uptime-kuma-api`?

The library (1.2.1, last released 2023, declares support up to Kuma 1.23.2) **reads** fine
against Kuma 2.x but **cannot write** — and its reads lie after your own writes. All verified
against the 2.4.0 server code:

| Call                                                                     | Behaviour against 2.4.0                                          |
|--------------------------------------------------------------------------|-----------------------------------------------------------------|
| `login`, `get_monitors`, `get_notifications`, `get_settings`, `get_tags` | work                                                            |
| `add_monitor()`                                                          | **`NOT NULL constraint failed: monitor.conditions`**            |
| `delete_monitor()`                                                       | **`"monitor does not exist"`** — although it exists             |
| `get_status_page()`                                                      | **`KeyError: 'incident'`** — the field is gone in 2.x           |

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

## Setup

Credentials come from `uptimekuma.local.env` (gitignored via `*.local.*`), real environment
variables (these win), or `--username` / `--password`:

```bash
cp uptimekumastuff/uptimekuma.env.example uptimekumastuff/uptimekuma.local.env
$EDITOR uptimekumastuff/uptimekuma.local.env
```

> **The Socket.IO login accepts username+password only.** API keys (`uk1_`/`uk2_`/`uk3_`) work
> exclusively for HTTP basic auth on `/metrics` — never for the socket. The login is also
> rate-limited: several logins in quick succession → `TimeoutError`.

Requires: `python-socketio`, `python-dotenv`, `typer`, `PyYAML`, `uptime-kuma-api` (the last only
for the reads in `uptimekuma_simpleapi.py`), plus `loguru` and `tabulate` for logging.

## `uptimekuma_apply.py` — YAML in, idempotent apply

Applies a desired state against an existing instance — re-runnable, with topological ordering
(nested groups) and full state awareness (so no check-mode ordering problem):

```bash
# dry run — show what would change, write nothing
python3 -m uptimekumastuff.uptimekuma_apply -f kuma_state.local.yml --check

# apply
python3 -m uptimekumastuff.uptimekuma_apply -f kuma_state.local.yml

# apply and delete monitors the YAML doesn't declare
python3 -m uptimekumastuff.uptimekuma_apply -f kuma_state.local.yml --prune
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
    hostname: 192.168.101.51
    active: true
```

References go by **name**, not ID — IDs differ per instance and wouldn't be declarative. Field
names are Kuma's (camelCase: `mqttTopic`, `retryInterval`), so an export from
`uptimekuma_simpleapi.py` drops in without translation.

Output masks secrets (`mqttPassword=<secret>`). The YAML itself holds plaintext secrets —
**don't commit it**; name it `*.local.*` (gitignored).

## `uptimekuma_simpleapi.py` — backup & migration

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

Verified: prod → fresh instance → re-export → field-compare = **identical**. `--paused` creates
every monitor inactive: no checks, no alarms — **mandatory for test instances**, otherwise the
clone starts probing production targets and paging people the moment the import finishes.

The import remaps every foreign ID (notification, tag, parent monitor) from the source to the
newly assigned target IDs, and creates monitors parents-before-children so nested groups survive.
It targets an **empty** instance — there is no merge or update, objects are always created new,
so re-running against a populated instance duplicates. (For idempotent updates use
`uptimekuma_apply.py`.)

## `uptimekuma_monitor.py` — Ansible module

An Ansible module that wraps `KumaClient` for idempotent, per-monitor management from a
playbook — `state: present`/`absent`, full `--check` support, and `changed`/`diff`/
`not_applicable` reporting. Use it when Kuma provisioning is one step in a larger Ansible run;
for a standalone declarative file, `uptimekuma_apply.py` is the better fit.

**Wiring it into Ansible.** The module imports its client via
`from ansible.module_utils.uptimekuma_client import KumaClient, KumaError`, so two files have to
be reachable from your Ansible config's `library` and `module_utils` paths:

| Ansible path                       | points at                                |
|------------------------------------|------------------------------------------|
| `library/uptimekuma_monitor.py`    | `uptimekumastuff/uptimekuma_monitor.py`  |
| `module_utils/uptimekuma_client.py`| `uptimekumastuff/uptimekuma_client.py`   |

**Symlink** rather than copy, so this package stays the single source of truth:

```bash
ln -s /path/to/uptimekumastuff/uptimekuma_monitor.py library/uptimekuma_monitor.py
ln -s /path/to/uptimekumastuff/uptimekuma_client.py  module_utils/uptimekuma_client.py
```

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

Like `uptimekuma_apply.py`, references go by **name** (`parent`, `notifications`). Options are
snake_case (`retry_interval`, `mqtt_topic`); the ~100 rarer Kuma fields aren't mapped
individually — pass them camelCase under `extra:`. `state: absent` deletes by name. Full option
docs: `ansible-doc -M /path/to/library uptimekuma_monitor`.

> **check_mode caveat:** against an empty instance, a task whose `parent`/`notifications` only a
> *prior task of the same run* would create fails under `--check` — the reference doesn't exist
> yet. `uptimekuma_apply.py` avoids this because it knows the whole desired state up front.

## Library

```python
from uptimekumastuff.uptimekuma_client import KumaClient

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
from uptimekumastuff.uptimekuma_simpleapi import SimpleKumaApi

api = SimpleKumaApi("https://uptimekuma.example.lan", "admin", "…")
try:
    state = api.export_state()
finally:
    api.close()

print(len(state["monitors"]), "monitors")
```

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
- **`add` requires `conditions`** (NOT NULL); `uptimekuma_apply.py` supplies it via
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
from uptimekumastuff.uptimekuma_client import KumaClient

with KumaClient("http://127.0.0.1:3001") as c:
    if c.need_setup():
        c.setup("admin", "testpass123!!")
```

**Readiness:** the port is open *before* the socket handlers are registered (migrations still
running). Don't poll the port — poll for a `needSetup` response.

Clean up with `podman rm -f kuma-test` — after an import the instance holds real secrets.
