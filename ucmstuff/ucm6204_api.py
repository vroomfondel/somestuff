#!/usr/bin/env python3
"""HTTP-API control client and WebSocket event client for the Grandstream UCM6204.

The UCM6204 exposes two **separate** interfaces with different session models:

1. **HTTPS API** (``/api``) — request/response actions for querying and *controlling*
   the PBX (system status, list channels, ``acceptCall``, ``refuseCall``,
   ``Hangup``, ``dialExtension`` …). Authenticated with the **API user**
   (e.g. ``cdrapi``) via challenge/response over HTTPS. Implemented by
   :class:`UCM6204`.
2. **WebSocket API** (``/websockify``) — a persistent connection over which the UCM
   *pushes* real-time events (``ActiveCallStatus``, ``ExtensionStatus``,
   ``PbxStatus`` …). The **client connects to the UCM**, authenticates with a
   **web user** (e.g. ``administrator``) via a challenge/login handshake sent as
   JSON frames, then ``subscribe``s to event names. Implemented by
   :class:`UCMEventClient`.

These are genuinely independent: the API user does **not** work on the WebSocket,
and the WebSocket does **not** accept HTTPS-API query actions (returns ``-19``).
To both *monitor* and *control* calls you run both clients together — receive an
event via :class:`UCMEventClient`, then act on it via :class:`UCM6204`.

Both interfaces negotiate a weak Diffie-Hellman group, so a lowered OpenSSL
security level (``@SECLEVEL=1``) is used for the HTTPS session and the WSS socket.

Attributes:
    logger (logging.Logger): Module-wide logger named ``ucm6204``.

Dependencies:
    ``pip install requests typer websocket-client``

Example:
    Monitor events and act on incoming calls on a specific trunk::

        from ucmstuff.ucm6204_api import UCM6204, UCMEventClient, TrunkCallRouter, IncomingCall

        api = UCM6204(host="ucm.example", api_user="cdrapi", api_password="…")
        api.connect()

        events = UCMEventClient(host="ucm.example", web_user="webuser",
                                web_password="…")

        def route(call: IncomingCall) -> None:
            if call.number == "+491234567":
                api.accept_call(call.channel)

        events.add_event_handler(TrunkCallRouter("MyTrunk", on_call=route).handle)
        events.run(block=True)
"""

from __future__ import annotations

import hashlib
import json
import logging
import ssl
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar, Literal, NotRequired, TypedDict

import requests
import typer
import websocket
from requests.adapters import HTTPAdapter

logger = logging.getLogger("ucm6204")

# ──────────────────────────────────────────────────────────────────────────────
# Type aliases
# ──────────────────────────────────────────────────────────────────────────────

# A single decoded JSON value (recursive), and a top-level JSON object.
JSONValue = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject = dict[str, JSONValue]

# A field of an outgoing HTTPS-API ``request`` object (strings, or the not-yet-set
# cookie ``None``), and the full request payload envelope.
RequestField = str | None
RequestPayload = dict[str, dict[str, RequestField]]


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket protocol types
# ──────────────────────────────────────────────────────────────────────────────


class UCMEvent(StrEnum):
    """Subscribable WebSocket event names of the UCM6204.

    Every value is verified against this firmware — it is either emitted by the UCM
    or subscribed by its own web UI. ``subscribe`` is all-or-nothing: an unknown
    name makes the UCM reject the whole batch with status ``-9``, so only pass
    these members.
    """

    #: Per-channel call state (``add``/``update``/``delete``), caller/callee,
    #: trunk and ``state`` (Down/Ring/Ringing …) — the actual call events.
    ACTIVE_CALL = "ActiveCallStatus"
    #: Extension state: ``Idle`` / ``Ringing`` / ``InUse``.
    EXTENSION = "ExtensionStatus"
    #: PBX summary: available/busy/unavailable/unmonitored trunk counts, calls_num.
    PBX = "PbxStatus"
    #: Per-trunk reachability / registration status.
    TRUNK = "TrunkStatus"
    #: User presence (available, away, DND …).
    PRESENCE = "PresenceStatus"
    #: Per-extension voicemail counts (new / old / urgent).
    VOICEMAIL = "VoiceMailStatus"
    #: CPU (per-core) and memory usage — noisy, ticks every few seconds.
    RESOURCE_USAGE = "ResourceUsageStatus"
    #: Network interface up/down status.
    INTERFACE = "InterfaceStatus"
    #: Equipment capacity / limits (extensions, concurrent calls …).
    EQUIPMENT_CAPACITY = "EquipmentCapacityStatus"


#: Default events to subscribe to: the call-relevant ones (list, since ``subscribe``
#: takes an array and the config field is mutable per instance).
DEFAULT_EVENTS: list[str] = [UCMEvent.ACTIVE_CALL, UCMEvent.EXTENSION, UCMEvent.PBX]


class ChallengeRequest(TypedDict):
    """WS ``challenge`` request message (step 1 of the handshake)."""

    transactionid: str
    action: Literal["challenge"]
    username: str
    version: str


class LoginRequest(TypedDict):
    """WS ``login`` request message (step 2): ``token = MD5(challenge + password)``."""

    transactionid: str
    action: Literal["login"]
    username: str
    token: str


class SubscribeRequest(TypedDict):
    """WS ``subscribe`` request message: register the event names to receive."""

    transactionid: str
    action: Literal["subscribe"]
    eventnames: list[str]


class HeartbeatRequest(TypedDict):
    """WS ``heartbeat`` request message: keepalive, sent periodically."""

    transactionid: str
    action: Literal["heartbeat"]


