#!/usr/bin/env python3
"""Bridge UCM6204 WebSocket events onto MQTT — and optionally act on MQTT commands.

Keeps transport/parsing (:mod:`ucmstuff.ucm6204_api`) separate from messaging
(:mod:`mqttstuff`): this is the *only* module that imports ``mqttstuff``, so the
core client stays dependency-light and usable without a broker.

Two directions, both optional and independently usable:

* **Outbound (monitor → MQTT).** Register :meth:`MqttEventBridge.publish_event`
  as a :data:`~ucmstuff.ucm6204_api.EventHandler` (every raw event) and/or
  :meth:`publish_incoming` as a :data:`~ucmstuff.ucm6204_api.CallHandler`
  (parsed incoming calls on routed trunks) with the :class:`UCMEventClient`.
* **Inbound (MQTT → control).** With ``enable_commands=True`` and an
  :class:`~ucmstuff.ucm6204_api.UCM6204` control client, subscribe to
  ``<base>/cmd/#`` and dispatch ``accept`` / ``refuse`` / ``hangup`` to the API.

Topic layout (``base_topic`` defaults to ``ucm6204``)::

    ucm6204/events/<eventname>       retained   full notify event as JSON
    ucm6204/calls/<trunk>/incoming   transient  parsed IncomingCall as JSON
    ucm6204/cmd/{accept,refuse,hangup}   inbound   {"channel": "PJSIP/..."}

Incoming calls are published per inbound trunk (``<trunk>`` is the call's
``inbound_trunk_name``, MQTT-sanitized); subscribe to ``ucm6204/calls/+/incoming``
for all trunks or ``ucm6204/calls/<trunk>/incoming`` for one.

Serialization note: mqttstuff's ``publish_one`` only wraps ``dict`` values (via
``json.dumps``); a bare ``list`` — e.g. an ``ActiveCallStatus`` ``eventbody`` — is
passed straight to paho and raises ``TypeError``. This bridge therefore always
publishes a ``dict`` (the whole event, or a hand-built payload), never a naked
list.
"""

import logging
from dataclasses import asdict
from typing import Any, Iterable

from mqttstuff import MWMqttMessage, MosquittoClientWrapper

from ucmstuff.ucm6204_api import IncomingCall, NotifyEvent, UCM6204, UCMAPIError

logger = logging.getLogger(__name__)

#: MQTT topic-level separator and single-/multi-level wildcards — illegal inside a
#: topic segment, so any of these in a trunk name would split or break the topic.
_MQTT_RESERVED = str.maketrans({"/": "_", "+": "_", "#": "_"})


def _mqtt_safe(name: str) -> str:
    """Turn a trunk name into a single, publishable MQTT topic segment.

    Replaces the reserved characters ``/ + #`` with ``_`` and trims surrounding
    whitespace. An empty or whitespace-only name (e.g. a call with no
    ``inbound_trunk_name``) collapses to ``"unknown"`` so the topic never contains
    an empty level.
    """
    safe = name.strip().translate(_MQTT_RESERVED)
    return safe or "unknown"


