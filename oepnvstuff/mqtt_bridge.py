"""Bridge GTFS-realtime monitoring results onto MQTT.

Keeps feed handling (:mod:`oepnvstuff.gtfs_realtime` /
:mod:`oepnvstuff.monitor`) separate from messaging (:mod:`mqttstuff`): this is
the *only* module that imports ``mqttstuff``, so the core stays
dependency-light and usable without a broker.

The bridge's methods are plain :class:`~oepnvstuff.monitor.RealtimeMonitor`
handlers — register them via :func:`attach_bridge` (or individually with
``add_cycle_handler`` & co.).

Topic layout (``base_topic`` defaults to ``oepnv``)::

    oepnv/status                                 per-cycle summary (feed ts/age, stale flag, per-line counts)
    oepnv/lines/<line>/status                    one line's status (updates, delay stats, static trips)
    oepnv/departures                             all upcoming departures in one document (every cycle,
                                                 empty list when the board is empty)
    oepnv/departures/<line>/<stop>/<direction>   one group's departures (ASCII-simplified segments,
                                                 e.g. departures/1/S_Blankenese/U_Kellinghusenstrasse)
    oepnv/alerts                                 each new (deduplicated) service alert
    oepnv/stale                                  fired once on the transition into staleness

Everything is published with ``retain=False``: subscribers see the live stream
only, the broker never keeps a last value. Late subscribers therefore wait at
most one poll interval for the next cycle's status/departures instead of
possibly acting on an outdated retained board.

Serialization note: mqttstuff's ``publish_one`` only wraps ``dict`` values (via
``json.dumps``); this bridge therefore always publishes a ``dict``, never a
bare list.

TLS: passed natively to ``mqttstuff`` (>= 0.0.6) — server auth via CA
(``None`` = system CA store), optional mutual TLS (client cert + key), optional
``tls_insecure`` for self-signed certificates. The wrapper validates the
option combination and raises ``ValueError`` on inconsistencies.
"""

import datetime
import logging
import re
import unicodedata

from mqttstuff import MosquittoClientWrapper

from oepnvstuff.gtfs_realtime import ServiceAlert
from oepnvstuff.monitor import CycleResult, LineStatus, NextDeparture, RealtimeMonitor

logger = logging.getLogger(__name__)

#: MQTT topic-level separator and single-/multi-level wildcards — illegal inside
#: a topic segment, so any of these in a line name would split or break the topic.
_MQTT_RESERVED = str.maketrans({"/": "_", "+": "_", "#": "_"})

#: German umlauts/eszett → ASCII digraphs, applied before the generic
#: accent-stripping so "Straße" becomes "Strasse", not "Strae".
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


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


def _ascii_safe(name: str) -> str:
    """Turn a display name into an ASCII-simplified MQTT topic segment.

    Transliterates German umlauts/eszett (``Straße`` → ``Strasse``), strips all
    remaining accents/non-ASCII, and collapses every other character that is
    not ``[A-Za-z0-9._-]`` (spaces, commas, MQTT wildcards, …) into ``_``.
    Used for the ``departures/<line>/<stop>/<direction>`` sub-topics so
    subscribers can address groups without quoting/encoding issues.

    Args:
        name: The raw display name (line, stop name or headsign).

    Returns:
        A single ASCII topic segment, never empty (falls back to ``unknown``).
    """
    text = name.translate(_UMLAUTS)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return text or "unknown"


