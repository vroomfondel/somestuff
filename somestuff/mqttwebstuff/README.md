# mqttwebstuff

A tiny live web view onto arbitrary MQTT streams: FastAPI subscribes to the
broker, a **mapper plugin** (a plain, mountable Python file or module)
filters/maps every message, and the browser receives rendered HTML fragments via
**Server-Sent Events**, displayed with **htmx** (+ SSE extension) on **PicoCSS**.
No frontend build, no framework; the only hand-written JavaScript is a dozen
lines in `base.html.j2` preserving open `<details>` states across SSE swaps.

## Modules

- **`serve.py`**: Typer CLI (`mqttweb-serve`). Loads the mapper plugin (or the
  generic fallback), connects to the broker and runs the web app with uvicorn.
  The MQTT connection tolerates an unreachable broker at startup: paho keeps
  retrying in the background while the web app is already up (empty board,
  `/healthz` showing `mqtt_connected: false`).
- **`hub.py`**: `ViewHub`, the last-value cache between MQTT and the browsers.
  It keeps the last rendered item per `(panel, key)` and fans updates out to all
  SSE clients. Messages are submitted thread-safely from paho's network thread.
- **`plugin_api.py`**: the contract between core and plugin. `ViewEvent`
  describes one mapped message (panel, key, data, template, sort, group, title,
  ttl); `load_plugin` validates a mapper given as a file path or dotted module.
- **`webapp.py`**: the FastAPI app and the Jinja2 environment (including the
  `hhmm` filter).
- **`plugins/oepnv_view.py`**: reference mapper rendering the `oepnvstuff`
  GTFS-realtime stream as a departure board (departures, feed status,
  per-line coverage, service alerts). It follows the publisher's topic base via
  `OEPNV_MQTT_BASE_TOPIC` (default `oepnv`; multi-level bases like `home/oepnv`
  work).
- **`templates/`, `static/`**: built-in Jinja2 templates (generic JSON card,
  indented topic tree) and vendored htmx, htmx-ext-sse and PicoCSS, so the
  server serves everything itself (no CDN egress).

Logging (loguru, with an intercept for stdlib `logging` from `paho` and
`uvicorn`) and the startup banner are shared via `somestuff.mqttwebstuff`.

```
MQTT broker ──> paho thread ──> ViewHub (last-value cache) ──> SSE ──> htmx swaps panels
                                    │
                              mapper plugin
                              filter / panel / key / template / ttl
```

### Why a cache in the middle?

Streams such as the oepnv one publish with `retain=False`. The hub keeps the
last rendered item per `(panel, key)`, so a freshly opened tab gets the full
current board server-rendered on `GET /`, then only deltas over `GET /stream`.
Items carry a TTL, so topics that stop being published disappear from the
board, mirroring the non-retained semantics.

### Endpoints

| Route      | Purpose                                                         |
|------------|-----------------------------------------------------------------|
| `/`        | Full board, server-rendered from the cache                      |
| `/stream`  | SSE: `panel:<name>` repaints, `board` on new panels, keepalives |
| `/healthz` | `{"ok": true, "mqtt_connected": ...}`                           |

### Deep links

Group boxes (e.g. one stop on the oepnv board) and tree rows carry stable,
ASCII-slugged anchor ids: `/#g-<panel>-<label>` and `/#t-<topic>`, e.g.
`/#g-departures-ellerbek-moordamm` or `/#t-nodered-hydrostatics-hydrostatic1`.
Hovering a label reveals its self-link (`#`); the jump target is highlighted.
Since ids survive repaints and restarts, the links are bookmarkable.

## Installation

```bash
pip install somestuff-mqttwebstuff
```

`somestuff` is a PEP 420 namespace; the package is imported as
`somestuff.mqttwebstuff` and shares the namespace with the other `somestuff-*`
distributions.

## Usage

The console script and module invocation are equivalent:

