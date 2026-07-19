"""Reference mapper plugin: the oepnvstuff GTFS-realtime stream as a live board.

Consumes the topic layout published by :mod:`oepnvstuff.mqtt_bridge` (see its
module docstring) and arranges it into four panels: departure board, feed
status, per-line coverage and service alerts.

The topic tree's base defaults to ``oepnv`` and follows the publisher's
``OEPNV_MQTT_BASE_TOPIC`` environment variable (read at plugin load time), so
a non-default ``--mqtt-base-topic`` on the checker side only needs the same
env value on this side.

Run it locally against a broker with::

    python3 -m mqttwebstuff.serve --mapper mqttwebstuff/plugins/oepnv_view.py \
        --mqtt-host broker.example.org

In Kubernetes the file ships inside the image (``/app/mqttwebstuff/plugins/``);
a modified copy can simply be mounted over it (or anywhere else) via ConfigMap
and pointed at with ``MQTTWEB_MAPPER``.
"""

import datetime
import hashlib
import os
from typing import Any

from mqttwebstuff.plugin_api import ViewEvent

TITLE = "ÖPNV Live"

#: Root of the publisher's topic tree — deliberately the SAME environment
#: variable oepnvstuff's --mqtt-base-topic reads, so publisher and board can
#: share one env value. Multi-level bases ("home/oepnv") work too.
BASE_TOPIC = os.getenv("OEPNV_MQTT_BASE_TOPIC", "oepnv").strip("/")

SUBSCRIPTIONS = [f"{BASE_TOPIC}/#"]

_BASE_PARTS = BASE_TOPIC.split("/")

PANELS = {
    "departures": "Abfahrten",
    "status": "Feed-Status",
    "lines": "Linien",
    "alerts": "Meldungen",
}

#: The publisher polls every ~20 s and re-publishes every group each cycle;
#: 180 s tolerates a few missed cycles before a vanished group leaves the board.
_CYCLE_TTL = 180.0

#: Alerts are edge-triggered (published once, deduplicated upstream), so they
#: need to linger much longer than cycle-refreshed items.
_ALERT_TTL = 6 * 3600.0


def _departures_sort(payload: dict[str, Any]) -> str:
    """Sort a departure group by its earliest expected departure.

    Args:
        payload: The group payload from ``<base>/departures/<line>/<stop>/<direction>``.

    Returns:
        A lexicographically sortable key (ISO timestamps sort naturally).
    """
    departures = payload.get("departures") or []
    first = departures[0] if isinstance(departures, list) and departures else {}
    expected = first.get("expected") if isinstance(first, dict) else None
    return f"{expected or '9999'}|{payload.get('line', '')}"


def map_message(topic: str, payload: Any) -> ViewEvent | None:
    """Map one oepnv MQTT message onto the board (``None`` = drop).

    Args:
        topic: Full topic below the :data:`BASE_TOPIC` tree.
        payload: Decoded JSON payload.

    Returns:
        The board placement, or ``None`` for messages the board does not show
        (the aggregate departures document, unknown topics, non-dict payloads).
    """
    if not isinstance(payload, dict):
        return None
    parts = topic.split("/")
    if parts[: len(_BASE_PARTS)] != _BASE_PARTS:
        return None
    rel = parts[len(_BASE_PARTS) :]

    # <base>/departures/<line>/<stop>/<direction> — one card per group.
    if len(rel) == 4 and rel[0] == "departures":
        return ViewEvent(
            panel="departures",
            key="/".join(rel[1:]),
            data=payload,
            template="oepnv_departures.html.j2",
            sort=_departures_sort(payload),
            ttl=_CYCLE_TTL,
        )

    # <base>/departures — aggregate document; the group topics carry the same data.
    if rel == ["departures"]:
        return None

    # <base>/status — one summary card, fixed slot at the top of the panel.
    if rel == ["status"]:
        return ViewEvent(
            panel="status", key="summary", data=payload, template="oepnv_status.html.j2", sort="0", ttl=_CYCLE_TTL
        )

    # <base>/stale — edge-triggered; keep it visible a while below the summary.
    if rel == ["stale"]:
        return ViewEvent(panel="status", key="stale", data=payload, template="oepnv_stale.html.j2", sort="1", ttl=600.0)

    # <base>/lines/<line>/status — one card per line.
    if len(rel) == 3 and rel[0] == "lines" and rel[2] == "status":
        line = rel[1]
        return ViewEvent(
            panel="lines", key=line, data=payload, template="oepnv_line.html.j2", sort=f"{line:0>6}", ttl=_CYCLE_TTL
        )

    # <base>/alerts — content-addressed so a re-published alert replaces itself.
    if rel == ["alerts"]:
        digest = hashlib.sha1(f"{payload.get('entity')}|{payload.get('text')}".encode()).hexdigest()[:12]
        received = datetime.datetime.now().isoformat(timespec="seconds")
        return ViewEvent(
            panel="alerts",
            key=digest,
            data={**payload, "received": received},
            template="oepnv_alert.html.j2",
            sort=received,
            ttl=_ALERT_TTL,
        )

    return None