#: Any request ``message`` the client sends over the WebSocket.
RequestMessage = ChallengeRequest | LoginRequest | SubscribeRequest | HeartbeatRequest


class ActiveCall(TypedDict):
    """One channel entry in an ``ActiveCallStatus`` eventbody.

    On ``action == "delete"`` only ``channel``/``chantype``/``action`` are present;
    all other fields are therefore optional.
    """

    channel: str
    chantype: str
    action: Literal["add", "update", "delete"]
    uniqueid: NotRequired[str]
    linkedid: NotRequired[str]
    state: NotRequired[str]  # Down / Ring / Ringing / Up / ...
    service: NotRequired[str]
    callername: NotRequired[str | None]
    callernum: NotRequired[str | None]
    connectedname: NotRequired[str | None]
    connectednum: NotRequired[str | None]
    callid: NotRequired[str]
    alloc_time: NotRequired[str]
    inbound_trunk_name: NotRequired[str | None]
    outbound_trunk_name: NotRequired[str | None]


class ExtensionState(TypedDict):
    """One entry in an ``ExtensionStatus`` eventbody."""

    extension: str
    status: str  # Idle / Ringing / InUse / ...


class NotifyEvent(TypedDict):
    """A single ``notify`` item pushed by the UCM and passed to handlers.

    The wire frame also carries ``action`` (always ``"notify"``), but ``_dispatch``
    already filters on that before handing the item over, so it is intentionally
    not part of this type — by here it is always ``"notify"`` and carries no info.

    ``eventbody`` is left as :data:`JSONValue` because its shape depends on
    ``eventname`` (e.g. a ``list[ActiveCall]`` for ``ActiveCallStatus``, a
    ``list[ExtensionState]`` for ``ExtensionStatus``, a ``dict`` for
    ``PbxStatus``). Handlers narrow it based on ``eventname``.
    """

    eventname: str
    eventbody: JSONValue
    transactionid: NotRequired[str]


# A callback invoked with a single decoded :class:`NotifyEvent`.
EventHandler = Callable[[NotifyEvent], None]


# ──────────────────────────────────────────────────────────────────────────────
# TLS
# ──────────────────────────────────────────────────────────────────────────────


class _LegacyTLSAdapter(HTTPAdapter):
    """``HTTPAdapter`` that pins a custom :class:`ssl.SSLContext`.

    The UCM6204 only offers a weak Diffie-Hellman group during the TLS handshake,
    which modern OpenSSL rejects with ``[SSL: DH_KEY_TOO_SMALL]``. This adapter
    injects an ``SSLContext`` with a lowered security level into every connection
    of the mounted session.

    Args:
        ssl_context: The context to use for all HTTPS connections.
    """

    def __init__(self, ssl_context: ssl.SSLContext) -> None:
        self._ssl_context = ssl_context
        super().__init__()

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        """Attach the pinned SSL context when the pool manager is created."""
        kwargs["ssl_context"] = self._ssl_context
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args: Any, **kwargs: Any) -> object:
        """Attach the pinned SSL context when a proxy manager is created."""
        kwargs["ssl_context"] = self._ssl_context
        return super().proxy_manager_for(*args, **kwargs)


def _build_ssl_context(security_level: int, verify_ssl: bool) -> ssl.SSLContext:
    """Build an ``SSLContext`` tuned for the UCM's legacy TLS stack.

    Args:
        security_level: OpenSSL security level (``@SECLEVEL``). ``1`` allows the
            UCM's 1024-bit Diffie-Hellman group.
        verify_ssl: If ``False``, disable hostname and certificate verification.

    Returns:
        ssl.SSLContext: The configured context.
    """
    context = ssl.create_default_context()
    context.set_ciphers(f"DEFAULT:@SECLEVEL={security_level}")
    if not verify_ssl:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _build_ws_sslopt(security_level: int, verify_ssl: bool) -> dict[str, object]:
    """Build the ``sslopt`` dict for ``websocket-client`` with a lowered level.

    Args:
        security_level: OpenSSL security level (``@SECLEVEL``) for the WSS socket.
        verify_ssl: If ``False``, disable hostname and certificate verification.

    Returns:
        dict[str, object]: Keyword options accepted by
        :func:`websocket.create_connection`'s ``sslopt`` parameter.
    """
    opt: dict[str, object] = {"ciphers": f"DEFAULT:@SECLEVEL={security_level}"}
    if not verify_ssl:
        opt["cert_reqs"] = ssl.CERT_NONE
        opt["check_hostname"] = False
    return opt


def _md5_token(challenge: str, password: str) -> str:
    """Compute the login token as ``MD5(challenge + password)``.

    Args:
        challenge: The challenge string returned by the UCM.
        password: The user's password.

    Returns:
        str: The hexadecimal MD5 digest used to authenticate.
    """
    return hashlib.md5((challenge + password).encode()).hexdigest()


# ──────────────────────────────────────────────────────────────────────────────
# HTTPS API control client
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class UCM6204Config:
    """Configuration for the :class:`UCM6204` HTTPS-API control client.

    Attributes:
        host (str): IP address or hostname of the UCM6204.
        port (int): TCP port of the HTTPS API. Defaults to ``8089``.
        api_user (str): API username (e.g. ``cdrapi``).
        api_password (str): The matching API password.
        api_version (str): API protocol version sent in the challenge request.
            Defaults to ``"1.0"``.
        verify_ssl (bool): Enable TLS certificate verification. Defaults to
            ``False`` (the UCM usually serves a self-signed certificate).
        tls_security_level (int): OpenSSL security level for the TLS handshake.
            Defaults to ``1`` to accept the UCM's weak Diffie-Hellman group.
        cookie_refresh_interval (int): Seconds after which the API cookie is
            proactively refreshed. Defaults to ``540`` (UCM timeout = 600 s).
    """

    host: str
    port: int = 8089
    api_user: str = ""
    api_password: str = ""
    api_version: str = "1.0"
    verify_ssl: bool = False
    tls_security_level: int = 1
    cookie_refresh_interval: int = 540