```bash
mqttweb-serve --mapper somestuff.mqttwebstuff.plugins.oepnv_view --mqtt-host broker.example.org
python3 -m somestuff.mqttwebstuff.serve --mapper somestuff.mqttwebstuff.plugins.oepnv_view --mqtt-host broker.example.org
```

`--mapper` accepts a dotted module name on the import path (as above) or the
path of a `.py` file, e.g. `--mapper /config/my_view.py`. In a repository
checkout, `--mapper somestuff/mqttwebstuff/plugins/oepnv_view.py` works as well.

Without a mapper, a generic view shows every message of the `--topics` patterns:

```bash
# default layout: indented hierarchical topic tree (branch rows per level,
# scalars inline, JSON collapsible)
mqttweb-serve --topics 'ecowitt/#,oepnv/status' --mqtt-host broker.example.org

# ad-hoc: peek into a whole topic tree and keep items forever
mqttweb-serve --mapper "" --topics 'nodered/#' --item-ttl 0 --title "Node-RED Live"

# one JSON card per topic instead of the tree
mqttweb-serve --mapper "" --topics 'nodered/#' --view flat
```

`--mapper ""` explicitly overrides a mapper configured via environment or env
file; a configured mapper otherwise wins over `--topics`. `--item-ttl` defaults
to 900 seconds (an item without a new message disappears), `0` keeps items
forever. `mqttweb-serve --help` lists all options.

## Configuration

Every option is also an environment variable (CLI wins over environment). The
tool additionally loads `mqttweb.local.env` from the current working directory
and from the package directory; precedence is real environment, then the CWD
file, then the package-directory file, with CLI options over all.

