# broadlinkstuff

Talk to **Broadlink RM4** IR/RF blasters from Python: address devices without a
broadcast, learn IR/RF codes, send stored or raw codes — and, for Midea/Coolix
air conditioners, **compute** the complete state matrix instead of learning all
280 combinations one by one in front of the device.

Everything lives in `broadlinkhelper.py`: the device fleet, the Coolix codec and
a Typer CLI. Logging setup is shared via `broadlinkstuff.configure_logging` /
`print_banner` (loguru, with a stdlib-`logging` intercept), mirroring the other
packages in this repo.

## Configuration

This package brings its own config files and does **not** touch the repo-wide
`config.yaml`: `broadlinkstuff/broadlink.yaml` holds the documented sample (and
is committed), the real devices go into `broadlink.local.yaml` (gitignored via
`*.local.*`), which is merged over it at runtime with `Helper.update_deep` —
the same base/local layering `config.yaml` / `config.local.yaml` uses.

```bash
python3 -m broadlinkstuff.broadlinkhelper discover > broadlink.local.yaml
$EDITOR broadlink.local.yaml
```

```yaml
broadlink:
  devices:
    Lounge:
      devtype: 0x649B   # RM4 pro
      host: "192.168.101.173"
      port: 80
      mac: "a043b0543a36"
```

| Field     | Meaning                                                                                                                                                                                                                                                                  |
|-----------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `devtype` | Hardware revision; selects the class (`0x648D` → rm4mini, `0x649B` → rm4pro) and goes into every packet. No constant exists for it and it cannot be guessed — take it from `discover`. A bare `0x649B` is parsed as an int by YAML; a quoted `"0x649B"` is accepted too. |
| `host`    | IP address. The only field that moves on its own (DHCP).                                                                                                                                                                                                                 |
| `port`    | Optional, defaults to 80 — which is what every RM4 uses.                                                                                                                                                                                                                 |
| `mac`     | Lower-case hex, no separators. Stable — used to re-find a device whose IP moved.                                                                                                                                                                                         |

The key (`Lounge`) is how you address the device on the command line; it is
independent of the name the device carries itself (see `rename`).

`broadlink.local.yaml` is looked up in the **current working directory** first,
then next to the module — first hit wins. Which file was actually read is logged
at DEBUG (`-v`). Values are validated by a pydantic model in
`broadlinkhelper.py`.

With no devices configured, every command falls back to a broadcast search.

## Usage

```bash
python3 -m broadlinkstuff.broadlinkhelper devices                    # what is reachable
python3 -m broadlinkstuff.broadlinkhelper discover                   # broadcast + config block
python3 -m broadlinkstuff.broadlinkhelper learn-ir Lounge            # press a button, hex comes out
python3 -m broadlinkstuff.broadlinkhelper send Lounge off            # code from CODES
python3 -m broadlinkstuff.broadlinkhelper send Lounge 2600ca00...    # raw hex
python3 -m broadlinkstuff.broadlinkhelper climate Lounge --temp 23   # Coolix computed
python3 -m broadlinkstuff.broadlinkhelper special Lounge swing       # swing on/off (toggle)
python3 -m broadlinkstuff.broadlinkhelper decode 2600ca00938e...     # no device, just maths
python3 -m broadlinkstuff.broadlinkhelper selftest
```

Global options (before the subcommand), each with a `BROADLINK_*` env var:
`--verbose/-v`, `--quiet/-q`, `--probe/--no-probe`, `--timeout`,
`--no-rediscover`, `--dry-run`.

**Logs go to stderr, payload to stdout** — so `learn-ir`, `codes --hex` and
`discover` can be piped without log lines coming along.

### Two things worth knowing about `--probe`

`--probe` (the default) confirms each configured device with a unicast `hello()`
— ~0.02 s per device against ~5 s for a broadcast — and gets model, name and
`is_locked` from the device itself. `--no-probe` builds the object purely
locally with zero network traffic.

That matters for `rename`: `set_name()` transmits `is_locked` along with the
name, and a locally built object carries the default `False`. Renaming a
**locked** device from such an object would silently unlock it. `rename`
therefore always calls `update()` first.

## Coolix (Midea) air conditioners

The learned climate codes are not an opaque blob but the **Coolix** protocol
(IRremoteESP8266 `decode_type_t::COOLIX`), which makes mode × fan × temperature
computable:

```python
from broadlinkstuff.broadlinkhelper import CoolixFan, CoolixMode, coolix_code

code = coolix_code(CoolixMode.HEAT, 21, CoolixFan.LOW).hex()   # for HA, Node-RED, …
```

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

## What an RM4 cannot do

- **No subdevices.** Those exist only on `broadlink.hub.s3`. An RM blasts IR/RF
  into the room and does not know who is listening.
- **No stored commands.** The device has no command memory: `check_data()` reads
  one volatile buffer holding the most recently learned code. The named commands
  ("TV on") live in the Broadlink app and its cloud, not on the box — which is
  why `CODES` in this module exists.
