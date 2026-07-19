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

    ucm6204/events/<eventname>       transient  full notify event as JSON
    ucm6204/calls/<trunk>/incoming   transient  parsed IncomingCall as JSON
    ucm6204/calls/<trunk>/ended      transient  same call once its leg terminates
    ucm6204/cmd/{accept,refuse,hangup}   inbound   {"channel": "PJSIP/..."}

All outbound messages are published with ``retain=False`` — they are point-in-time
notifications, never retained "last value" state.

Incoming calls are published per inbound trunk (``<trunk>`` is the call's
``inbound_trunk_name``, MQTT-sanitized); subscribe to ``ucm6204/calls/+/incoming``
for all trunks or ``ucm6204/calls/<trunk>/incoming`` for one. When the routed
call leg terminates, the same payload (``state: "Ended"``) is published to
``.../ended`` — start and end of a call share one stream, matched via
``channel``.

Serialization note: mqttstuff's ``publish_one`` only wraps ``dict`` values (via
``json.dumps``); a bare ``list`` — e.g. an ``ActiveCallStatus`` ``eventbody`` — is
passed straight to paho and raises ``TypeError``. This bridge therefore always
publishes a ``dict`` (the whole event, or a hand-built payload), never a naked
list.
"""

import logging
from dataclasses import asdict
from typing import Any, Iterable

from mqttstuff import MosquittoClientWrapper, MWMqttMessage

from ucmstuff.ucm6204_api import UCM6204, IncomingCall, NotifyEvent, UCMAPIError, UCMEventClient

# stdlib logger by design: ucm6204_api.configure_logging() routes root-logger
# records into loguru via its _InterceptHandler, so this output lands there too.
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

    Args:
        name: The raw trunk name (e.g. ``inbound_trunk_name`` from an
            :class:`~ucmstuff.ucm6204_api.ActiveCall`).

    Returns:
        str: A single MQTT topic segment, never empty.
    """
    safe = name.strip().translate(_MQTT_RESERVED)
    return safe or "unknown"