| Variable                    | CLI option            | Default                                 | Meaning                                                                                                                                                                           |
|-----------------------------|-----------------------|-----------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `MQTTWEB_MAPPER`            | `--mapper`            | empty (generic view)                    | Mapper plugin: `.py` file path or dotted module name                                                                                                                              |
| `MQTTWEB_TOPICS`            | `--topics`            | `#`                                     | Comma-separated subscribe patterns, generic view only                                                                                                                             |
| `MQTTWEB_ITEM_TTL`          | `--item-ttl`          | `900`                                   | Generic view: seconds until an unrefreshed card disappears, `0` = forever                                                                                                         |
| `MQTTWEB_VIEW`              | `--view`              | `hierarchical`                          | Generic view layout: `hierarchical` or `flat`                                                                                                                                     |
| `MQTTWEB_TITLE`             | `--title`             | empty (plugin's `TITLE`)                | Page title override                                                                                                                                                               |
| `MQTTWEB_LISTEN_HOST`       | `--listen-host`       | `0.0.0.0`                               | Bind address                                                                                                                                                                      |
| `MQTTWEB_LISTEN_PORT`       | `--listen-port`       | `8080`                                  | Bind port                                                                                                                                                                         |
| `MQTTWEB_LIMIT_CONCURRENCY` | `--limit-concurrency` | `0` (unlimited)                         | Reject new connections with 503 above N concurrent ones. Every open tab holds one SSE stream and a page load briefly needs several slots, so size generously (100+), not per user |
| `MQTTWEB_MQTT_HOST`         | `--mqtt-host`         | `mosquitto.mosquitto.svc.cluster.local` | MQTT broker host                                                                                                                                                                  |
| `MQTTWEB_MQTT_PORT`         | `--mqtt-port`         | `1883`                                  | MQTT broker port                                                                                                                                                                  |
| `MQTTWEB_MQTT_USER`         | `--mqtt-user`         | empty (anonymous)                       | MQTT username                                                                                                                                                                     |
| `MQTTWEB_MQTT_PASSWORD`     | `--mqtt-password`     | empty                                   | MQTT password                                                                                                                                                                     |
| `MQTTWEB_MQTT_TLS`          | `--mqtt-tls`          | `false`                                 | Encrypt the MQTT connection; the port is not switched automatically, set `--mqtt-port` (usually 8883)                                                                             |
| `MQTTWEB_MQTT_TLS_CA`       | `--mqtt-tls-ca`       | empty (system CA store)                 | CA certificate path                                                                                                                                                               |
| `MQTTWEB_MQTT_TLS_CERT`     | `--mqtt-tls-cert`     | empty                                   | Client certificate path (mutual TLS)                                                                                                                                              |
| `MQTTWEB_MQTT_TLS_KEY`      | `--mqtt-tls-key`      | empty                                   | Client key path (mutual TLS)                                                                                                                                                      |
| `MQTTWEB_MQTT_TLS_INSECURE` | `--mqtt-tls-insecure` | `false`                                 | Skip TLS hostname verification (self-signed certs); encrypted but MITM-able                                                                                                       |
| `MQTTWEB_VERBOSE`           | `--verbose`, `-v`     | `false`                                 | DEBUG logging, including the uvicorn access log                                                                                                                                   |

`--topics`, `--item-ttl` and `--view` are ignored when a mapper is set.
Plugin-specific settings are plain environment variables the plugin reads
itself; the oepnv plugin reads `OEPNV_MQTT_BASE_TOPIC` at load time.

The repository additionally contains an annotated `mqttweb.env.example`, a
Kubernetes deployment example and a secret example (not part of the installed
package): https://github.com/vroomfondel/somestuff/tree/main/somestuff/mqttwebstuff/

## Writing a mapper plugin

A plugin is one Python file (or an importable module), no packaging needed.
Required attributes: `SUBSCRIPTIONS` and `map_message`. Optional: `TITLE`,
`PANELS` (panel name to heading, fixes the board order; an empty heading means a
plain panel without chrome) and `TEMPLATE_DIR`. `payload` is already decoded: a
`dict`/`list` when the raw payload parses as JSON, otherwise the raw string.

### Library

```python
from somestuff.mqttwebstuff.plugin_api import ViewEvent

TITLE = "My Stream"
SUBSCRIPTIONS = ["mystream/#"]
PANELS = {"main": "Main"}          # optional: fixes panel order + headings

# May also return a list of ViewEvents (one message -> several board items,
# e.g. a leaf plus synthesized ancestor rows, as the generic hierarchical view
# does) or None to drop the message.
def map_message(topic: str, payload) -> ViewEvent | None:
    if not isinstance(payload, dict):
        return None                # filter
    return ViewEvent(
        panel="main",              # board section
        key=topic,                 # stable identity -> idempotent replace
        data=payload,              # template context
        template=None,             # None = built-in generic JSON card
        sort="",                   # ordering within the panel/group ("" = by key)
        group="",                  # optional sub-heading label (e.g. stop name);
                                   # items cluster under it, "" = ungrouped
        ttl=300.0,                 # vanish after 5 min without re-publish
    )
```

Save it as `my_view.py` and run `mqttweb-serve --mapper my_view.py --mqtt-host broker.example.org`.

Templates: put Jinja2 files (`*.html.j2`) into a `templates/` directory next to
the plugin file (or set `TEMPLATE_DIR`, relative to the plugin's directory). The
plugin's directory is searched first, the built-ins second, so a plugin can also
override `base.html.j2` or add styles via an `extra_head.html.j2` include. The
`hhmm` filter formats ISO timestamps as `HH:MM`.

In Kubernetes, mount the plugin (+ templates) from a ConfigMap and point
`MQTTWEB_MAPPER` at it; see the deployment example in the repository (link
above). The oepnv reference plugin is part of the package, so the oepnv board
needs no mount at all.

## Notes

- Requires: `loguru`, `typer`, `fastapi`, `uvicorn`, `jinja2`, `python-dotenv`,
  `tabulate`, `mqttstuff`.
- Licensed under the LGPL, see `LICENSE.md` in the repository root.
