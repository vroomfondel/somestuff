# ucmstuff

Monitor and control a **Grandstream UCM6204** IP-PBX from Python: receive real-time
call events over WebSocket and act on them via the HTTPS API.

## What it does

The UCM6204 exposes **two separate** interfaces with different session models:

| Interface                     | Purpose                                                                                              | Auth                                | Client           |
|-------------------------------|------------------------------------------------------------------------------------------------------|-------------------------------------|------------------|
| **WebSocket** (`/websockify`) | UCM *pushes* real-time events (`ActiveCallStatus`, `ExtensionStatus`, `PbxStatus`, …)                | **web** user (e.g. `administrator`) | `UCMEventClient` |
| **HTTPS API** (`/api`)        | request/response *control* + queries (`acceptCall`, `refuseCall`, `Hangup`, `dialExtension`, CDR, …) | **API** user (e.g. `cdrapi`)        | `UCM6204`        |

They are genuinely independent: the API user does **not** work on the WebSocket,
and the WebSocket does **not** accept HTTPS-API query actions. To both *monitor*
and *control* calls you run both clients together — that is the "coordinated"
setup demonstrated by `example_router.py` and `somestuff_ucm6204_deployment.yml`.

### Modules

- **`ucm6204_api.py`** — the core: `UCM6204` (HTTPS-API control), `UCMEventClient`
  (WebSocket events, auto-reconnect + heartbeat), `TrunkCallRouter` /
  `IncomingCall` (route incoming calls on a trunk to a caller-based branch), a
  `/healthz` server for Kubernetes probes, and a Typer CLI (`main`).
- **`ucm6204_api_rest.py`** — `UCM6204Rest`, a subclass adding a named, typed,
  documented method for **every** HTTPS-API action (trunks, routes, IVRs, queues,
  paging, accounts, users, dialing/transfer, …) on top of the generic `api_call`.

## Setup (on the UCM)

1. **API Configuration → HTTPS API Settings (New)**: enable it, set the API
   username/password, and tick **Call Control** (required for accept/refuse).
2. Create a **web user** for the WebSocket (a limited one is fine; the API user
   does not work there).

Both interfaces negotiate a weak Diffie-Hellman group; the clients lower the
OpenSSL security level (`@SECLEVEL=1`) automatically so the handshake succeeds.

## Usage

### CLI

```bash
python3 -m ucmstuff.ucm6204_api \
  --host ucm.example.lan --port 8089 \
  --web-user webuser --web-password '…' \
  --api-user cdrapi   --api-password '…' \
  --trunk MyTrunk --trunk AnotherTrunk       # repeatable; omit for monitor-only
```

Every event is logged; `--trunk` attaches a `TrunkCallRouter` and logs incoming
calls on those trunks. Add your own branching in code (below).

### Library — coordinated monitor + control

See `example_router.py`. In short:

```python
from ucmstuff.ucm6204_api import UCM6204, UCMEventClient, TrunkCallRouter, IncomingCall

api = UCM6204(host="ucm.example.lan", api_user="cdrapi", api_password="…")
api.connect()

events = UCMEventClient(host="ucm.example.lan", web_user="webuser", web_password="…")

def route(call: IncomingCall) -> None:
    if call.number == "+491234567":
        api.accept_call(call.channel)
    elif call.name.upper().startswith("SPAM"):
        api.refuse_call(call.channel)

events.add_event_handler(TrunkCallRouter("MyTrunk", on_call=route).handle)
events.run(block=True)
```

### Full API access

```python
from ucmstuff.ucm6204_api_rest import UCM6204Rest

ucm = UCM6204Rest(host="ucm.example.lan", api_user="cdrapi", api_password="…")
ucm.connect()
for t in ucm.list_voip_trunks():
    print(t["trunk_name"], ucm.get_sip_trunk(trunk=str(t["trunk_index"]))["status"])
```

## Deployment

Coordinated deployment (one pod = events + control):

```bash
kubectl apply -f ucmstuff/somestuff-ucm6204-secret.local.yaml   # your filled-in copy
kubectl apply -f ucmstuff/somestuff_ucm6204_deployment.yml
```

The pod only makes **outbound** connections to the UCM — no service/ingress
needed. `/healthz` (port 8070) backs the liveness/readiness probes and returns
`200` once the WebSocket is connected and subscribed. The image must ship
`requests`, `typer` and `websocket-client`.

## Grandstream documentation (and what's outdated)

Grandstream's UCM API docs are inconsistent across firmware generations, and the
widely-linked PDF describes an event mechanism this firmware no longer uses:

- **Outdated — the `url` report-push model.** The
  [UCM6xxx HTTPS API Guide (PDF)][pdf] documents a `url` parameter on the `login`
  action: the UCM is supposed to *POST* "system reports and call reports" to that
  HTTP URL. On current UCM6204 firmware the `url` is still **accepted and stored**,
  but **nothing is POSTed to it** for call events — it is a dead legacy field.
  Follow that guide and you register a URL and never receive anything.
- **Actual — the WebSocket model.** Real-time events arrive over a **WebSocket**
  (`wss://<host>:<port>/websockify`): the client connects and performs a JSON
  `challenge` → `login` → `subscribe` handshake, then receives `notify` frames.
  This is a *separate* session from the HTTPS API — see the
  [HTTPS API knowledge base][kb] ("WebSocket is supported … for immediate
  notifications and reports"). Crucially it authenticates with a **web** user, not
  the API user.

`UCMEventClient` implements the WebSocket model; the `url`-POST model is
intentionally not used. `UCM6204` / `UCM6204Rest` implement the request/response
HTTPS API from the same guide — that part is still current.

Original documentation:

- HTTPS API knowledge base: <https://documentation.grandstream.com/knowledge-base/https-api>
- UCM6xxx HTTPS API Guide (PDF): <https://www.grandstream.com/hubfs/Product_Documentation/UCM_API_Guide.pdf>

[kb]: https://documentation.grandstream.com/knowledge-base/https-api
[pdf]: https://www.grandstream.com/hubfs/Product_Documentation/UCM_API_Guide.pdf

## Notes

- **`inbound_trunk_name` ≠ `trunk_name`**: the trunk name in an `ActiveCallStatus`
  event can differ from the configured name in `listVoIPTrunk`. Verify against a
  real call (every event is logged) and set `UCM_TRUNKS` / `--trunk` accordingly.
- Requires: `requests`, `typer`, `websocket-client`.
- Config files with real credentials go in `*.local.*` (gitignored); commit only
  the `.example` variants.
