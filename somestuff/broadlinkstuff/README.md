# broadlinkstuff

Talk to **Broadlink RM4** IR/RF blasters from Python: address devices without a
broadcast, learn IR/RF codes, send stored or raw codes — and, for Midea/Coolix
air conditioners, **compute** the complete state matrix instead of learning all
280 combinations one by one in front of the device.

## Modules

- **`broadlinkhelper.py`** — everything in one module: the device fleet, the
  Coolix codec and the Typer CLI.
  - **Device fleet** — devices are configured by name, host, `devtype` and MAC,
    and confirmed by a unicast `hello()` (about 0.02 s per device against about
    5 s for a broadcast). A device whose IP moved is re-found by MAC through a
    broadcast fallback. Commands: `devices`, `sensors` (temperature/humidity),
    `discover`, `rename`.
  - **IR/RF learning** — `learn-ir` and `learn-rf` print the learned code as hex
    on stdout; `send` transmits a named code from the built-in `CODES` library or
    raw hex, `codes` lists the library, `decode` analyses a packet without a
    device.
  - **Coolix (Midea) climate codec** — `climate` computes mode × fan ×
    temperature codes, `special` sends the commands that are not a state (`off`,
    `swing`, `sleep`, ...), `selftest` checks the generator against learned
    reference codes.
  - **Dry-run** — `--dry-run` sends nothing and only shows what would be sent.
- **`ac_api.py`** — a small FastAPI in front of per-room shell scripts
  (`activate_`/`deactivate_`/`gettemp_ac_<room>.sh`). Rooms are discovered from
  the script directory, so a new room is three scripts, not a code change. The
  scripts can be anything that drives the blaster (`broadlinkhelper`, a
  `podman run`, ...); calls are serialised per room so two IR bursts never
  interleave at one device. Needs the `api` extra.

Logging (loguru, with an intercept for stdlib `logging` from `broadlink`) and the
startup banner are shared via `somestuff.broadlinkstuff`.

## Installation

```bash
pip install somestuff-broadlinkstuff          # CLI only
pip install 'somestuff-broadlinkstuff[api]'   # + broadlink-ac-api (fastapi, uvicorn)
```

`somestuff` is a PEP 420 namespace; the package is imported as
`somestuff.broadlinkstuff` and shares the namespace with the other `somestuff-*`
distributions.

## Usage

The console script and module invocation are equivalent:

```bash
broadlinkhelper devices
python3 -m somestuff.broadlinkstuff.broadlinkhelper devices
```

```bash
broadlinkhelper devices                    # what is reachable
broadlinkhelper sensors                    # temperature/humidity, all of them
broadlinkhelper sensors Lounge --json      # one device, machine-readable
broadlinkhelper discover                   # broadcast + config block
broadlinkhelper learn-ir Lounge            # press a button, hex comes out
broadlinkhelper learn-rf Lounge            # needs a device with an RF part
broadlinkhelper send Lounge off            # code from CODES
broadlinkhelper send Lounge 2600ca00...    # raw hex
broadlinkhelper climate Lounge --temp 23   # Coolix computed
broadlinkhelper special Lounge swing       # swing on/off (toggle)
broadlinkhelper special Lounge swing-v-step -n 3   # vane 3 steps
broadlinkhelper rename Lounge Wohnzimmer   # write the device name
broadlinkhelper codes --hex                # the code library
broadlinkhelper decode 2600ca00938e...     # no device, just maths
broadlinkhelper selftest
```

Subcommands:

| Command    | Purpose                                                                                     |
|------------|---------------------------------------------------------------------------------------------|
| `devices`  | List the configured devices that are reachable.                                             |
| `sensors`  | Temperature/humidity of one device or all of them; `--json` keys the output by device name. |
| `discover` | Broadcast search; prints a ready-made YAML block (`--timeout`, default 5 s).                |
| `rename`   | Write the name the device carries itself (`--yes`/`-y` skips the confirmation).             |
| `learn-ir` | Learn an IR code, hex on stdout (`--timeout`, default 30 s).                                |
| `learn-rf` | Learn an RF code, hex on stdout (`--timeout` per phase, default 30 s).                      |
| `send`     | Send a named code or raw hex (`--repeat`/`-n`, `--delay`, default 0.5 s).                   |
| `climate`  | Send a computed Coolix state (`--mode`/`-m`, `--temp`/`-t` 17..30, `--fan`/`-f`).           |
| `special`  | Send a Coolix special command (`--repeat`/`-n`, `--delay`, default 0.8 s).                  |
| `codes`    | List the code library with its Coolix interpretation (optional filter, `--hex`).            |
| `decode`   | Analyse a named code or raw hex; no device needed.                                          |
| `selftest` | Check the Coolix generator against the learned reference codes; exit code 1 on failure.     |

