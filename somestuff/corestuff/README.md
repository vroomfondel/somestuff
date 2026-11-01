# somestuff-corestuff

Shared settings loader for the `somestuff.*` packages that read the repo-wide `config.yaml`
(`ecowittstuff`, `netatmostuff`, `hydromailstuff`, `llmstuff`).

```python
from somestuff.corestuff.config import get_settings, get_timezone, settings

get_settings().mqtt.host      # explicit
settings.mqtt.host            # lazy proxy, same object
```

Nothing is read at import time. The first access loads, in this order of precedence:

| Source                      | Where it is looked for                                                                       |
|-----------------------------|----------------------------------------------------------------------------------------------|
| environment variables       | nested with `__`, e.g. `MQTT__HOST=broker`                                                   |
| `config.local.yaml`         | `SOMESTUFF_CONFIG_LOCAL_PATH`, else `SOMESTUFF_CONFIG_DIR_PATH`, else the CWD, else `/app`   |
| `config.yaml`               | `SOMESTUFF_CONFIG_PATH`, else `SOMESTUFF_CONFIG_DIR_PATH`, else the CWD, else `/app`, else the packaged sample |

`config.local.yaml` carries the secrets and is never part of a distribution; keep it in the directory you start the tool from.
`python3 -m somestuff.corestuff.config` (or `somestuff-config-dump`) prints the effective settings.

`somestuff.corestuff.configure_logging()` installs the shared loguru format and routes stdlib `logging` into it;
`somestuff.corestuff.helper` has the JSON encoder (`ComplexEncoder`) and `update_deep()`.
