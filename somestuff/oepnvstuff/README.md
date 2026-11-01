# oepnvstuff

Validate whether an open GTFS-Realtime feed actually carries real-time data
(delays / actual times) for specific transit lines at a specific station, either
as a one-shot check or as a long-running watcher that publishes its findings to
MQTT.

Open-data GTFS-RT aggregators (such as gtfs.de) merge whatever the transit
agencies deliver. For many rural and suburban lines that is schedule only: the
feed exists, but your lines never get actual times. This tool answers "is there
real realtime for my lines at my stop?" before you build anything on top of it.
The defaults (station `Blankenese`, lines `1/12/22/189`) are just defaults;
every parameter is overridable via CLI option or `OEPNV_*` environment variable.

## Modules

- **`check_realtime.py`** — Typer CLI: one-shot check or `--watch` poll loop,
  console report, optional MQTT publishing.
- **`monitor.py`** — `RealtimeMonitor`: poll loop with staleness detection that
  dispatches typed results to pluggable `on_*` handlers (cycle, alert, stale,
  error). Console output and the MQTT bridge are just handlers.
- **`gtfs_static.py`** — static feed: cached download with conditional GET plus
  schedule indexing (`StaticFeedIndex`): matched stops, routes of the target
  lines, and the trips that belong to those lines and serve a matched stop.
- **`gtfs_realtime.py`** — realtime feed: `RealtimeFetcher` (ETag /
  If-Modified-Since) and protobuf parsing into a `RealtimeSnapshot`
  (TripUpdates + ServiceAlerts).
- **`mqtt_bridge.py`** — `GtfsMqttBridge`: publishes cycle and line status,
  departures, new alerts and stale transitions via `mqttstuff`.

Only `mqtt_bridge.py` imports `mqttstuff`; the core runs without a broker.
Logging (loguru, with an intercept for stdlib `logging`, e.g. `requests` and
`paho-mqtt`) and the startup banner are shared via `somestuff.oepnvstuff`.

### How it works

1. The static GTFS feed (planned schedule, one big zip) is downloaded once and
   indexed: stops whose name contains the station query, the routes of the target
   lines, and the trips of those lines that serve one of the matched stops. Next
   to the cached zip a `<name>.meta.json` sidecar stores the server's
   `ETag`/`Last-Modified`; every later run sends a conditional GET, so an
   unchanged feed costs a `304` round-trip instead of a re-download.
2. The realtime feed (`.pb`, TripUpdates + ServiceAlerts) is fetched, or in
   `--watch` mode polled (GTFS-RT is not push/WebSocket). The fetcher keeps the
   HTTP validators between polls, so unchanged cycles are `304`s.
3. TripUpdates are matched against the target trips (`trip_id`) and target lines
   (`route_id`); delay values are aggregated per line.
4. Staleness is detected two ways, and either marks the cycle stale: the feed's
   own `FeedHeader.timestamp` is older than `--max-age` seconds, or
   `--stale-cycles` consecutive cycles passed without a timestamp change.

## Installation

```bash
pip install somestuff-oepnvstuff
```

`somestuff` is a PEP 420 namespace; the package is imported as
`somestuff.oepnvstuff` and shares the namespace with the other `somestuff-*`
distributions.

## Usage

Console script and module invocation are equivalent:

```bash
oepnv-check-realtime
python3 -m somestuff.oepnvstuff.check_realtime
```

One-shot check (human-readable report, the exit code summarises the result):

```bash
oepnv-check-realtime
oepnv-check-realtime --station "Blankenese" --lines "286,488"
```

Both feed options also accept local file paths (handy for tests):
`--static feed.zip --realtime snapshot.pb`.

Exit codes: `0` realtime found for at least one line (or watch ended cleanly),
`2` schedule found but no realtime (or stale abort), `3` no matching stops or
trips in the static feed, `1` technical error.

### Stations, lines and `--near`

```bash
# which stops would match? Reads only stops.txt plus one stop_times pass and
# shows coordinates, serving lines (incl. agency) and directions per stop
oepnv-check-realtime --show-stops

# several stations at once, ';'-separated (stop names contain commas and spaces)
oepnv-check-realtime --station "Blankenese;Ellerbek" --lines "1,189,195"

# same-named stations in different towns? GTFS has no postal codes, so filter
# geographically by 'lat,lon,radius_km'; applies to the check itself as well.
# Several areas are ';'-separated and OR-combined.
oepnv-check-realtime --show-stops --station Blankenese --near 53.5633,9.8144,2
oepnv-check-realtime --near "53.5633,9.8144,2;53.647,9.892,3"
```

### Watch mode

```bash
# poll loop, one compact status line per cycle
oepnv-check-realtime --watch --interval 180

# exit with code 2 once the feed goes stale (lets a supervisor restart it)
oepnv-check-realtime --watch --stop-on-stale
```

### Departures