### AC API

```bash
broadlink-ac-api                                              # 127.0.0.1:8099, scripts from ~/.local/bin
AC_API_SCRIPT_DIR=/opt/ac-scripts broadlink-ac-api --listen-host 0.0.0.0
python3 -m somestuff.broadlinkstuff.ac_api --log-level debug

curl -s localhost:8099/rooms | jq
curl -s localhost:8099/rooms/lounge/temp | jq
curl -sX POST localhost:8099/rooms/lounge/on | jq
```

| Route                    | Purpose                                                                 |
|--------------------------|-------------------------------------------------------------------------|
| `GET /healthz`           | Liveness and the number of discovered rooms (touches no hardware).      |
| `GET /rooms`             | Discovered rooms and the actions (`on`/`off`/`temp`) each one supports. |
| `POST /rooms/{room}/on`  | Run `activate_ac_<room>.sh`.                                            |
| `POST /rooms/{room}/off` | Run `deactivate_ac_<room>.sh`.                                          |
| `GET /rooms/{room}/temp` | Run `gettemp_ac_<room>.sh` and parse its JSON (`{"<device>": {...}}`).  |

A non-zero exit becomes `502` with the script's stderr, a timeout (`--timeout`,
default 90 s) `504`. Options are also read from `AC_API_LISTEN_HOST`,
`AC_API_LISTEN_PORT`, `AC_API_SCRIPT_DIR`, `AC_API_TIMEOUT` and
`AC_API_LOG_LEVEL`. There is no authentication: bind to localhost (the default)
or put it behind something that has any.

`climate` modes are `cool`, `dry`, `heat`, `fan_only`, `heat_cool`; fan speeds are
`auto`, `low`, `mid`, `high`.

Global options (before the subcommand), each with a `BROADLINK_*` env var:
`--verbose/-v`, `--quiet/-q`, `--probe/--no-probe`, `--timeout`,
`--no-rediscover`, `--dry-run`.

**Logs go to stderr, payload to stdout** — so `learn-ir`, `codes --hex`,
`sensors --json` and `discover` can be piped without log lines coming along.

`sensors` keys its JSON by device name whether or not a name was given, so a
script does not have to care: `sensors Lounge --json | jq .Lounge.temperature`.
Naming a device also keeps the traffic to that device — `devices` and a bare
`sensors` log in to every configured entry, which is what makes their listing
complete, but is wasted work when one reading is wanted.

### Two things worth knowing about `--probe`

`--probe` (the default) confirms each configured device with a unicast `hello()`
— about 0.02 s per device against about 5 s for a broadcast — and gets model,
name and `is_locked` from the device itself. `--no-probe` builds the object
purely locally with zero network traffic.

That matters for `rename`: `set_name()` transmits `is_locked` along with the
name, and a locally built object carries the default `False`. Renaming a
**locked** device from such an object would silently unlock it. `rename`
therefore always calls `update()` first.

## Configuration

The package brings its own config files; the sample `broadlink.yaml` ships
inside the package next to the module and holds only documented examples. Real
devices go into `broadlink.local.yaml`, which is merged over the shipped sample
at runtime.

`broadlink.local.yaml` is looked up in the **current working directory** first,
then in the package directory — first hit wins. Which file was actually read is
logged at DEBUG (`-v`). Values are validated by a pydantic model.

With no devices configured, every command falls back to a broadcast search.
`discover` prints a ready-made YAML block for exactly this file:

```bash
broadlinkhelper discover > broadlink.local.yaml
$EDITOR broadlink.local.yaml
```

```yaml
broadlink:
  devices:
    Lounge:
      devtype: 0x649B   # RM4 pro
      host: "192.0.2.173"
      port: 80
      mac: "020000ab0173"
```

