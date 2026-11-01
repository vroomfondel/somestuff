# somestuff-netatmostuff

Netatmo weather station client and a publisher loop that forwards outdoor temperature, rain and pressure to MQTT.

```bash
pip install somestuff-netatmostuff
netatmo-crontanamo LOOP 300 10
python3 -m somestuff.netatmostuff.Crontanamo shootonce
```

Configuration comes from `somestuff-corestuff` (`config.yaml` + `config.local.yaml` in the working directory):
`netatmo.username/password/client_id/client_secret`, the module names/ids, `mqtt.*`, `mqtt_topics.netatmo.*`.
The refresh token is persisted in `~/.netatmo.credentials` (or `$STORAGEPATH`).

`lnetatmo.py` is a vendored copy of [philippelt/netatmo-api-python](https://github.com/philippelt/netatmo-api-python)
and is licensed under the GPLv3; this distribution is therefore GPLv3 as a whole.