class MqttEventBridge:
    """Publish UCM events to MQTT and optionally dispatch MQTT commands to the API.

    See the module docstring for the topic layout and the outbound/inbound
    direction split.
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
            base_topic: Root of the topic tree. Defaults to ``"ucm6204"``.
            api: A connected :class:`UCM6204` control client. Required only when the
                inbound command path is enabled (:meth:`start` with
                ``enable_commands=True``); ``None`` disables control.
            tls: Encrypt the connection with TLS.
            tls_ca: Path to the CA certificate (PEM) that signed the broker's
                certificate; ``None`` uses the system CA store.
            tls_cert: Path to a client certificate (PEM) for mutual TLS, or ``None``.
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
        self._api = api
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

    # ── Outbound: UCM → MQTT ────────────────────────────────────────────────

    def publish_event(self, event: NotifyEvent) -> None:
        """EventHandler: publish one raw notify event to ``<base>/events/<eventname>``.

        Register with :meth:`UCMEventClient.add_event_handler`. Publishes the whole
        event dict (``eventname`` + ``eventbody`` + ``transactionid``) as JSON.
        Never raises — a broker hiccup must not kill the WebSocket receive loop.

        Args:
            event: The notify event delivered by :class:`UCMEventClient`.
        """
        name = str(event.get("eventname") or "unknown")
        self._safe_publish(f"{self._base}/events/{name}", dict(event))

    def publish_incoming(self, call: IncomingCall) -> None:
        """CallHandler: publish a parsed incoming call to ``<base>/calls/<trunk>/incoming``.

        Register as the ``on_call`` of a :class:`TrunkCallRouter` (or call it from
        your own router). The inbound trunk becomes a topic segment (MQTT-sanitized,
        see :func:`_mqtt_safe`), so subscribers can filter per trunk. Drops the bulky
        ``raw`` leg dict from the payload.

        Args:
            call: The parsed incoming call, as produced by :class:`TrunkCallRouter`.
        """
        payload = {k: v for k, v in asdict(call).items() if k != "raw"}
        trunk = _mqtt_safe(call.trunk)
        self._safe_publish(f"{self._base}/calls/{trunk}/incoming", payload)

    def publish_ended(self, call: IncomingCall) -> None:
        """CallHandler: publish a terminated call to ``<base>/calls/<trunk>/ended``.

        Register as the ``on_end`` of a :class:`TrunkCallRouter`. The payload has
        the same shape as :meth:`publish_incoming` (``state`` is ``"Ended"``), so
        subscribers can pair start and end of a call via ``channel``.

        Args:
            call: The ended call, as produced by :class:`TrunkCallRouter`.
        """
        payload = {k: v for k, v in asdict(call).items() if k != "raw"}
        trunk = _mqtt_safe(call.trunk)
        self._safe_publish(f"{self._base}/calls/{trunk}/ended", payload)

    def _safe_publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish ``payload`` (always a dict → JSON), swallowing broker errors.

        Always publishes with ``retain=False``: these are point-in-time events
        (an event fired, a call arrived), so a retained "last value" left on the
        broker would be replayed as stale state to every late subscriber.

        Args:
            topic: Full MQTT topic to publish to.
            payload: The JSON-serializable payload (arbitrary JSON-compatible
                values, hence ``Any``); ``mqttstuff.publish_one`` wraps it in
                ``json.dumps``. See the module docstring's serialization note on
                why this must always be a ``dict``, never a bare ``list``.
        """
        try:
            ok = self._mq.publish_one(topic, payload, retain=False)
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
        logger.info("MQTT bridge connecting to host=%s port=%d base=%s", self._host, self._port, self._base)
        connected = self._mq.wait_for_connect_and_start_loop()
        if connected:
            logger.info("MQTT bridge successfully connected to host=%s port=%d", self._host, self._port)
        else:
            logger.warning(
                "MQTT bridge NOT connected to host=%s port=%d within timeout — paho keeps retrying in background",
                self._host,
                self._port,
            )

    def stop(self) -> None:
        """Disconnect from the broker."""
        try:
            self._mq.disconnect()
        except Exception:
            logger.exception("MQTT disconnect failed")

    #: Command topic suffix → matching :class:`~ucmstuff.ucm6204_api.UCM6204` method name.
    _COMMANDS: dict[str, str] = {"accept": "accept_call", "refuse": "refuse_call", "hangup": "hangup"}

    def _on_command(self, msg: MWMqttMessage, userdata: dict[str, Any]) -> None:
        """Dispatch one ``<base>/cmd/<action>`` message to the API control client.

        Payload is JSON with at least ``{"channel": "PJSIP/..."}``. Unknown actions,
        missing channels and API errors are logged, never raised (this runs in the
        paho network thread).

        Registered via :meth:`start` as the ``mqttstuff`` message callback for
        ``<base>/cmd/#`` (``rettype="json"``), so ``mqttstuff`` calls it directly —
        its signature is dictated by :meth:`MosquittoClientWrapper.add_message_callback`.

        Args:
            msg: The decoded command message; ``msg.value`` is the JSON payload
                (expected to be a ``dict``) and ``msg.topic`` is the full command
                topic (``<base>/cmd/<action>``).
            userdata: ``mqttstuff``'s internal per-client state dict (topic list,
                QoS, connect condition …). Untouched here — accepted only because
                ``mqttstuff`` always passes it to message callbacks; its value
                types are heterogeneous library internals, hence ``Any``.
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
    eventclient: UCMEventClient,
    bridge: MqttEventBridge,
    trunks: Iterable[str] = (),
    *,
    raw_events: bool = True,
) -> None:
    """Convenience wiring: hook a :class:`MqttEventBridge` onto a UCMEventClient.

    Args:
        eventclient: The :class:`~ucmstuff.ucm6204_api.UCMEventClient` to attach the
            bridge to.
        bridge: The bridge to attach.
        trunks: Inbound trunk name(s); if given, a
            :class:`~ucmstuff.ucm6204_api.TrunkCallRouter` is registered that
            publishes parsed incoming calls via
            :meth:`MqttEventBridge.publish_incoming` and their termination via
            :meth:`MqttEventBridge.publish_ended`. Empty → no call routing.
        raw_events: Also publish every raw event via
            :meth:`MqttEventBridge.publish_event`. Defaults to ``True``.
    """
    # Imported here so the module's import graph doesn't force TrunkCallRouter on
    # callers that only want raw-event publishing.
    from ucmstuff.ucm6204_api import TrunkCallRouter

    if raw_events:
        eventclient.add_event_handler(bridge.publish_event)
    trunk_list = [t for t in trunks if t]
    if trunk_list:
        router = TrunkCallRouter(trunk_list, on_call=bridge.publish_incoming, on_end=bridge.publish_ended)
        eventclient.add_event_handler(router.handle)