class UCM6204:
    """HTTPS-API control client for the Grandstream UCM6204.

    Handles challenge/response authentication, holds and auto-refreshes the session
    cookie and exposes a generic :meth:`api_call` plus convenience wrappers for
    queries (system status, channel lists, CDR) and call control (``acceptCall``,
    ``refuseCall``, ``Hangup``, ``dialExtension`` …). Pair it with
    :class:`UCMEventClient` to act on received events.

    Attributes:
        config (UCM6204Config): The bundled configuration of the instance.
    """

    def __init__(
        self,
        host: str,
        port: int = 8089,
        api_user: str = "",
        api_password: str = "",
        api_version: str = "1.0",
        verify_ssl: bool = False,
        tls_security_level: int = 1,
    ) -> None:
        """Initialize the client and the underlying HTTP session.

        Args:
            host: IP address or hostname of the UCM6204.
            port: TCP port of the HTTPS API. Defaults to ``8089``.
            api_user: API username.
            api_password: The matching API password.
            api_version: API protocol version for the challenge request.
                Defaults to ``"1.0"``.
            verify_ssl: Enable TLS certificate verification. Defaults to ``False``.
            tls_security_level: OpenSSL security level. Defaults to ``1`` to accept
                the UCM's weak Diffie-Hellman group.
        """
        self.config = UCM6204Config(
            host=host,
            port=port,
            api_user=api_user,
            api_password=api_password,
            api_version=api_version,
            verify_ssl=verify_ssl,
            tls_security_level=tls_security_level,
        )
        self._cookie: str | None = None
        self._cookie_timestamp: float = 0.0
        self._session: requests.Session = requests.Session()
        self._session.verify = verify_ssl
        context = _build_ssl_context(tls_security_level, verify_ssl)
        self._session.mount("https://", _LegacyTLSAdapter(context))
        if not verify_ssl:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @property
    def api_url(self) -> str:
        """str: Full URL of the UCM's HTTPS API endpoint."""
        return f"https://{self.config.host}:{self.config.port}/api"

    # ── Authentication ──────────────────────────────────────────────────────

    def _get_challenge(self) -> str:
        """Request the challenge string from the UCM (auth step 1).

        Returns:
            str: The challenge string used to compute the login token.

        Raises:
            requests.HTTPError: On an HTTP error status of the response.
            UCMAPIError: If the response ``status`` is non-zero or malformed.
        """
        payload: RequestPayload = {
            "request": {
                "action": "challenge",
                "user": self.config.api_user,
                "version": self.config.api_version,
            }
        }
        data = self._post(payload)
        challenge = self._response_string(data, "challenge")
        logger.debug("Challenge received: %s", challenge)
        return challenge

    def _login(self) -> str:
        """Perform the login (auth step 2) and return the session cookie.

        Returns:
            str: The session cookie issued by the UCM.

        Raises:
            requests.HTTPError: On an HTTP error status of the response.
            UCMAPIError: If the response ``status`` is non-zero (e.g. -37 on wrong
                account/password) or malformed.
        """
        token = _md5_token(self._get_challenge(), self.config.api_password)
        payload: RequestPayload = {
            "request": {
                "action": "login",
                "token": token,
                "user": self.config.api_user,
            }
        }
        data = self._post(payload)
        cookie = self._response_string(data, "cookie")
        logger.info("API login successful, cookie: %s", cookie)
        return cookie

    def connect(self) -> None:
        """Establish the connection: perform the login and store the cookie.

        Must be called before the first :meth:`api_call`.

        Raises:
            requests.HTTPError: On an HTTP error status during login.
            UCMAPIError: On an API error during login.
        """
        self._cookie = self._login()
        self._cookie_timestamp = time.time()

    def _refresh_cookie_if_needed(self) -> None:
        """Refresh the cookie if it is older than the refresh interval."""
        if time.time() - self._cookie_timestamp > self.config.cookie_refresh_interval:
            logger.info("API cookie expired — renewing login …")
            self.connect()

    # ── API calls ───────────────────────────────────────────────────────────

    def _post(self, payload: RequestPayload) -> JSONObject:
        """POST a request payload, validate the status and return the reply.

        Args:
            payload: The full ``{"request": {...}}`` envelope to send.

        Returns:
            JSONObject: The decoded JSON reply.

        Raises:
            requests.HTTPError: On an HTTP error status of the response.
            UCMAPIError: If the reply ``status`` is non-zero.
        """
        resp = self._session.post(
            self.api_url,
            json=payload,
            headers={"Content-Type": "application/json;charset=UTF-8", "Connection": "close"},
        )
        resp.raise_for_status()
        data: JSONObject = resp.json()
        self._check_status(data)
        return data

    def api_call(self, action: str, **params: str) -> JSONObject:
        """Perform a generic HTTPS-API call against the UCM.

        The cookie is refreshed if needed before the call. All keyword arguments
        are inserted as fields into the ``request`` object.

        Args:
            action: Name of the API action (e.g. ``"getSystemStatus"``).
            **params: Additional request parameters.

        Returns:
            JSONObject: The decoded JSON response.

        Raises:
            requests.HTTPError: On an HTTP error status of the response.
            UCMAPIError: If the response ``status`` is non-zero.
        """
        self._refresh_cookie_if_needed()
        request: dict[str, RequestField] = {"action": action, "cookie": self._cookie}
        for key, val in params.items():
            request[key] = val
        return self._post({"request": request})

    # ── Convenience methods ─────────────────────────────────────────────────

    def get_system_status(self) -> JSONObject:
        """Query the active system status (``getSystemStatus``)."""
        return self.api_call("getSystemStatus")

    def get_system_general_status(self) -> JSONObject:
        """Query the general system status (``getSystemGeneralStatus``)."""
        return self.api_call("getSystemGeneralStatus")

    def list_accounts(self, options: str | None = None, page: int = 1, item_num: int = 50) -> JSONObject:
        """List the configured accounts/extensions (``listAccount``).

        Args:
            options: Optional comma-separated field filter. ``None`` → defaults.
            page: Pagination page number (1-based). Defaults to ``1``.
            item_num: Number of entries per page. Defaults to ``50``.

        Returns:
            JSONObject: Response of the ``listAccount`` action.
        """
        params: dict[str, str] = {"page": str(page), "item_num": str(item_num)}
        if options:
            params["options"] = options
        return self.api_call("listAccount", **params)

    def list_bridged_channels(self) -> JSONObject:
        """List the currently bridged channels (``listBridgedChannels``)."""
        return self.api_call("listBridgedChannels")

    def list_unbridged_channels(self) -> JSONObject:
        """List the currently unbridged channels (``listUnBridgedChannels``)."""
        return self.api_call("listUnBridgedChannels")

    def dial_extension(self, extension: str) -> JSONObject:
        """Initiate a call to an extension (``dialExtension``).

        Args:
            extension: The extension to dial.

        Returns:
            JSONObject: Response of the ``dialExtension`` action.
        """
        return self.api_call("dialExtension", extension=extension)

    def hangup(self, channel: str) -> JSONObject:
        """Hang up an active channel (``Hangup``).

        Args:
            channel: Identifier of the channel to hang up.

        Returns:
            JSONObject: Response of the ``Hangup`` action.
        """
        return self.api_call("Hangup", channel=channel)

    def accept_call(self, channel: str) -> JSONObject:
        """Answer an incoming call (``acceptCall``).

        Requires **Call Control** enabled in the API configuration. After the
        real-time event arrives, the application has ~10 seconds to act.

        Args:
            channel: Channel name from the ``ActiveCallStatus`` event, e.g.
                ``"PJSIP/trunk_1-00000002"``.

        Returns:
            JSONObject: Response of the ``acceptCall`` action.
        """
        return self.api_call("acceptCall", channel=channel)

    def refuse_call(self, channel: str) -> JSONObject:
        """Reject an incoming call (``refuseCall``).

        Requires **Call Control** enabled in the API configuration. After the
        real-time event arrives, the application has ~10 seconds to act.

        Args:
            channel: Channel name from the ``ActiveCallStatus`` event.

        Returns:
            JSONObject: Response of the ``refuseCall`` action.
        """
        return self.api_call("refuseCall", channel=channel)

    def mute(self, channel: str) -> JSONObject:
        """Mute a channel (``mute``).

        Args:
            channel: Identifier of the channel to mute.

        Returns:
            JSONObject: Response of the ``mute`` action.
        """
        return self.api_call("mute", channel=channel)

    def unmute(self, channel: str) -> JSONObject:
        """Unmute a channel (``unmute``).

        Args:
            channel: Identifier of the channel to unmute.

        Returns:
            JSONObject: Response of the ``unmute`` action.
        """
        return self.api_call("unmute", channel=channel)

    def get_cdr(self, start_time: str, end_time: str, format: str = "json", **params: str) -> JSONObject:
        """Query Call Detail Records for a time range (``cdrapi``).

        Args:
            start_time: Start time in ``'YYYY-MM-DD'`` format.
            end_time: End time in ``'YYYY-MM-DD'`` format.
            format: Output format. Defaults to ``"json"``.
            **params: Further parameters passed through to the CDR API.

        Returns:
            JSONObject: Response of the ``cdrapi`` action.
        """
        return self.api_call("cdrapi", startTime=start_time, endTime=end_time, format=format, **params)

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _check_status(data: JSONObject) -> None:
        """Check the ``status`` field of a reply and raise on errors.

        Args:
            data: The decoded JSON reply.

        Raises:
            UCMAPIError: If ``data["status"]`` is non-zero (a non-integer status
                is treated as system error ``-9``).
        """
        raw_status = data.get("status", 0)
        status = raw_status if isinstance(raw_status, int) else -9
        if status != 0:
            raise UCMAPIError(status, data)

    @staticmethod
    def _response_string(data: JSONObject, field_name: str) -> str:
        """Extract a string field from the ``response`` object of a reply.

        Args:
            data: The decoded JSON reply.
            field_name: Name of the field to read from ``data["response"]``.

        Returns:
            str: The value of the requested field.

        Raises:
            UCMAPIError: If ``response`` or the field is missing/not a string.
        """
        response = data.get("response")
        if not isinstance(response, dict):
            raise UCMAPIError(-9, data)
        value = response.get(field_name)
        if not isinstance(value, str):
            raise UCMAPIError(-9, data)
        return value


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket event client
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class UCMEventConfig:
    """Configuration for the :class:`UCMEventClient` WebSocket client.

    Attributes:
        host (str): IP address or hostname of the UCM6204.
        ws_port (int): TCP port of the WebSocket endpoint. Defaults to ``8089``.
        ws_path (str): WebSocket path. Defaults to ``"/websockify"``.
        web_user (str): Web user for the WS handshake (e.g. ``administrator``).
            The HTTPS-API user does **not** work here.
        web_password (str): The matching web-user password.
        version (str): Protocol version in the WS challenge. Defaults to ``"1"``.
        event_names (list[str]): Event names to subscribe to. The UCM rejects the
            whole ``subscribe`` (``-9``) if any name is invalid.
        verify_ssl (bool): Enable TLS certificate verification. Defaults to
            ``False``.
        tls_security_level (int): OpenSSL security level for the WSS socket.
            Defaults to ``1`` to accept the UCM's weak Diffie-Hellman group.
        heartbeat_interval (int): Seconds between application-level ``heartbeat``
            frames that keep the WS session alive. Defaults to ``30``.
        reconnect_delay (int): Seconds to wait before reconnecting after a drop.
            Defaults to ``5``.
        recv_timeout (int): Socket read poll interval in seconds; also bounds how
            promptly heartbeats are sent. Defaults to ``5``.
    """

    host: str
    web_user: str
    web_password: str
    ws_port: int = 8089
    ws_path: str = "/websockify"
    version: str = "1"
    event_names: list[str] = field(default_factory=lambda: list(DEFAULT_EVENTS))
    verify_ssl: bool = False
    tls_security_level: int = 1
    heartbeat_interval: int = 30
    reconnect_delay: int = 5
    recv_timeout: int = 5