| Field     | Meaning                                                                                                                                                                                                                                                                  |
|-----------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `devtype` | Hardware revision; selects the class (`0x648D` → rm4mini, `0x649B` → rm4pro) and goes into every packet. No constant exists for it and it cannot be guessed — take it from `discover`. A bare `0x649B` is parsed as an int by YAML; a quoted `"0x649B"` is accepted too. |
| `host`    | IP address. The only field that moves on its own (DHCP).                                                                                                                                                                                                                 |
| `port`    | Optional, defaults to 80 — which is what every RM4 uses.                                                                                                                                                                                                                 |
| `mac`     | Lower-case hex, no separators. Stable — used to re-find a device whose IP moved.                                                                                                                                                                                         |

The key (`Lounge`) is how you address the device on the command line; it is
independent of the name the device carries itself (see `rename`).

### Environment variables

Each global option can be set through the environment instead of the command line.

| Variable                  | Option            | Default | Meaning                                         |
|---------------------------|-------------------|---------|-------------------------------------------------|
| `BROADLINK_VERBOSE`       | `--verbose`       | off     | DEBUG logging.                                  |
| `BROADLINK_QUIET`         | `--quiet`         | off     | Warnings and errors only.                       |
| `BROADLINK_PROBE`         | `--probe`         | on      | Confirm devices by unicast (yields is_locked).  |
| `BROADLINK_TIMEOUT`       | `--timeout`       | 2       | Seconds per unicast attempt.                    |
| `BROADLINK_NO_REDISCOVER` | `--no-rediscover` | off     | Do not fall back to broadcast when unreachable. |
| `BROADLINK_DRY_RUN`       | `--dry-run`       | off     | Send nothing, only show what would be sent.     |

## Coolix (Midea) air conditioners

The learned climate codes are not an opaque blob but the **Coolix** protocol
(IRremoteESP8266 `decode_type_t::COOLIX`), which makes mode × fan × temperature
computable.

On the wire: header 4692 µs mark / 4416 µs space, then 48 bits pulse-distance
coded, MSB first — three data bytes each followed by its complement, so 24 bits
of payload. The packet is sent twice.

Two things a naive generator gets wrong, both handled here:

1. The temperature field is **Gray-coded**, not binary (17 °C → 0x0, 18 → 0x1,
   19 → 0x3, 20 → 0x2, …).
2. `dry` and `heat_cool` drive the fan themselves and carry a fixed fan nibble;
   `fan_only` shares its mode nibble with `dry` and is distinguished only by the
   temperature field reading `0xE`.

`special` sends the commands that are *not* a state — `off`, `swing`,
`swing-v-step`, `sleep`, `turbo`, `led`, `clean`, `swing-h`. The protocol has no
absolute vane position: `swing` is a toggle, `swing-v-step` moves one step
further (and is therefore sent as a **single** frame, otherwise every press
would move two positions).

`swing-h` is **unconfirmed** — in IRremoteESP8266 the constant is never sent nor
decoded, ESPHome has no horizontal command at all, and the library's own hex
comment contradicts its binary literal. Do not rely on it before seeing it work
on your own unit.

`selftest` checks the generator against the three learned reference codes in
`CODES` — at word level, not byte-wise, because the RM4 measures with its own
jitter while learning.

### Library

```python
from somestuff.broadlinkstuff.broadlinkhelper import CoolixFan, CoolixMode, coolix_code

code = coolix_code(CoolixMode.HEAT, 21, CoolixFan.LOW).hex()   # for HA, Node-RED, …
```

## What an RM4 cannot do

- **No subdevices.** Those exist only on `broadlink.hub.s3`. An RM blasts IR/RF
  into the room and does not know who is listening.
- **No stored commands.** The device has no command memory: `check_data()` reads
  one volatile buffer holding the most recently learned code. The named commands
  ("TV on") live in the Broadlink app and its cloud, not on the box — which is
  why `CODES` in this module exists.

## Notes

- Requires: `broadlink`, `typer`, `pydantic`, `ruamel.yaml`, `loguru`, `tabulate`.
- Source and repository-only material:
  https://github.com/vroomfondel/somestuff/tree/main/somestuff/broadlinkstuff/
- Licensed under the LGPL, see `LICENSE.md` in the repository root.
