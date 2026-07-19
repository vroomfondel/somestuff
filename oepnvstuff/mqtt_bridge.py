"""Bridge GTFS-realtime monitoring results onto MQTT.

Keeps feed handling (:mod:`oepnvstuff.gtfs_realtime` /
:mod:`oepnvstuff.monitor`) separate from messaging (:mod:`mqttstuff`): this is
the *only* module that imports ``mqttstuff``, so the core stays
dependency-light and usable without a broker.

The bridge's methods are plain :class:`~oepnvstuff.monitor.RealtimeMonitor`
handlers — register them via :func:`attach_bridge` (or individually with
``add_cycle_handler`` & co.).

Topic layout (``base_topic`` defaults to ``oepnv``)::

    oepnv/status                  retained   per-cycle summary (feed ts/age, stale flag, per-line counts)
    oepnv/lines/<line>/status     retained   one line's status (updates, delay stats, static trips)
    oepnv/alerts                  transient  each new (deduplicated) service alert
    oepnv/stale                   transient  fired once on the transition into staleness

Status topics are retained: they are genuine "last known value" state, so a
late subscriber (e.g. a dashboard pod restarting) immediately sees the current
situation. Alerts and stale transitions are point-in-time events and therefore
published with ``retain=False``.

Serialization note: mqttstuff's ``publish_one`` only wraps ``dict`` values (via
``json.dumps``); this bridge therefore always publishes a ``dict``, never a
bare list.
"""

import logging

from mqttstuff import MosquittoClientWrapper

from oepnvstuff.gtfs_realtime import ServiceAlert
from oepnvstuff.monitor import CycleResult, LineStatus, RealtimeMonitor

logger = logging.getLogger(__name__)

#: MQTT topic-level separator and single-/multi-level wildcards — illegal inside
#: a topic segment, so any of these in a line name would split or break the topic.
_MQTT_RESERVED = str.maketrans({"/": "_", "+": "_", "#": "_"})

#: JSON-compatible payload values this bridge produces (no nesting beyond one
#: dict/list level is needed for its flat status payloads).
_JsonValue = str | int | float | bool | None


def _mqtt_safe(name: str) -> str:
    """Turn a line name into a single, publishable MQTT topic segment.

    Replaces the reserved characters ``/ + #`` with ``_`` and trims surrounding
    whitespace. An empty or whitespace-only name collapses to ``"unknown"`` so
    the topic never contains an empty level.

    Args:
        name: The raw line name (``route_short_name``).

    Returns:
        A single MQTT topic segment, never empty.
    """
    safe = name.strip().translate(_MQTT_RESERVED)
    return safe or "unknown"


def _line_payload(status: LineStatus) -> dict[str, _JsonValue]:
    """Build the JSON payload for one line's status.

    Args:
        status: The line status to serialize.

    Returns:
        Flat dict with update count, delay statistics and static trip count.
    """
    return {
        "line": status.line,
        "has_realtime": status.has_realtime,
        "updates": status.updates,
        "delay_values": len(status.delays),
        "delay_min_s": min(status.delays) if status.delays else None,
        "delay_max_s": max(status.delays) if status.delays else None,
        "delay_avg_s": round(status.avg_delay, 1) if status.avg_delay is not None else None,
        "static_trips": status.static_trips,
    }