class UCMEventClient:
    """WebSocket event client that receives real-time events from the UCM6204.

    Connects to ``wss://<host>:<ws_port><ws_path>``, authenticates with a web user
    via a JSON ``challenge``/``login`` handshake, ``subscribe``s to the configured
    event names and dispatches each ``notify`` event to the registered handlers.
    An application-level ``heartbeat`` keeps the session alive, and the client
    reconnects automatically after a drop.

    Attributes:
        config (UCMEventConfig): The bundled configuration of the instance.
    """

    def __init__(
        self,
        host: str,
        web_user: str,
        web_password: str,
        ws_port: int = 8089,
        ws_path: str = "/websockify",
        event_names: list[str] | None = None,
        verify_ssl: bool = False,
        tls_security_level: int = 1,
        heartbeat_interval: int = 30,
    ) -> None:
        """Initialize the event client.

        Args:
            host: IP address or hostname of the UCM6204.
            web_user: Web user for the WS handshake (e.g. ``administrator``).
            web_password: The matching web-user password.
            ws_port: TCP port of the WebSocket endpoint. Defaults to ``8089``.
            ws_path: WebSocket path. Defaults to ``"/websockify"``.
            event_names: Event names to subscribe to. ``None`` → a sensible default
                (``ActiveCallStatus``, ``ExtensionStatus``, ``PbxStatus``).
            verify_ssl: Enable TLS certificate verification. Defaults to ``False``.
            tls_security_level: OpenSSL security level. Defaults to ``1``.
            heartbeat_interval: Seconds between ``heartbeat`` frames. Defaults 30.
        """
        self.config = UCMEventConfig(
            host=host,
            web_user=web_user,
            web_password=web_password,
            ws_port=ws_port,
            ws_path=ws_path,
            event_names=event_names if event_names is not None else list(DEFAULT_EVENTS),
            verify_ssl=verify_ssl,
            tls_security_level=tls_security_level,
            heartbeat_interval=heartbeat_interval,
        )
        self._handlers: list[EventHandler] = []
        self._ws: websocket.WebSocket | None = None
        self._tx = 1000
        self._stop = threading.Event()
        self._connected = False

    @property
    def ws_url(self) -> str:
        """str: Full ``wss://`` URL of the WebSocket endpoint."""
        return f"wss://{self.config.host}:{self.config.ws_port}{self.config.ws_path}"

    @property
    def connected(self) -> bool:
        """bool: Whether the client is authenticated and subscribed right now."""
        return self._connected

    def on_event(self, handler: EventHandler) -> EventHandler:
        """Decorator that registers a callback for incoming events.

        Args:
            handler: Function invoked per event with the decoded notify object
                ``{"eventname": ..., "eventbody": ..., "transactionid": ...}``.

        Returns:
            EventHandler: The unchanged, now-registered handler.
        """
        self._handlers.append(handler)
        return handler

    def add_event_handler(self, handler: EventHandler) -> None:
        """Register an event handler (alternative to the decorator).

        Args:
            handler: Function invoked per event with the decoded notify object.
        """
        self._handlers.append(handler)

    def _next_tx(self) -> str:
        """Return a fresh transaction id for a request frame."""
        self._tx += 1
        return f"tx{self._tx}"

    @staticmethod
    def _send(ws: websocket.WebSocket, message: RequestMessage) -> None:
        """Send a ``request`` frame (fire-and-forget).

        Args:
            ws: The connected WebSocket.
            message: The typed ``message`` object of the request frame.
        """
        ws.send(json.dumps({"type": "request", "message": message}))

    def _await_response(self, ws: websocket.WebSocket) -> JSONObject:
        """Read frames until a ``response`` arrives, dispatching notifies meanwhile.

        The WebSocket is full-duplex: ``notify`` frames can interleave with the
        response to a request (e.g. during the handshake). Any such notify is
        dispatched to the handlers so **nothing is lost**.

        Args:
            ws: The connected WebSocket.

        Returns:
            WSResponse: The ``message`` object of the response frame.

        Raises:
            UCMAPIError: If the response ``status`` is non-zero.
        """
        while True:
            raw = ws.recv()
            try:
                frame = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("WS: undecodable frame while awaiting response: %r", raw[:1000])
                continue
            if isinstance(frame, dict) and frame.get("type") == "response":
                msg = frame.get("message")
                if not isinstance(msg, dict):
                    raise UCMAPIError(-9, frame)
                message: JSONObject = msg
                raw_status = message.get("status", 0)
                status = raw_status if isinstance(raw_status, int) else -9
                if status != 0:
                    raise UCMAPIError(status, message)
                return message
            self._dispatch(raw)

    def _authenticate(self, ws: websocket.WebSocket) -> None:
        """Run the WS handshake: challenge, login and subscribe.

        Args:
            ws: The freshly connected WebSocket.

        Raises:
            UCMAPIError: If challenge, login or subscribe returns a non-zero status
                (e.g. -9 when an event name is invalid or the user is not a web
                user).
        """
        challenge_req: ChallengeRequest = {
            "transactionid": self._next_tx(),
            "action": "challenge",
            "username": self.config.web_user,
            "version": self.config.version,
        }
        self._send(ws, challenge_req)
        c = self._await_response(ws)
        challenge = c.get("challenge")
        if not isinstance(challenge, str):
            raise UCMAPIError(-9, c)
        login_req: LoginRequest = {
            "transactionid": self._next_tx(),
            "action": "login",
            "username": self.config.web_user,
            "token": _md5_token(challenge, self.config.web_password),
        }
        self._send(ws, login_req)
        self._await_response(ws)
        logger.info("WS login successful for user %s", self.config.web_user)
        subscribe_req: SubscribeRequest = {
            "transactionid": self._next_tx(),
            "action": "subscribe",
            "eventnames": self.config.event_names,
        }
        self._send(ws, subscribe_req)
        self._await_response(ws)
        logger.info("WS subscribed to: %s", ", ".join(self.config.event_names))

    def _dispatch(self, raw: str | bytes) -> None:
        """Parse a received frame and dispatch its ``notify`` items to handlers.

        Lossless by design: anything that is not a recognized ``notify`` event is
        logged at ``WARNING`` (undecodable, unexpected shape, non-notify item)
        rather than silently dropped. Routine ``response`` acks (e.g. heartbeat)
        are logged at ``DEBUG``.

        Args:
            raw: The raw text/bytes frame received from the UCM.
        """
        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("WS: undecodable frame (NOT dropped, raw): %r", raw[:1000])
            return
        if not isinstance(frame, dict):
            logger.warning("WS: unexpected non-object frame (NOT dropped): %s", frame)
            return
        if frame.get("type") == "response":
            logger.debug("WS response ack: %s", frame.get("message"))
            return
        message = frame.get("message")
        if not isinstance(message, list):
            logger.warning("WS: unrecognized frame, no type matched (NOT dropped): %s", frame)
            return
        for item in message:
            if isinstance(item, dict) and item.get("action") == "notify":
                event: NotifyEvent = item  # type: ignore[assignment]  # structural
                for handler in self._handlers:
                    try:
                        handler(event)
                    except Exception:
                        logger.exception("Event handler error for %s", item.get("eventname"))
            else:
                logger.warning("WS: non-notify message item (NOT dropped): %s", item)

    def run(self, block: bool = True) -> None:
        """Run the event loop: connect, authenticate, receive and reconnect.

        Args:
            block: ``True`` runs the loop in the calling thread (for scripts);
                ``False`` runs it in a daemon background thread and returns.
        """
        if block:
            self._run_loop()
        else:
            threading.Thread(target=self._run_loop, daemon=True).start()

    def _run_loop(self) -> None:
        """Connection loop with authentication, receive polling and heartbeat."""
        sslopt = _build_ws_sslopt(self.config.tls_security_level, self.config.verify_ssl)
        header = [f"Origin: https://{self.config.host}:{self.config.ws_port}"]
        while not self._stop.is_set():
            try:
                logger.info("WS connecting to %s", self.ws_url)
                ws = websocket.create_connection(
                    self.ws_url,
                    sslopt=sslopt,
                    header=header,
                    timeout=10,
                )
                self._ws = ws
                self._authenticate(ws)
                self._connected = True
                ws.settimeout(self.config.recv_timeout)
                last_hb = time.time()
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        raw = ""
                    if raw:
                        self._dispatch(raw)
                    if time.time() - last_hb >= self.config.heartbeat_interval:
                        # Fire-and-forget; the heartbeat response is a non-notify
                        # frame that the recv loop harmlessly ignores.
                        heartbeat_req: HeartbeatRequest = {"transactionid": self._next_tx(), "action": "heartbeat"}
                        self._send(ws, heartbeat_req)
                        last_hb = time.time()
            except Exception as exc:
                self._connected = False
                if self._stop.is_set():
                    break
                logger.warning("WS connection lost (%s) — reconnecting in %ds", exc, self.config.reconnect_delay)
                self._stop.wait(self.config.reconnect_delay)
            finally:
                self._connected = False
                self._close_ws()
        logger.info("WS event loop stopped")

    def _close_ws(self) -> None:
        """Close the current WebSocket, ignoring errors."""
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def stop(self) -> None:
        """Stop the event loop and close the connection."""
        self._stop.set()
        self._close_ws()