```bash
# next N departures per line+direction, with realtime delay
# ("1 Richtung S Rissen: in 2 min (+80s)")
oepnv-check-realtime --watch --departures --departures-count 2
```

`--departures-horizon` sets the look-ahead window in hours (default 48).

### MQTT

```bash
oepnv-check-realtime --watch --mqtt --mqtt-host broker.example.org
mosquitto_sub -h broker.example.org -v -t 'oepnv/#'
```

With `base_topic` at its default `oepnv`:

| Topic                                        | Payload                                                                                                                                                                    |
|----------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `oepnv/status`                               | per-cycle summary: feed ts/age, stale flag, per-line hit counts                                                                                                            |
| `oepnv/lines/<line>/status`                  | one line: `updates`, `delay_min/max/avg_s`, `static_trips`                                                                                                                 |
| `oepnv/departures`                           | with `--departures`: all upcoming departures in one document (`in_minutes`, `delay_s`, `realtime`, stop, times); published every cycle, empty list when the board is empty |
| `oepnv/departures/<line>/<stop>/<direction>` | one group's departures; ASCII-simplified segments (`departures/1/S_Blankenese/U_Kellinghusenstrasse`)                                                                      |
| `oepnv/alerts`                               | each new (deduplicated) service alert: `{entity, text}`                                                                                                                    |
| `oepnv/stale`                                | fired once on the transition into staleness                                                                                                                                |

Everything is published with `retain=False`: the broker keeps no last value and
subscribers see the live stream only. A late subscriber waits at most one poll
interval for the next status/departures cycle.

## Configuration

Precedence: CLI option > real environment variable > `./oepnv.local.env` (current
working directory) > `oepnv.local.env` next to the installed `check_realtime`
module. Both env files are auto-loaded via `python-dotenv` and never override
variables that are already set. An example env file ships in the repository, not
in the wheel:
https://github.com/vroomfondel/somestuff/tree/main/somestuff/oepnvstuff/

| Env var                          | CLI                            | Default                                                                                       |
|----------------------------------|--------------------------------|-----------------------------------------------------------------------------------------------|
| `OEPNV_STATIC_URL`               | `--static`                     | `https://download.gtfs.de/germany/nv_free/latest.zip` (URL or `.zip` path)                    |
| `OEPNV_REALTIME_URL`             | `--realtime`                   | `https://realtime.gtfs.de/realtime-free.pb` (URL or `.pb` path)                               |
| `OEPNV_LINES`                    | `--lines`, `-l`                | `1,12,22,189` (comma/space separated)                                                         |
| `OEPNV_STATION`                  | `--station`, `-s`              | `Blankenese` (case-insensitive substring match; several stations `;`-separated)               |
| `OEPNV_NEAR`                     | `--near`                       | off; `lat,lon,radius_km` geo filter for the matched stops, areas `;`-separated (OR)           |
| `OEPNV_ENABLE_DEPARTURES`        | `--departures/--no-departures` | off; report upcoming departures per line+direction                                            |
| `OEPNV_DEPARTURES_COUNT`         | `--departures-count`           | `3`; next N departures per line+direction                                                     |
| `OEPNV_DEPARTURES_HORIZON_HOURS` | `--departures-horizon`         | `48` h look-ahead                                                                             |
| `OEPNV_CACHE_DIR`                | `--cache-dir`                  | `.gtfs_cache`                                                                                 |
| `OEPNV_FORCE_REFRESH`            | `--force-refresh`              | off; re-download the static zip even on `304`                                                 |
| `OEPNV_WATCH`                    | `--watch`, `-w`                | off                                                                                           |
| `OEPNV_INTERVAL`                 | `--interval`                   | `180` s, see Bandwidth below                                                                  |
| `OEPNV_MAX_AGE`                  | `--max-age`                    | `120` s (`0` = off)                                                                           |
| `OEPNV_STALE_CYCLES`             | `--stale-cycles`               | `6`                                                                                           |
| `OEPNV_STOP_ON_STALE`            | `--stop-on-stale`              | off                                                                                           |
| `OEPNV_RECENT_WINDOW`            | `--recent-window`              | `900` s; a line still counts as `realtime_recent` this long after its last update (`0` = off) |
| `OEPNV_MQTT_ENABLE`              | `--mqtt/--no-mqtt`             | off                                                                                           |
| `OEPNV_MQTT_HOST`                | `--mqtt-host`                  | `mosquitto.mosquitto.svc.cluster.local`                                                       |
| `OEPNV_MQTT_PORT`                | `--mqtt-port`                  | `1883`                                                                                        |
| `OEPNV_MQTT_USER`                | `--mqtt-user`                  | anonymous                                                                                     |
| `OEPNV_MQTT_PASSWORD`            | `--mqtt-password`              | empty                                                                                         |
| `OEPNV_MQTT_BASE_TOPIC`          | `--mqtt-base-topic`            | `oepnv`                                                                                       |
| `OEPNV_MQTT_TLS`                 | `--mqtt-tls`                   | off; the port is not switched automatically, TLS brokers usually listen on `8883`             |
| `OEPNV_MQTT_TLS_CA`              | `--mqtt-tls-ca`                | system CA store; CA certificate path                                                          |
| `OEPNV_MQTT_TLS_CERT`            | `--mqtt-tls-cert`              | off; client certificate for mutual TLS (with `OEPNV_MQTT_TLS_KEY`)                            |
| `OEPNV_MQTT_TLS_KEY`             | `--mqtt-tls-key`               | off; client key for mutual TLS                                                                |
| `OEPNV_MQTT_TLS_INSECURE`        | `--mqtt-tls-insecure`          | off; skip hostname verification (self-signed certs), chain verification still applies         |
| `OEPNV_VERBOSE`                  | `--verbose`, `-v`              | off; DEBUG logging                                                                            |

