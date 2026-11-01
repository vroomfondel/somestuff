# somestuff-hydromailstuff

Assembles a daily status email from the last MQTT water-level readings and Netatmo rain/temperature data,
renders it with a Jinja2 template and sends it via SMTP.

```bash
pip install somestuff-hydromailstuff
hydromail
python3 -m somestuff.hydromailstuff.hydromail
```

Configuration comes from `somestuff-corestuff` (`config.yaml` + `config.local.yaml` in the working directory):
`hydromail.*` (SMTP ip, sender, recipients), `mqtt.*`, `mqtt_topics.wasserstandsmesser.*`, `netatmo.*`.
`DISABLE_MAIL_SEND=True` renders without sending; outside a Kubernetes pod sending is disabled anyway.
Depends on `somestuff-netatmostuff` (GPLv3), so this distribution is GPLv3 too.