def _line_payload(status: LineStatus) -> dict[str, object]:
    """Build the JSON payload for one line's status.

    Args:
        status: The line status to serialize.

    Returns:
        Flat dict with update count, realtime recency, delay statistics and
        static trip count.
    """
    return {
        "line": status.line,
        "has_realtime": status.has_realtime,
        "realtime_recent": status.realtime_recent,
        "seconds_since_realtime": status.seconds_since_realtime,
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
        tls: bool = False,
        tls_ca: str | None = None,
        tls_cert: str | None = None,
        tls_key: str | None = None,
        tls_insecure: bool = False,
    ) -> None:
        """Initialize the bridge and the underlying MQTT client wrapper.

        Does not connect yet — call :meth:`start` to connect and begin the
        network loop.

        Args:
            host: MQTT broker host (e.g. ``mosquitto.mosquitto.svc.cluster.local``).
            port: Broker port. Defaults to ``1883``; TLS brokers usually listen
                on ``8883`` — set that explicitly, it is NOT switched
                automatically.
            username: Broker username, or ``None`` for anonymous.
            password: Broker password, or ``None``.
            base_topic: Root of the topic tree. Defaults to ``"oepnv"``.
            tls: Encrypt the connection with TLS.
            tls_ca: Path to the CA certificate (PEM) that signed the broker's
                certificate; ``None`` uses the system CA store (sufficient for
                e.g. Let's-Encrypt brokers).
            tls_cert: Path to a client certificate (PEM) for mutual TLS, or
                ``None``.
            tls_key: Path to the client certificate's private key, or ``None``.
            tls_insecure: Skip hostname verification (self-signed certificates
                whose CN/SAN does not match the host). The connection is still
                encrypted, but vulnerable to MITM — last resort only.

        Raises:
            ValueError: TLS options are inconsistent — ``tls_cert`` without
                ``tls_key`` (or vice versa), or ``tls_*`` values given while
                ``tls`` is off (validated by ``mqttstuff``); unreadable
                certificate/key files propagate from ``ssl``.
        """
        self._base = base_topic.strip("/")
        self._host = host
        self._port = port
        # TLS is native in mqttstuff >= 0.0.6 (ca None -> system CA store).
        self._mq = MosquittoClientWrapper(
            host=host,
            port=port,
            username=username,
            password=password,
            tls=tls,
            tls_ca_certs=tls_ca,
            tls_certfile=tls_cert,
            tls_keyfile=tls_key,
            tls_insecure=tls_insecure,
        )
        if tls and tls_insecure:
            logger.warning("MQTT TLS hostname verification DISABLED (tls_insecure) — encrypted but MITM-able")

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
        ``<base>/lines/<line>/status``, all non-retained.
        Never raises — a broker hiccup must not kill the poll loop.

        Args:
            cycle: The completed cycle delivered by the monitor.
        """
        snapshot = cycle.snapshot
        summary: dict[str, object] = {
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
        self._safe_publish(f"{self._base}/status", summary, retain=False)
        for status in cycle.per_line.values():
            self._safe_publish(
                f"{self._base}/lines/{_mqtt_safe(status.line)}/status", _line_payload(status), retain=False
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
        so this fires once per staleness episode (the per-cycle ``<base>/status``
        keeps carrying the ``stale`` flag).

        Args:
            cycle: The first stale cycle.
        """
        payload: dict[str, object] = {
            "time": cycle.wall_time.isoformat(timespec="seconds"),
            "feed_age_s": cycle.feed_age,
            "unchanged_cycles": cycle.unchanged_cycles,
        }
        self._safe_publish(f"{self._base}/stale", payload, retain=False)

    @staticmethod
    def _departure_entry(nd: NextDeparture, now: datetime.datetime) -> dict[str, object]:
        """Serialize one departure for the JSON payloads.

        Args:
            nd: The departure to serialize.
            now: The cycle's wall time, reference for ``in_minutes``.

        Returns:
            Flat dict with times, minutes-until, delay and realtime flag.
        """
        return {
            "scheduled": nd.scheduled.isoformat(timespec="seconds"),
            "expected": nd.expected.isoformat(timespec="seconds"),
            "in_minutes": round(nd.minutes_until(now), 1),
            "delay_s": nd.delay_seconds,
            "realtime": nd.delay_seconds is not None,
            "stop_id": nd.stop_id,
        }

    def publish_departures(self, cycle: CycleResult) -> None:
        """CycleHandler: publish the cycle's upcoming departures (non-retained).

        Register with :meth:`RealtimeMonitor.add_cycle_handler`. Publishes:

        * ``<base>/departures`` — one document per cycle with every upcoming
          departure (next N per line and direction, as configured on the
          monitor); an empty ``departures`` list when the board is empty, so
          subscribers can tell "no departures" from "no data".
        * ``<base>/departures/<line>/<stop>/<direction>`` — one sub-topic per
          group with just that group's departures. Segments are
          ASCII-simplified (see :func:`_ascii_safe`); same-named platforms
          (Steig A/B) merge into one ``<stop>`` level, the per-departure
          ``stop_id`` stays in the payload.

        Nothing is retained, so groups that vanish from one cycle to the next
        simply stop being published. When the monitor runs without
        ``compute_departures`` nothing is published and no topic ever appears.

        Args:
            cycle: The completed cycle delivered by the monitor.
        """
        payload: dict[str, object] = {
            "time": cycle.wall_time.isoformat(timespec="seconds"),
            "departures": [
                {
                    "line": nd.line,
                    "direction": nd.direction,
                    "stop": nd.stop_name,
                    **self._departure_entry(nd, cycle.wall_time),
                }
                for nd in cycle.next_departures
            ],
        }
        self._safe_publish(f"{self._base}/departures", payload, retain=False)

        groups: dict[tuple[str, str, str], list[NextDeparture]] = {}
        for nd in cycle.next_departures:
            groups.setdefault((nd.line, nd.stop_name, nd.direction), []).append(nd)
        for (line, stop, direction), nds in groups.items():
            topic = f"{self._base}/departures/{_ascii_safe(line)}/{_ascii_safe(stop)}/{_ascii_safe(direction)}"
            group_payload: dict[str, object] = {
                "time": cycle.wall_time.isoformat(timespec="seconds"),
                "line": line,
                "stop": stop,
                "direction": direction,
                "departures": [self._departure_entry(nd, cycle.wall_time) for nd in nds],
            }
            self._safe_publish(topic, group_payload, retain=False)

    def _safe_publish(self, topic: str, payload: dict[str, object], *, retain: bool) -> None:
        """Publish ``payload`` (always a dict → JSON), swallowing broker errors.

        Args:
            topic: Full MQTT topic to publish to.
            payload: The JSON-serializable payload (flat values or one level of
                nested lists/dicts); ``mqttstuff.publish_one`` wraps it in
                ``json.dumps``.
            retain: Whether the broker should keep the message as last value
                for the topic (this bridge always publishes ``False`` — see
                module docstring).
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
    :meth:`GtfsMqttBridge.publish_departures` (a no-op unless the monitor
    computes departures), :meth:`GtfsMqttBridge.publish_alert` and
    :meth:`GtfsMqttBridge.publish_stale` with the corresponding monitor
    handler slots.

    Args:
        monitor: The monitor whose results should be published.
        bridge: The bridge to attach.
    """
    monitor.add_cycle_handler(bridge.publish_cycle)
    monitor.add_cycle_handler(bridge.publish_departures)
    monitor.add_alert_handler(bridge.publish_alert)
    monitor.add_stale_handler(bridge.publish_stale)