`--show-stops` has no environment variable.

### Bandwidth

`OEPNV_INTERVAL` is the only lever on data volume, for a reason that is not
obvious:

- `RealtimeFetcher` sends conditional requests (`If-None-Match` /
  `If-Modified-Since`) and gtfs.de answers them correctly with `304`.
- gtfs.de rebuilds `realtime-free.pb` once per minute (`FeedHeader.timestamp`
  always lands on `:50`). With an interval of 60 s or less every poll is
  therefore a genuinely new body; the `304` is structurally unreachable, not
  broken.
- The feed is uncompressed (no `Content-Encoding`) and there is no smaller
  regional variant: `realtime-{nv,fv,rv}.pb` return 404, only the nationwide
  feed exists.

Volume therefore depends only on how many feed versions are fetched, not on how
often you poll. Polling faster than the rebuild costs nothing (the extra polls
are `304`s); polling slower is the only saving. Measured over 360 cycles: about
44 MB per version (min 29, max 53).

| Interval        | Versions/day | Volume/day |
|-----------------|--------------|------------|
| 20-60 s         | 1440         | ~61 GB     |
| 180 s (default) | 480          | ~20 GB     |
| 300 s           | 288          | ~12 GB     |
| 600 s           | 144          | ~6 GB      |

`OEPNV_MAX_AGE` does not have to grow with the interval: `age` is
`now - feed_ts` at fetch time and stays below 60 s because of the per-minute
rebuild, regardless of the interval.

### Kubernetes

Configure everything via env in the Deployment. Point `OEPNV_CACHE_DIR` at a
persistent volume so pod restarts reuse the large static zip via `304`, and
combine `OEPNV_WATCH=1` with `OEPNV_STOP_ON_STALE=1` so a dead feed exits with
code 2 and the kubelet restarts the pod. A ready-made Deployment (watch + MQTT +
cache volume) and a Secret example for the broker credentials are in the
repository, not in the wheel:
https://github.com/vroomfondel/somestuff/tree/main/somestuff/oepnvstuff/

## Library

The monitor knows nothing about consoles or brokers: it dispatches typed
results to registered handlers. Add your own the same way the console report and
the MQTT bridge do:

```python
from somestuff.oepnvstuff.gtfs_realtime import RealtimeFetcher
from somestuff.oepnvstuff.gtfs_static import build_index, obtain
from somestuff.oepnvstuff.monitor import CycleResult, RealtimeMonitor

index = build_index(obtain("https://download.gtfs.de/germany/nv_free/latest.zip", ".gtfs_cache"),
                    ["1", "189"], "Blankenese")
monitor = RealtimeMonitor(RealtimeFetcher("https://realtime.gtfs.de/realtime-free.pb"), index,
                          interval=180.0, stop_on_stale=True)

def my_handler(cycle: CycleResult) -> None:
    for line, status in cycle.per_line.items():
        print(line, status.updates, status.avg_delay)

monitor.add_cycle_handler(my_handler)          # every completed cycle
monitor.add_alert_handler(print)               # each new service alert (deduplicated)
monitor.add_stale_handler(print)               # edge-triggered on going stale
monitor.add_error_handler(print)               # fetch errors (loop keeps running)

raise SystemExit(monitor.watch())              # or: monitor.check_once()
```

Handler exceptions are logged and swallowed, so one misbehaving sink cannot kill
the poll loop.

## Data sources

The defaults are the free, registration-less feeds from gtfs.de (CC BY-SA 4.0,
https://gtfs.de/de/open-data/): static `germany/nv_free/latest.zip` (German
regional transit) and `realtime-free.pb` (TripUpdates + ServiceAlerts). Any other
GTFS / GTFS-RT pair works via `--static` / `--realtime`, for example an agency's
own feed if the open aggregate turns out to lack your lines (for HVV territory:
Geofox-GTI).

## Notes

- Requires: `loguru`, `typer`, `gtfs-realtime-bindings`, `python-dotenv`,
  `requests`, `tabulate`, `mqttstuff`.
- Licensed under the LGPL, see `LICENSE.md` in the repository root.