# ──────────────────────────────────────────────────────────────────────────────
# Incoming-call routing
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class IncomingCall:
    """Ergonomic, parsed view of an incoming call, built by :class:`TrunkCallRouter`.

    Derived from one ``ActiveCall`` leg (see :class:`ActiveCall`) of an
    ``ActiveCallStatus`` event during the ringing phase.

    Attributes:
        number (str): The external caller's number (``callernum``).
        name (str): The caller's display name (``callername``); may be empty.
        channel (str): The PJSIP channel — pass to :meth:`UCM6204.accept_call`,
            :meth:`UCM6204.refuse_call` or :meth:`UCM6204.hangup`.
        trunk (str): The inbound trunk the call arrived on (``inbound_trunk_name``).
        uniqueid (str): The call's unique id.
        state (str): The ring state (``Ring`` / ``Ringing``).
        raw (JSONObject): The full underlying ``ActiveCall`` leg dict.
    """

    number: str
    name: str
    channel: str
    trunk: str
    uniqueid: str
    state: str
    raw: JSONObject


#: A callback invoked with a parsed :class:`IncomingCall`. This is where the
#: caller-based branching (by number/name) lives.
CallHandler = Callable[[IncomingCall], None]


class TrunkCallRouter:
    """Fire a callback exactly once per incoming call on the configured trunk(s).

    Register :meth:`handle` with :meth:`UCMEventClient.add_event_handler`. It
    filters ``ActiveCallStatus`` events down to the given inbound trunk(s), waits
    for the ringing phase, de-duplicates by ``channel`` (one call emits many
    events for the same leg) and invokes ``on_call`` with a structured
    :class:`IncomingCall`. The caller's own branching (by number/name) lives in
    that callback, keeping transport/parsing separate from business logic.

    Note:
        Stateful — it holds the set of in-progress channels for de-duplication, so
        it is a normal instance (not a bag of static methods). Channels are
        released automatically when a leg reports ``action == "delete"``.

    Example::

        def route(call: IncomingCall) -> None:
            if call.number == "+491234567":
                api.accept_call(call.channel)
            elif call.name.upper().startswith("SPAM"):
                api.refuse_call(call.channel)

        events.add_event_handler(TrunkCallRouter("MyTrunk", on_call=route).handle)
    """

    def __init__(
        self, trunks: str | Iterable[str], on_call: CallHandler, ring_states: Iterable[str] = ("Ring", "Ringing")
    ) -> None:
        """Initialize the router.

        Args:
            trunks: One inbound trunk name, or several, to react to. Must match
                the UCM's ``inbound_trunk_name`` exactly.
            on_call: Callback invoked once per matching incoming call.
            ring_states: Call states that count as "ringing" and trigger the
                callback. Defaults to ``("Ring", "Ringing")``.
        """
        self._trunks: set[str] = {trunks} if isinstance(trunks, str) else set(trunks)
        self._on_call = on_call
        self._ring_states: set[str] = set(ring_states)
        self._active: set[str] = set()

    def handle(self, event: NotifyEvent) -> None:
        """Event handler: route matching incoming calls to ``on_call``.

        Register this with :meth:`UCMEventClient.add_event_handler`.

        Args:
            event: The notify event delivered by :class:`UCMEventClient`.
        """
        if event.get("eventname") != "ActiveCallStatus":
            return
        body = event.get("eventbody")
        if not isinstance(body, list):
            return
        for item in body:
            if isinstance(item, dict):
                self._process(item)

    def _process(self, call: JSONObject) -> None:
        """Filter and de-duplicate one call leg, then invoke the callback.

        De-duplication keys on ``channel`` (not ``uniqueid``): a call emits many
        ``add``/``update`` frames for the same channel, and the terminating
        ``delete`` frame carries only ``channel`` (no ``uniqueid``) — so the
        channel is the one id present across the whole leg lifecycle.

        Args:
            call: One ``ActiveCall`` leg from the event body.
        """
        channel = call.get("channel")
        if not isinstance(channel, str):
            return
        if call.get("action") == "delete":
            self._active.discard(channel)  # leg gone → allow a future call to fire
            return
        if call.get("inbound_trunk_name") not in self._trunks:
            return
        if call.get("state") not in self._ring_states:
            return
        if channel in self._active:
            return  # already handled this call leg
        self._active.add(channel)
        uid = call.get("uniqueid")
        incoming = IncomingCall(
            number=str(call.get("callernum") or "").strip(),
            name=str(call.get("callername") or "").strip(),
            channel=channel,
            trunk=str(call.get("inbound_trunk_name") or ""),
            uniqueid=uid if isinstance(uid, str) else "",
            state=str(call.get("state") or ""),
            raw=call,
        )
        try:
            self._on_call(incoming)
        except Exception:
            logger.exception("on_call handler error for channel=%s", channel)


