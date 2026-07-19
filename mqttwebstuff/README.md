# mqttwebstuff

A tiny live web view onto arbitrary MQTT streams: FastAPI subscribes to the
broker, a **mapper plugin** (a plain, mountable Python file) filters/maps every
message, and the browser receives rendered HTML fragments via **Server-Sent
Events** — displayed with **htmx** (+ SSE extension) on **PicoCSS**, zero
hand-written JavaScript.

```
MQTT broker ──> paho thread ──> ViewHub (last-value cache) ──> SSE ──> htmx swaps panels
                                    │
                              mapper plugin (.py)
                              filter / panel / key / template / ttl
```

## Why a cache in the middle?

The oepnv stream (and this repo's other streams) publish with `retain=False`.
The hub keeps the last rendered item per `(panel, key)`, so a freshly opened
tab gets the full current board server-rendered on `GET /`, then only deltas
over `GET /stream`. Items carry a TTL, so topics that stop being published
disappear from the board — mirroring the non-retained semantics.

## Running

```bash
# oepnv departure board (reference plugin, ships in the image);
# --mapper accepts a file path OR a dotted module name
python3 -m mqttwebstuff.serve --mapper mqttwebstuff/plugins/oepnv_view.py --mqtt-host broker.example.org
python3 -m mqttwebstuff.serve --mapper mqttwebstuff.plugins.oepnv_view --mqtt-host broker.example.org

# generic JSON-card view of any stream
python3 -m mqttwebstuff.serve --topics 'ecowitt/#,oepnv/status' --mqtt-host broker.example.org

# ad-hoc: peek into a whole topic tree (one card per subtopic, newest payload
# wins). --mapper "" explicitly overrides a mapper configured via env /
# mqttweb.local.env — a set mapper otherwise wins over --topics. --item-ttl 0
# keeps cards forever (default: gone after 15 min without a new message).
python3 -m mqttwebstuff.serve --mapper "" --topics 'nodered/#' --item-ttl 0 --title "Node-RED Live"
```

All options are also environment variables (`MQTTWEB_*`, CLI wins) — see
`python3 -m mqttwebstuff.serve --help` or the annotated catalog in
`mqttweb.env.example` (copy to `mqttweb.local.env`, gitignored, auto-loaded).
For Kubernetes credentials see `somestuff-mqttweb-secret.example.yaml`. TLS
options mirror oepnvstuff's (`--mqtt-tls`, `--mqtt-tls-ca`, mTLS cert/key,
`--mqtt-tls-insecure`).

## Writing a mapper plugin

One file, no packaging:

```python
from mqttwebstuff.plugin_api import ViewEvent

TITLE = "My Stream"
SUBSCRIPTIONS = ["mystream/#"]
PANELS = {"main": "Main"}          # optional: fixes panel order + headings

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

Templates: put Jinja2 files (`*.html.j2`) into a `templates/` directory next
to the plugin file (or set `TEMPLATE_DIR`). The plugin's directory is searched
first, the built-ins second — a plugin can therefore also override
`base.html.j2` or add styles via an `extra_head.html.j2` include. The `hhmm`
filter formats ISO timestamps as `HH:MM`.

In Kubernetes, mount the plugin (+ templates) from a ConfigMap and point
`MQTTWEB_MAPPER` at it — see `somestuff_mqttweb_deployment.yml`. The oepnv
reference plugin ships inside the image, so the oepnv board needs no mount at
all.

## Endpoints

| Route      | Purpose                                                        |
|------------|----------------------------------------------------------------|
| `/`        | Full board, server-rendered from the cache                     |
| `/stream`  | SSE: `panel:<name>` repaints, `board` on new panels, keepalives |
| `/healthz` | `{"ok": true, "mqtt_connected": ...}`                          |

## Vendored assets

`static/` contains pinned copies of htmx 2.0.6, htmx-ext-sse 2.2.3 and
PicoCSS 2.1.1 so the pod serves everything itself (no CDN egress).