class GtfsMqttBridge:
    """Publish per-cycle line statuses, alerts and stale transitions to MQTT.

    See the module docstring for the topic layout and retain semantics.
    """

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        *,
        base_topic: str = "oepnv",
    ) -> None:
        """Initialize the bridge and the underlying MQTT client wrapper.

        Does not connect yet — call :meth:`start` to connect and begin the
        network loop.

        Args:
            host: MQTT broker host (e.g. ``mosquitto.mosquitto.svc.cluster.local``).
            port: Broker port. Defaults to ``1883``.
            username: Broker username, or ``None`` for anonymous.
            password: Broker password, or ``None``.
            base_topic: Root of the topic tree. Defaults to ``"oepnv"``.
        """
        self._base = base_topic.strip("/")
        self._host = host
        self._port = port
        self._mq = MosquittoClientWrapper(host=host, port=port, username=username, password=password)

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Connect to the broker and start the network loop (paho thread).

        A failed initial connect is logged, not raised — monitoring must not die
        because the broker was briefly unavailable. A connect that merely times
        out leaves paho retrying in the background; a connect that *raises*
        (e.g. ``ConnectionRefusedError`` on a closed port) leaves the bridge
        unconnected, which each later publish attempt will log as a warning.
        """
        logger.info(f"MQTT bridge connecting to host={self._host} port={self._port} base={self._base}")
        try:
            connected = self._mq.wait_for_connect_and_start_loop()
        except Exception as exc:
            logger.warning(
                f"MQTT bridge connect to host={self._host} port={self._port} failed: "
                f"{type(exc).__name__}: {exc} — continuing without a broker connection"
            )
            return
        if connected:
            logger.info(f"MQTT bridge successfully connected to host={self._host} port={self._port}")
        else:
            logger.warning(
                f"MQTT bridge NOT connected to host={self._host} port={self._port} within timeout"
                " — paho keeps retrying in background"
            )

    def stop(self) -> None:
        """Disconnect from the broker."""
        try:
            self._mq.disconnect()
        except Exception:
            logger.exception("MQTT disconnect failed")

    # ── handlers (register with RealtimeMonitor) ────────────────────────────

    def publish_cycle(self, cycle: CycleResult) -> None:
        """CycleHandler: publish the cycle summary and one status per line.

        Register with :meth:`RealtimeMonitor.add_cycle_handler`. Publishes the
        overall summary to ``<base>/status`` and each line's status to
        ``<base>/lines/<line>/status``, all retained (last-value state).
        Never raises — a broker hiccup must not kill the poll loop.

        Args:
            cycle: The completed cycle delivered by the monitor.
        """
        snapshot = cycle.snapshot
        summary: dict[str, _JsonValue] = {
            "time": cycle.wall_time.isoformat(timespec="seconds"),
            "feed_timestamp": snapshot.feed_timestamp if snapshot else None,
            "feed_age_s": cycle.feed_age,
            "stale": cycle.stale,
            "unchanged_cycles": cycle.unchanged_cycles,
            "any_realtime": cycle.any_realtime,
            "total_trip_updates": snapshot.total_trip_updates if snapshot else 0,
            "lines_with_realtime": sum(1 for s in cycle.per_line.values() if s.has_realtime),
            "lines_total": len(cycle.per_line),
        }
        self._safe_publish(f"{self._base}/status", summary, retain=True)
        for status in cycle.per_line.values():
            self._safe_publish(
                f"{self._base}/lines/{_mqtt_safe(status.line)}/status", _line_payload(status), retain=True
            )

    def publish_alert(self, alert: ServiceAlert) -> None:
        """AlertHandler: publish one new service alert to ``<base>/alerts``.

        Register with :meth:`RealtimeMonitor.add_alert_handler` — the monitor
        deduplicates, so each alert is published once.

        Args:
            alert: The newly seen service alert.
        """
        self._safe_publish(f"{self._base}/alerts", {"entity": alert.entity, "text": alert.text}, retain=False)

    def publish_stale(self, cycle: CycleResult) -> None:
        """StaleHandler: publish the transition into staleness to ``<base>/stale``.

        Register with :meth:`RealtimeMonitor.add_stale_handler` — edge-triggered,
        so this fires once per staleness episode (the retained ``<base>/status``
        keeps carrying the ``stale`` level).

        Args:
            cycle: The first stale cycle.
        """
        payload: dict[str, _JsonValue] = {
            "time": cycle.wall_time.isoformat(timespec="seconds"),
            "feed_age_s": cycle.feed_age,
            "unchanged_cycles": cycle.unchanged_cycles,
        }
        self._safe_publish(f"{self._base}/stale", payload, retain=False)

    def _safe_publish(self, topic: str, payload: dict[str, _JsonValue], *, retain: bool) -> None:
        """Publish ``payload`` (always a dict → JSON), swallowing broker errors.

        Args:
            topic: Full MQTT topic to publish to.
            payload: The JSON-serializable payload; ``mqttstuff.publish_one``
                wraps it in ``json.dumps``.
            retain: Whether the broker should keep the message as last value
                for the topic (state yes, events no — see module docstring).
        """
        try:
            ok = self._mq.publish_one(topic, payload, retain=retain)
            if not ok:
                logger.warning(f"MQTT publish to {topic} not confirmed")
        except Exception:
            logger.exception(f"MQTT publish to {topic} failed")


def attach_bridge(monitor: RealtimeMonitor, bridge: GtfsMqttBridge) -> None:
    """Convenience wiring: hook a :class:`GtfsMqttBridge` onto a monitor.

    Registers :meth:`GtfsMqttBridge.publish_cycle`,
    :meth:`GtfsMqttBridge.publish_alert` and :meth:`GtfsMqttBridge.publish_stale`
    with the corresponding monitor handler slots.

    Args:
        monitor: The monitor whose results should be published.
        bridge: The bridge to attach.
    """
    monitor.add_cycle_handler(bridge.publish_cycle)
    monitor.add_alert_handler(bridge.publish_alert)
    monitor.add_stale_handler(bridge.publish_stale)
