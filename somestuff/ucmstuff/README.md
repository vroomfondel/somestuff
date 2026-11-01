# ucmstuff

Monitor and control a **Grandstream UCM6204** IP-PBX from Python: receive real-time
call events over WebSocket, act on them via the HTTPS API, route incoming trunk
calls by caller, and optionally bridge everything onto MQTT.

The UCM6204 exposes **two separate** interfaces with different session models:

| Interface                     | Purpose                                                                                                | Auth                                | Client           |
|-------------------------------|--------------------------------------------------------------------------------------------------------|-------------------------------------|------------------|
| **WebSocket** (`/websockify`) | UCM *pushes* real-time events (`ActiveCallStatus`, `ExtensionStatus`, `PbxStatus`, ...)                | **web** user (e.g. `administrator`) | `UCMEventClient` |
| **HTTPS API** (`/api`)        | request/response *control* + queries (`acceptCall`, `refuseCall`, `Hangup`, `dialExtension`, CDR, ...) | **API** user (e.g. `cdrapi`)        | `UCM6204`        |

They are genuinely independent: the API user does **not** work on the WebSocket,
and the WebSocket does **not** accept HTTPS-API query actions. To both *monitor*
and *control* calls you run both clients together.

## Modules

- **`ucm6204_api`** — the core: `UCMEventClient` (WebSocket events, auto-reconnect
  and heartbeat), `UCM6204` (HTTPS-API control), `TrunkCallRouter` / `IncomingCall`
  (route incoming calls on a trunk to a caller-based branch), a `/healthz` server
  for Kubernetes probes, and the Typer CLI (`ucm6204`).
- **`ucm6204_api_rest`** — `UCM6204Rest`, a subclass adding a named, typed,
  documented method for **every** HTTPS-API action (trunks, routes, IVRs, queues,
  paging, accounts, users, dialing/transfer, ...) on top of the generic `api_call`.
- **`mqtt_bridge`** — `MqttEventBridge` publishes events and parsed incoming calls
  to MQTT and, optionally, accepts `accept` / `refuse` / `hangup` commands. It is the
  only module that imports `mqttstuff`. Topics (base topic defaults to `ucm6204`),
  all published with `retain=False`:

  ```
  ucm6204/events/<eventname>           full notify event as JSON
  ucm6204/calls/<trunk>/incoming       parsed IncomingCall as JSON
  ucm6204/calls/<trunk>/ended          same call once its leg terminates
  ucm6204/cmd/{accept,refuse,hangup}   inbound, payload {"channel": "PJSIP/..."}
  ```

  `<trunk>` is the call's `inbound_trunk_name`, sanitized for use as a topic segment.
- **`example_router`** — runnable example wiring `UCMEventClient`, `UCM6204` and
  `TrunkCallRouter` together from environment variables (see Library).
- **`/healthz`** — served by the CLI on `--health-host` / `--health-port`
  (default `0.0.0.0:8070`); returns `200` with `{"connected": true}` once the
  WebSocket is connected and subscribed, otherwise `503`. The process only makes
  **outbound** connections to the UCM, so no service or ingress is needed.

Logging (loguru, with an intercept for stdlib `logging`) and the startup banner are
shared via `somestuff.ucmstuff`.

## Installation

```bash
pip install somestuff-ucmstuff
```

`somestuff` is a PEP 420 namespace; the package is imported as
`somestuff.ucmstuff` and shares the namespace with the other `somestuff-*`
distributions.

### Setup on the UCM

1. **API Configuration -> HTTPS API Settings (New)**: enable it, set the API
   username/password, and tick **Call Control** (required for accept/refuse).
2. Create a **web user** for the WebSocket (a limited one is fine; the API user
   does not work there).

Both interfaces negotiate a weak Diffie-Hellman group; the clients lower the
OpenSSL security level (`@SECLEVEL=1`) automatically so the handshake succeeds.

## Usage

The console script and module invocation are equivalent:

```bash
ucm6204 \
  --host ucm.example.lan --port 8089 \
  --web-user webuser --web-password '...' \
  --api-user cdrapi   --api-password '...' \
  --trunk MyTrunk --trunk AnotherTrunk       # repeatable; omit for monitor-only

python3 -m somestuff.ucmstuff.ucm6204_api --host ucm.example.lan --web-user webuser --web-password '...' -v
```

