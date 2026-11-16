# hassstuff

Home Assistant over REST **and** WebSocket: thin typed clients (`somestuff.hassstuff.client`) and a CLI
(`hass-api`, or `python -m somestuff.hassstuff`). The WebSocket API covers what REST does not expose:
entity/device/area registry, helpers, Lovelace dashboards, traces.

```bash
hass-api --help
hass-api <command> --help
```

## Connection

| Option         | Env               | Default                                                                               |
|----------------|-------------------|---------------------------------------------------------------------------------------|
| `--url`        | `HASS_URL`        | content of `homeassistant_url.local.txt`, otherwise `http://homeassistant.local:8123` |
| `--token`      | `HASS_TOKEN`      | –                                                                                     |
| `--token-file` | `HASS_TOKEN_FILE` | `homeassistant_token.local.txt`                                                       |
| `--insecure`   | –                 | TLS is verified                                                                       |
| `--timeout`    | –                 | `30` seconds                                                                          |

Both `*.local.txt` files are searched in the current directory and every parent directory, each also under
`secrets.local/`. `--token` wins over `--token-file`. Global options go **before** the command:
`hass-api --url https://… --token abc states`.

## Commands

| Command                                                 | API       | Purpose                                                    |
|---------------------------------------------------------|-----------|------------------------------------------------------------|
| `info`                                                  | REST + WS | Version, user, admin flag                                  |
| `states [PATTERN] [--domain D] [--json]`                | REST      | States, pattern = substring or glob on ID/name             |
| `state ENTITY [--yaml]`                                 | REST      | Full state object                                          |
| `call DOMAIN.SERVICE [k=v …] [--data JSON]`             | REST      | Service call, `--return-response` for responses            |
| `template 'JINJA'`                                      | REST      | Render a template server-side, `-` = stdin                 |
| `history ENTITY … [--hours N] [--attributes]`           | REST      | State history                                              |
| `logbook [ENTITY] [--hours N]`                          | REST      | Logbook including trigger (user, service, automation)      |
| `areas` / `devices [PATTERN]`                           | WS        | Areas, devices with area and model                         |
| `entities [PATTERN] [--area A] [--platform P]`          | WS        | Entity registry, area also inherited from the device       |
| `entity-update ENTITY [--name …] [--area …] …`          | WS        | Name, icon, area, ID, enable/hide, labels                  |
| `helper list\|create\|update\|delete TYPE …`            | WS        | `input_*`, `counter`, `timer`, `schedule`                  |
| `automation list\|get\|put\|delete`                     | REST      | UI automations, `put` shows a diff first                   |
| `script list\|get\|put\|delete`                         | REST      | UI scripts, as above                                       |
| `trace list [REF]` / `trace get REF [RUN_ID]`           | REST + WS | Run traces, without `RUN_ID` the latest run                |
| `dashboard list\|get [URL_PATH]\|save FILE [URL_PATH]`  | WS        | Lovelace dashboards in storage mode                        |
| `ws TYPE [k=v …] [--data JSON]`                         | WS        | Any WebSocket command                                      |

`k=v` values are parsed as JSON where possible (`max=300`, `options='["a","b"]'`), otherwise as text.

`REF` for automations/scripts: entity ID (`automation.foo`) **or** config ID. The config ID of an automation
is its `id` attribute, not the part after `automation.`; `automation list` shows both.

## Examples

```bash
hass-api entities --area Kitchen
hass-api logbook light.kitchen --hours 2          # who switched it?
hass-api trace get automation.morning_lights --yaml
hass-api automation get morning_lights --yaml > /tmp/a.yaml
hass-api automation put morning_lights /tmp/a.yaml --dry-run
hass-api helper create input_number name="Target" min=0 max=300 step=5 unit_of_measurement=°C
hass-api entity-update sensor.foo --area Kitchen --name "Kitchen temperature"
hass-api call logger.set_level homeassistant.components.mqtt=debug
hass-api dashboard get my-dashboard --yaml > /tmp/d.yaml
hass-api dashboard save /tmp/d.yaml my-dashboard --dry-run
hass-api ws config/label_registry/list
```

## Library

```python
from somestuff.hassstuff.client import HassConfig, HassRest, HassWebSocket, default_token_file, default_url, load_token

config = HassConfig(url=default_url(), token=load_token(None, default_token_file()))
```

## Limits

- `put`/`save` replace the whole object. First `get`, then edit, then `put` (without `--dry-run`).
- `delete` asks for confirmation, `--yes` skips it.
- Helpers created through config flows (template, utility meter, threshold, group …) do not go through
  `helper`, only through `config_entries/flow` (possible via `ws`, but multi-step).
- YAML dashboards and hand-written automations in `automations.yaml` without `id` are not writable.