# ──────────────────────────────────────────────────────────────────────────────
# Health server (Kubernetes probes)
# ──────────────────────────────────────────────────────────────────────────────


def start_health_server(host: str, port: int, is_ready: Callable[[], bool]) -> ThreadingHTTPServer:
    """Start a tiny HTTP server exposing ``/healthz`` for Kubernetes probes.

    Since the pod no longer serves an inbound webhook, this minimal endpoint gives
    the liveness/readiness probes something to hit. It reports ``200`` once the
    WebSocket event client is connected, otherwise ``503``.

    Args:
        host: Bind address (e.g. ``"0.0.0.0"``).
        port: Listen port.
        is_ready: Callable returning ``True`` when the client is connected.

    Returns:
        ThreadingHTTPServer: The running server (serving in a daemon thread).
    """

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            ready = is_ready()
            self.send_response(200 if ready else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"connected": ready}).encode())

        def log_message(self, fmt: str, *args: object) -> None:
            logger.debug("health %s - %s", self.address_string(), fmt % args)

    server = ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health server on http://%s:%d/healthz", host, port)
    return server


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────


class UCMAPIError(Exception):
    """Error raised for a failed UCM6204 API/WebSocket call.

    Raised when a reply carries a non-zero ``status``. The plain-text description
    is resolved from :attr:`ERROR_CODES`.

    Attributes:
        status (int): The numeric error code from the UCM.
        raw (object): The full reply object, kept for diagnostics (loosely typed
            since it is used only for inspection, never for control flow).
        ERROR_CODES (dict[int, str]): Known error codes → descriptions.
    """

    ERROR_CODES: ClassVar[dict[int, str]] = {
        0: "Success",
        -1: "Invalid parameters",
        -5: "Authentication required",
        -6: "Cookie error",
        -7: "Connection closed",
        -8: "System timeout",
        -9: "System error",
        -15: "Invalid value",
        -16: "Entry not found",
        -19: "Not supported",
        -24: "Data operation failed",
        -25: "Update failed",
        -26: "Data query failed",
        -37: "Wrong account or password",
        -43: "Data modified or deleted",
        -44: "Entry already exists",
        -45: "Operation too frequent",
        -47: "No permission",
        -50: "Command contains sensitive characters",
        -68: "Login restriction",
        -70: "Login forbidden",
        -71: "Username does not exist",
    }

    def __init__(self, status: int, raw: object) -> None:
        """Create the error with a resolved plain-text message.

        Args:
            status: The numeric error code from the reply.
            raw: The full reply object (stored for later diagnostics).
        """
        self.status = status
        self.raw = raw
        msg = self.ERROR_CODES.get(status, f"Unknown error code: {status}")
        super().__init__(f"UCM API error [{status}]: {msg}")