Every event is logged; `--trunk` attaches a `TrunkCallRouter` and logs incoming
calls on those trunks. Add your own branching in code (see Library).

Options:

| Option                                                                                    | Default                                      | Meaning                                                         |
|-------------------------------------------------------------------------------------------|----------------------------------------------|-----------------------------------------------------------------|
| `--host`, `--web-user`, `--web-password`                                                  | required                                     | UCM address and web-user credentials for the WebSocket          |
| `--api-user`, `--api-password`                                                            | empty                                        | API-user credentials; empty disables control actions            |
| `--port`                                                                                  | `8089`                                       | HTTPS-API / WebSocket port                                      |
| `--events`                                                                                | `ActiveCallStatus,ExtensionStatus,PbxStatus` | comma-separated event names to subscribe to                     |
| `--trunk`                                                                                 | none                                         | inbound trunk to route calls for; repeatable                    |
| `--health-host`, `--health-port`                                                          | `0.0.0.0`, `8070`                            | bind address and port of the `/healthz` server                  |
| `--mqtt` / `--no-mqtt`                                                                    | off                                          | enable the MQTT bridge                                          |
| `--mqtt-host`, `--mqtt-port`                                                              | empty, `1883`                                | broker (host required with `--mqtt`)                            |
| `--mqtt-user`, `--mqtt-password`                                                          | empty                                        | broker credentials; empty user = anonymous                      |
| `--mqtt-base-topic`                                                                       | `ucm6204`                                    | root of the topic tree                                          |
| `--mqtt-commands`                                                                         | off                                          | accept commands on `<base>/cmd/#` (requires `--api-user`)       |
| `--mqtt-tls`, `--mqtt-tls-ca`, `--mqtt-tls-cert`, `--mqtt-tls-key`, `--mqtt-tls-insecure` | off, empty                                   | broker TLS, CA, mutual-TLS client cert/key, skip hostname check |
| `-v`, `--verbose`                                                                         | off                                          | debug logging                                                   |

`ucm6204 --help` shows the exact flag forms.

## Configuration

The CLI takes UCM connection settings only as flags; the `--mqtt*` options also
read environment variables. `example_router` reads the connection settings from
the environment. Boolean variables accept `true|false` (`1|0` works as well).

| Variable                                | Read by          | Meaning                                                                                              |
|-----------------------------------------|------------------|------------------------------------------------------------------------------------------------------|
| `UCM_HOST`                              | `example_router` | UCM address (required)                                                                               |
| `UCM_PORT`                              | `example_router` | HTTPS-API / WebSocket port, default `8089`                                                           |
| `UCM_API_USER`, `UCM_API_PASSWORD`      | `example_router` | API user for control actions (needs "HTTPS API" + "Call Control")                                    |
| `UCM_WEB_USER`, `UCM_WEB_PASSWORD`      | `example_router` | web user for the WebSocket event stream                                                              |
| `UCM_TRUNKS`                            | `example_router` | space-separated inbound trunk names; empty = monitor only                                            |
| `UCM_MQTT_ENABLE`                       | CLI (`--mqtt`)   | enable the MQTT bridge, default off                                                                  |
| `UCM_MQTT_HOST`, `UCM_MQTT_PORT`        | CLI              | broker, default port `1883`                                                                          |
| `UCM_MQTT_USER`, `UCM_MQTT_PASSWORD`    | CLI              | broker credentials                                                                                   |
| `UCM_MQTT_BASE_TOPIC`                   | CLI              | topic root, default `ucm6204`                                                                        |
| `UCM_MQTT_COMMANDS`                     | CLI              | accept accept/refuse/hangup on `<base>/cmd/#` (needs an API user)                                    |
| `UCM_MQTT_TLS`                          | CLI              | TLS to the broker; the port is not switched, set `UCM_MQTT_PORT` (usually `8883`)                    |
| `UCM_MQTT_TLS_CA`                       | CLI              | CA certificate path; empty = system CA store                                                         |
| `UCM_MQTT_TLS_CERT`, `UCM_MQTT_TLS_KEY` | CLI              | client certificate and key for mutual TLS (both or neither)                                          |
| `UCM_MQTT_TLS_INSECURE`                 | CLI              | skip hostname verification (self-signed); still encrypted but MITM-able, chain verification stays on |

