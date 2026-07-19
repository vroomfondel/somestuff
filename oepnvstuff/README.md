# oepnvstuff

Validate whether an open **GTFS-Realtime** feed actually carries real-time data
(delays / actual times) for specific transit lines at a specific station — as a
one-shot check, or as a long-running watcher that publishes its findings to
**MQTT**.

Background: open-data GTFS-RT aggregators (like [gtfs.de](https://gtfs.de)) merge
whatever the transit agencies deliver. For many rural/suburban lines that is
*schedule only* — the feed exists, but your lines never get actual times. This
tool answers the question "is there real realtime for *my* lines at *my* stop?"
before you build anything on top of it. The defaults (station `Blankenese`,
lines `1/12/22/189`) are just defaults — every parameter is overridable via CLI
option or `OEPNV_*` environment variable.

## Modules

| Module              | Purpose                                                                                    |
|---------------------|--------------------------------------------------------------------------------------------|
| `check_realtime.py` | Typer CLI: one-shot check / `--watch` poll loop, console report, optional MQTT             |
| `monitor.py`        | `RealtimeMonitor`: poll loop + staleness detection, dispatches to pluggable `on_*` handlers |
| `gtfs_static.py`    | Static feed: cached download (conditional GET) + schedule indexing (`StaticFeedIndex`)     |
| `gtfs_realtime.py`  | RT feed: `RealtimeFetcher` (ETag/If-Modified-Since) + protobuf parsing (`RealtimeSnapshot`) |
| `mqtt_bridge.py`    | `GtfsMqttBridge`: publishes cycle/line status, alerts and stale transitions via `mqttstuff` |

Only `mqtt_bridge.py` imports `mqttstuff` — the core runs without a broker.

## How it works

1. The **static** GTFS feed (planned schedule, one big zip) is downloaded once
   and indexed: stops whose name contains the station query, the routes of the
   target lines, and the trips that belong to those lines **and** serve one of
   the matched stops. Next to the cached zip a `<name>.meta.json` sidecar stores
   the server's `ETag`/`Last-Modified`; every later run sends a conditional GET,
   so an unchanged feed costs a `304` round-trip instead of a re-download — no
   TTL needed.
2. The **realtime** feed (`.pb`, TripUpdates + ServiceAlerts) is fetched (or, in
   `--watch`, polled — GTFS-RT is *not* push/WebSocket). The fetcher keeps the
   HTTP validators between polls, so unchanged cycles are `304`s.
3. TripUpdates are matched against the target trips (`trip_id`) and target
   lines (`route_id`); delay values are aggregated per line.
4. **Staleness** is detected two ways (either marks the cycle stale): the feed's
   own `FeedHeader.timestamp` older than `--max-age` seconds, or `--stale-cycles`
   consecutive cycles without a timestamp change.

## Usage

```bash
# one-shot check, human-readable report, exit code says it all
python3 -m oepnvstuff.check_realtime

# explicit station / lines (overrides the defaults — e.g. only the Elbe-bank buses)
python3 -m oepnvstuff.check_realtime --station "Blankenese" --lines "286,488"

# which stops would match? (fast: reads only stops.txt + one stop_times pass;
# shows coordinates, serving lines incl. agency, and directions per stop)
python3 -m oepnvstuff.check_realtime --show-stops

# same-named stations in different towns? GTFS has no postal codes — filter
# geographically by 'lat,lon,radius_km' (applies to the check itself as well;
# 53.5633,9.8144 is S Blankenese)
python3 -m oepnvstuff.check_realtime --show-stops --station Blankenese --near 53.5633,9.8144,2

# poll loop, one compact status line per cycle
python3 -m oepnvstuff.check_realtime --watch --interval 20

# several stations at once (';'-separated — stop names contain commas/spaces)
python3 -m oepnvstuff.check_realtime --station "Blankenese;Ellerbek" --lines "1,189,195"

# departure board: next N departures per line+direction, with realtime delay
# ("1 Richtung S Rissen: in 2 min (+80s)"); publishes to <base>/departures with --mqtt
python3 -m oepnvstuff.check_realtime --watch --departures --departures-count 2

# poll loop that exits (code 2) once the feed goes stale — lets k8s restart it
python3 -m oepnvstuff.check_realtime --watch --stop-on-stale

# publish results to MQTT while watching
python3 -m oepnvstuff.check_realtime --watch --mqtt --mqtt-host broker.example.org
```

Both feed options also accept **local file paths** (handy for tests):
`--static feed.zip --realtime snapshot.pb`.

**Exit codes:** `0` realtime found for at least one line (or watch ended
cleanly) · `2` schedule found but no realtime (or stale abort) · `3` no matching
stops/trips in the static feed · `1` technical error.

## Configuration

Precedence: **CLI option > real environment variable > `./oepnv.local.env`
(CWD) > `oepnvstuff/oepnv.local.env` (package dir)** — both env files are
auto-loaded via `python-dotenv` and gitignored via `*.local.*`.

```bash
cp oepnvstuff/oepnv.env.example oepnvstuff/oepnv.local.env
$EDITOR oepnvstuff/oepnv.local.env
```

| Env var                | CLI                | Default                                     |
|------------------------|--------------------|---------------------------------------------|
| `OEPNV_STATIC_URL`     | `--static`         | `https://download.gtfs.de/germany/nv_free/latest.zip` |
| `OEPNV_REALTIME_URL`   | `--realtime`       | `https://realtime.gtfs.de/realtime-free.pb` |
| `OEPNV_LINES`          | `--lines`, `-l`    | `1,12,22,189` (comma/space separated)       |
| `OEPNV_STATION`        | `--station`, `-s`  | `Blankenese` (substring match, case-insensitive; several stations `;`-separated) |
| `OEPNV_NEAR`           | `--near`           | *(off)* — `lat,lon,radius_km` geo filter for the matched stops |
| `OEPNV_DEPARTURES`     | `--departures`     | off — report upcoming departures per line+direction |
| `OEPNV_DEPARTURES_COUNT` | `--departures-count` | `3` — next N departures per line+direction |
| `OEPNV_DEPARTURES_HORIZON` | `--departures-horizon` | `48` h look-ahead (a fresh static feed appears within 48h anyway) |
| `OEPNV_CACHE_DIR`      | `--cache-dir`      | `.gtfs_cache`                               |
| `OEPNV_FORCE_REFRESH`  | `--force-refresh`  | off                                         |
| `OEPNV_WATCH`          | `--watch`, `-w`    | off                                         |
| `OEPNV_INTERVAL`       | `--interval`       | `20` s (gtfs.de updates every ~10 s)        |
| `OEPNV_MAX_AGE`        | `--max-age`        | `120` s (`0` = off)                         |
| `OEPNV_STALE_CYCLES`   | `--stale-cycles`   | `6`                                         |
| `OEPNV_STOP_ON_STALE`  | `--stop-on-stale`  | off                                         |
| `OEPNV_MQTT_ENABLE`    | `--mqtt/--no-mqtt` | off                                         |
| `OEPNV_MQTT_HOST`      | `--mqtt-host`      | `mosquitto.mosquitto.svc.cluster.local`     |
| `OEPNV_MQTT_PORT`      | `--mqtt-port`      | `1883`                                      |
| `OEPNV_MQTT_USER`      | `--mqtt-user`      | *(anonymous)*                               |
| `OEPNV_MQTT_PASSWORD`  | `--mqtt-password`  | *(empty)*                                   |
| `OEPNV_MQTT_BASE_TOPIC`| `--mqtt-base-topic`| `oepnv`                                     |
| `OEPNV_MQTT_TLS`       | `--mqtt-tls`       | off — TLS brokers usually listen on `8883`: set the port explicitly |
| `OEPNV_MQTT_TLS_CA`    | `--mqtt-tls-ca`    | *(system CA store)* — CA certificate path   |
| `OEPNV_MQTT_TLS_CERT`  | `--mqtt-tls-cert`  | *(off)* — client certificate for mutual TLS (with `…_KEY`) |
| `OEPNV_MQTT_TLS_KEY`   | `--mqtt-tls-key`   | *(off)* — client key for mutual TLS         |
| `OEPNV_MQTT_TLS_INSECURE` | `--mqtt-tls-insecure` | off — skip hostname verification (self-signed certs) |
| `OEPNV_VERBOSE`        | `--verbose`, `-v`  | off                                         |

**k3s notes:** configure everything via env in the Deployment; point
`OEPNV_CACHE_DIR` at a persistent volume so pod restarts reuse the (large)
static zip via `304`; combine `OEPNV_WATCH=1` + `OEPNV_STOP_ON_STALE=1` so a
dead feed exits with code 2 and the kubelet restarts the pod. Ready-made
examples: `somestuff_oepnv_deployment.yml` (watch + MQTT + cache volume) and
`somestuff-oepnv-secret.example.yaml` (broker credentials; copy to
`*.local.yaml`, gitignored).

## MQTT topic layout

`base_topic` defaults to `oepnv`:

| Topic                        | Retain | Payload                                                          |
|------------------------------|--------|------------------------------------------------------------------|
| `oepnv/status`               | yes    | per-cycle summary: feed ts/age, stale flag, per-line hit counts  |
| `oepnv/lines/<line>/status`  | yes    | one line: `updates`, `delay_min/max/avg_s`, `static_trips`       |
| `oepnv/departures`           | yes    | with `--departures`: all upcoming departures in one document (`in_minutes`, `delay_s`, `realtime`, stop, times) |
| `oepnv/departures/<line>/<stop>/<direction>` | yes | one group's departures; ASCII-simplified segments (`departures/1/S_Blankenese/U_Kellinghusenstrasse`); vanished groups get their retained message cleared |
| `oepnv/alerts`               | no     | each *new* (deduplicated) service alert: `{entity, text}`        |
| `oepnv/stale`                | no     | fired once on the transition into staleness                      |

Status topics are retained (genuine last-known-value state — a restarting
dashboard immediately sees the current situation); alerts and stale transitions
are point-in-time events and therefore transient.

```bash
mosquitto_sub -h broker.example.org -v -t 'oepnv/#'
```

## Library / writing your own handler

The monitor knows nothing about consoles or brokers — it dispatches typed
results to registered handlers. Console output and the MQTT bridge are just
handlers; add your own the same way:

```python
from oepnvstuff.gtfs_realtime import RealtimeFetcher
from oepnvstuff.gtfs_static import build_index, obtain
from oepnvstuff.monitor import CycleResult, RealtimeMonitor

index = build_index(obtain("https://download.gtfs.de/germany/nv_free/latest.zip", ".gtfs_cache"),
                    ["1", "189"], "Blankenese")
monitor = RealtimeMonitor(RealtimeFetcher("https://realtime.gtfs.de/realtime-free.pb"), index,
                          interval=20.0, stop_on_stale=True)

def my_handler(cycle: CycleResult) -> None:
    for line, status in cycle.per_line.items():
        print(line, status.updates, status.avg_delay)

monitor.add_cycle_handler(my_handler)          # every completed cycle
monitor.add_alert_handler(print)               # each new service alert (deduplicated)
monitor.add_stale_handler(print)               # edge-triggered on going stale
monitor.add_error_handler(print)               # fetch errors (loop keeps running)

raise SystemExit(monitor.watch())              # or: monitor.check_once()
```

Handler exceptions are logged and swallowed — one misbehaving sink cannot kill
the poll loop.

## Data sources

Defaults are the free, registration-less feeds from
[gtfs.de](https://gtfs.de/de/open-data/) (CC BY-SA 4.0): static
`germany/nv_free/latest.zip` (German regional transit) and
`realtime-free.pb` (TripUpdates + ServiceAlerts). Any other GTFS/GTFS-RT source
pair works via `--static`/`--realtime` — e.g. an agency's own feed if the open
aggregate turns out to lack your lines (for HVV territory: Geofox-GTI).

Requires: `requests`, `gtfs-realtime-bindings` (protobuf), `typer`,
`python-dotenv`, plus `loguru` and `tabulate` for logging and `mqttstuff` only
if the MQTT bridge is used.