# ──────────────────────────────────────────────────────────────────────────────
# CLI / demo (Typer)
# ──────────────────────────────────────────────────────────────────────────────

app = typer.Typer(add_completion=False, help="UCM6204 event monitor + control client")


@app.command()
def main(
    host: str = typer.Option(..., "--host", help="UCM6204 IP address"),
    web_user: str = typer.Option(..., "--web-user", help="Web user for the WebSocket (events)"),
    web_password: str = typer.Option(..., "--web-password", help="Web-user password"),
    api_user: str = typer.Option("", "--api-user", help="API user for control actions (optional)"),
    api_password: str = typer.Option("", "--api-password", help="API-user password"),
    port: int = typer.Option(8089, "--port", help="HTTPS-API / WebSocket port"),
    events: str = typer.Option(
        "ActiveCallStatus,ExtensionStatus,PbxStatus", "--events", help="Comma-separated event names to subscribe to"
    ),
    trunk: list[str] = typer.Option(
        [],
        "--trunk",
        help="Inbound trunk name to route incoming calls for (e.g. "
        "MyTrunk). Repeatable: pass --trunk once per trunk "
        "(--trunk a --trunk b). Omit → no routing.",
    ),
    health_host: str = typer.Option("0.0.0.0", "--health-host", help="Health server bind address"),
    health_port: int = typer.Option(8070, "--health-port", help="Health server port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging"),
) -> None:
    """Monitor UCM6204 events over WebSocket and optionally control calls (CLI).

    Starts a health server for Kubernetes probes, connects the HTTPS-API control
    client (if API credentials are given), logs every event, and runs the
    WebSocket event client (blocking). With ``--trunk`` it attaches a
    :class:`TrunkCallRouter` that logs incoming calls on that trunk — extend its
    ``route_incoming`` callback with your own caller-based branching.

    Args:
        host: IP address of the UCM6204.
        web_user: Web user for the WebSocket event stream.
        web_password: Web-user password.
        api_user: API user for control actions. Empty → control disabled.
        api_password: API-user password.
        port: HTTPS-API / WebSocket port. Defaults to ``8089``.
        events: Comma-separated event names to subscribe to.
        trunk: Inbound trunk name to route incoming calls for. Empty → no routing.
        health_host: Bind address of the health server. Defaults to ``"0.0.0.0"``.
        health_port: Health server port. Defaults to ``8070``.
        verbose: If ``True``, log at DEBUG level, otherwise INFO.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    api: UCM6204 | None = None
    if api_user:
        api = UCM6204(host=host, port=port, api_user=api_user, api_password=api_password)
        api.connect()

    ec = UCMEventClient(
        host=host,
        web_user=web_user,
        web_password=web_password,
        ws_port=port,
        event_names=[e.strip() for e in events.split(",") if e.strip()],
    )

    @ec.on_event
    def log_event(event: NotifyEvent) -> None:
        """Log every event for visibility."""
        logger.info("event %s: %s", event.get("eventname"), json.dumps(event.get("eventbody"), ensure_ascii=False))

    trunks = [t.strip() for t in trunk if t.strip()]
    if trunks:

        def route_incoming(call: IncomingCall) -> None:
            """Handle one incoming call on a routed trunk — branch by caller here.

            This only logs the caller. Add your own routing rules below, using the
            control client ``api`` (available when ``--api-user`` is set), e.g.::

                if call.number == "+491234567":
                    api.accept_call(call.channel)
                elif call.name.upper().startswith("SPAM"):
                    api.refuse_call(call.channel)
            """
            logger.info("incoming %s call: %r <%s> on %s", call.trunk, call.name, call.number, call.channel)

        ec.add_event_handler(TrunkCallRouter(trunks, on_call=route_incoming).handle)

    start_health_server(health_host, health_port, lambda: ec.connected)
    logger.info(
        "Monitoring %s:%d — events: %s | control(API): %s | trunk routing: %s",
        host,
        port,
        ec.config.event_names,
        "enabled" if api is not None else "disabled",
        ", ".join(trunks) if trunks else "off",
    )
    logger.info("Waiting for UCM events. Press Ctrl+C to stop.")
    try:
        ec.run(block=True)
    except KeyboardInterrupt:
        ec.stop()


if __name__ == "__main__":
    app()