There is no env-file loader: put the variables in a file of your own, for example
`ucm.local.env`, and export them into the shell before starting:

```bash
set -a; . ./ucm.local.env; set +a
python3 -m somestuff.ucmstuff.example_router
ucm6204 --host "$UCM_HOST" --port "$UCM_PORT" --web-user "$UCM_WEB_USER" --web-password "$UCM_WEB_PASSWORD" \
  --api-user "$UCM_API_USER" --api-password "$UCM_API_PASSWORD"
```

The repository additionally contains `ucm.env.example`, a Kubernetes secret example
and `somestuff_ucm6204_deployment.yml` (one pod = events + control, `/healthz`
backing the liveness/readiness probes); they are not part of the installed package:
https://github.com/vroomfondel/somestuff/tree/main/somestuff/ucmstuff/

Keep real credentials in `*.local.*` files that are not committed.

## Library

Coordinated monitor + control, as in `example_router`:

```python
from somestuff.ucmstuff.ucm6204_api import UCM6204, UCMEventClient, TrunkCallRouter, IncomingCall

api = UCM6204(host="ucm.example.lan", api_user="cdrapi", api_password="...")
api.connect()

events = UCMEventClient(host="ucm.example.lan", web_user="webuser", web_password="...")

def route(call: IncomingCall) -> None:
    if call.number == "+491234567":
        api.accept_call(call.channel)
    elif call.name.upper().startswith("SPAM"):
        api.refuse_call(call.channel)

events.add_event_handler(TrunkCallRouter("MyTrunk", on_call=route).handle)
events.run(block=True)
```

Full API access:

```python
from somestuff.ucmstuff.ucm6204_api_rest import UCM6204Rest

ucm = UCM6204Rest(host="ucm.example.lan", api_user="cdrapi", api_password="...")
ucm.connect()
for t in ucm.list_voip_trunks():
    print(t["trunk_name"], ucm.get_sip_trunk(trunk=str(t["trunk_index"]))["status"])
```

## Grandstream documentation (and what is outdated)

Grandstream's UCM API docs are inconsistent across firmware generations, and the
widely-linked PDF describes an event mechanism this firmware no longer uses:

- **Outdated: the `url` report-push model.** The UCM6xxx HTTPS API Guide (PDF)
  documents a `url` parameter on the `login` action: the UCM is supposed to *POST*
  "system reports and call reports" to that HTTP URL. On current UCM6204 firmware
  the `url` is still **accepted and stored**, but **nothing is POSTed to it** for
  call events; it is a dead legacy field.
- **Actual: the WebSocket model.** Real-time events arrive over a **WebSocket**
  (`wss://<host>:<port>/websockify`): the client connects and performs a JSON
  `challenge` -> `login` -> `subscribe` handshake, then receives `notify` frames.
  This is a *separate* session from the HTTPS API, and it authenticates with a
  **web** user, not the API user.

`UCMEventClient` implements the WebSocket model; the `url`-POST model is
intentionally not used. `UCM6204` / `UCM6204Rest` implement the request/response
HTTPS API from the same guide, which is still current.

- HTTPS API knowledge base: https://documentation.grandstream.com/knowledge-base/https-api
- UCM6xxx HTTPS API Guide (PDF): https://www.grandstream.com/hubfs/Product_Documentation/UCM_API_Guide.pdf

## Notes

- **`inbound_trunk_name` is not `trunk_name`**: the trunk name in an `ActiveCallStatus`
  event can differ from the configured name in `listVoIPTrunk`. Verify against a
  real call (every event is logged) and set `UCM_TRUNKS` / `--trunk` accordingly.
- Requires: `loguru`, `typer`, `requests`, `urllib3`, `websocket-client`, `tabulate`,
  `mqttstuff`. Linux only.
- Licensed under the LGPL, see `LICENSE.md` in the repository root.
