# somestuff-ecowittstuff

Ecowitt weather station API client (Pydantic models for the `/device/real_time` response) and a publisher loop
that forwards the readings to MQTT.

```bash
pip install somestuff-ecowittstuff
ecowitt-realtime                       # one readout, logged
ecowitt-crontanamo LOOP 300 10         # poll every 300 s, publish to MQTT
python3 -m somestuff.ecowittstuff.Crontanamo LOOP 300 10
```

Configuration comes from `somestuff-corestuff` (`config.yaml` + `config.local.yaml` in the working directory):
`ecowitt.application_key`, `ecowitt.api_key`, `ecowitt.mac`, `mqtt.*`, `mqtt_topics.ecowitt.*`.