class MqttEventBridge:
    """Publish UCM events to MQTT and optionally dispatch MQTT commands to the API.

    Args:
        host: MQTT broker host (e.g. ``mosquitto.mosquitto.svc.cluster.local``).
        port: Broker port. Defaults to ``1883``.
        username: Broker username, or ``None`` for anonymous.
        password: Broker password, or ``None``.
        base_topic: Root of the topic tree. Defaults to ``"ucm6204"``.
        api: A connected :class:`UCM6204` control client. Required only when the
            inbound command path is enabled (:meth:`start` with
            ``enable_commands=True``); ``None`` disables control.
        retain_events: Publish ``<base>/events/*`` with the MQTT retain flag, so a
            late subscriber immediately sees the last state per event type.
            Defaults to ``True``.
    """

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        *,
        base_topic: str = "ucm6204",
        api: UCM6204 | None = None,
        retain_events: bool = True,
    ) -> None:
        self._base = base_topic.strip("/")
        self._api = api
        self._retain_events = retain_events
        self._mq = MosquittoClientWrapper(host=host, port=port, username=username, password=password)

    # ── Outbound: UCM → MQTT ────────────────────────────────────────────────

    def publish_event(self, event: NotifyEvent) -> None:
        """EventHandler: publish one raw notify event to ``<base>/events/<eventname>``.

        Register with :meth:`UCMEventClient.add_event_handler`. Publishes the whole
        event dict (``eventname`` + ``eventbody`` + ``transactionid``) as JSON.
        Never raises — a broker hiccup must not kill the WebSocket receive loop.
        """
        name = str(event.get("eventname") or "unknown")
        self._safe_publish(f"{self._base}/events/{name}", dict(event), retain=self._retain_events)

    def publish_incoming(self, call: IncomingCall) -> None:
        """CallHandler: publish a parsed incoming call to ``<base>/calls/<trunk>/incoming``.

        Register as the ``on_call`` of a :class:`TrunkCallRouter` (or call it from
        your own router). The inbound trunk becomes a topic segment (MQTT-sanitized,
        see :func:`_mqtt_safe`), so subscribers can filter per trunk. Drops the bulky
        ``raw`` leg dict from the payload.
        """
        payload = {k: v for k, v in asdict(call).items() if k != "raw"}
        trunk = _mqtt_safe(call.trunk)
        self._safe_publish(f"{self._base}/calls/{trunk}/incoming", payload, retain=False)

    def _safe_publish(self, topic: str, payload: dict[str, Any], *, retain: bool) -> None:
        """Publish ``payload`` (always a dict → JSON) swallowing broker errors."""
        try:
            ok = self._mq.publish_one(topic, payload, retain=retain)
            if not ok:
                logger.warning("MQTT publish to %s not confirmed", topic)
        except Exception:
            logger.exception("MQTT publish to %s failed", topic)

    # ── Inbound: MQTT → UCM control ─────────────────────────────────────────

    def start(self, enable_commands: bool = False) -> None:
        """Connect to the broker and start the network loop (blocking-in-thread).

        Args:
            enable_commands: Subscribe to ``<base>/cmd/#`` and dispatch commands to
                the API control client. Requires ``api=`` to have been supplied.

        Raises:
            ValueError: ``enable_commands=True`` without an ``api`` control client.
        """
        if enable_commands:
            if self._api is None:
                raise ValueError("enable_commands=True requires api=UCM6204(...)")
            cmd_topic = f"{self._base}/cmd/#"
            # set_topics() records the filter in the client's userdata, so on_connect
            # issues the actual SUBSCRIBE — on the first connect *and* on every paho
            # auto-reconnect. add_message_callback() alone only registers client-side
            # routing and would never make the broker deliver anything.
            self._mq.set_topics([cmd_topic])
            self._mq.add_message_callback(cmd_topic, self._on_command, rettype="json")
            logger.info("MQTT command path enabled on %s", cmd_topic)
        self._mq.wait_for_connect_and_start_loop()

    def stop(self) -> None:
        """Disconnect from the broker."""
        try:
            self._mq.disconnect()
        except Exception:
            logger.exception("MQTT disconnect failed")

    #: Command topic suffix → (UCM6204 method name, human label).
    _COMMANDS: dict[str, str] = {"accept": "accept_call", "refuse": "refuse_call", "hangup": "hangup"}

    def _on_command(self, msg: MWMqttMessage, userdata: Any) -> None:
        """Dispatch one ``<base>/cmd/<action>`` message to the API control client.

        Payload is JSON with at least ``{"channel": "PJSIP/..."}``. Unknown actions,
        missing channels and API errors are logged, never raised (this runs in the
        paho network thread).
        """
        action = str(msg.topic).rsplit("/", 1)[-1]
        method = self._COMMANDS.get(action)
        if method is None:
            logger.warning("ignoring MQTT command with unknown action %r (topic=%s)", action, msg.topic)
            return
        body = msg.value if isinstance(msg.value, dict) else {}
        channel = body.get("channel")
        if not isinstance(channel, str) or not channel:
            logger.warning("MQTT command %s without a 'channel' — ignoring (payload=%r)", action, msg.value)
            return
        assert self._api is not None  # guaranteed by start(enable_commands=True)
        try:
            getattr(self._api, method)(channel)
            logger.info("MQTT command %s on %s -> ok", action, channel)
        except UCMAPIError as exc:
            logger.warning("MQTT command %s on %s failed: %s", action, channel, exc)


def attach_bridge(
    events: Any,
    bridge: MqttEventBridge,
    trunks: Iterable[str] = (),
    *,
    raw_events: bool = True,
) -> None:
    """Convenience wiring: hook a :class:`MqttEventBridge` onto a UCMEventClient.

    Args:
        events: A :class:`~ucmstuff.ucm6204_api.UCMEventClient`.
        bridge: The bridge to attach.
        trunks: Inbound trunk name(s); if given, a
            :class:`~ucmstuff.ucm6204_api.TrunkCallRouter` publishes parsed
            incoming calls via :meth:`MqttEventBridge.publish_incoming`.
        raw_events: Also publish every raw event via
            :meth:`MqttEventBridge.publish_event`. Defaults to ``True``.
    """
    # Imported here so the module's import graph doesn't force TrunkCallRouter on
    # callers that only want raw-event publishing.
    from ucmstuff.ucm6204_api import TrunkCallRouter

    if raw_events:
        events.add_event_handler(bridge.publish_event)
    trunk_list = [t for t in trunks if t]
    if trunk_list:
        events.add_event_handler(TrunkCallRouter(trunk_list, on_call=bridge.publish_incoming).handle)
