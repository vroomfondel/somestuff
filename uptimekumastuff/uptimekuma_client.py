"""Direct Socket.IO client for Uptime Kuma 2.x - without uptime-kuma-api.

The foundation for idempotent provisioning: read -> compare -> write -> read again.
Its user is :mod:`uptimekumastuff.uptimekuma_apply`; usable directly as a library.

Why not uptime-kuma-api?
    The lib reads a **cache**, not the server::

        # uptime_kuma_api/api.py, get_monitors()
        # TODO: replace with getMonitorList?
        r = list(self._get_event_data(Event.MONITOR_LIST).values())

    It never calls ``getMonitorList``. The snapshot comes from the login push and is only
    passively updated - after your own writes it is stale. That is exactly what
    ``delete_monitor()`` failed on ("monitor does not exist", although it existed). For
    idempotency and check_mode this is disqualifying: the actual state you read must be real.

    This client calls ``getMonitorList`` and waits for the ``monitorList`` push sent in
    response. python-socketio is not a new dependency here - the lib builds on it itself.

Known server quirks (all verified against the 2.4.0 server code):
    * ``add`` needs ``conditions`` (NOT NULL); if missing, the server writes NULL.
    * ``notificationIDList`` must be a dict ``{id: True}`` on write - the server does
      ``for (let id in ...)``, a list yields the indices instead of the IDs.
    * ``editMonitor`` does **not** read ``active`` and ``weight``: ``active`` goes via
      ``pauseMonitor``/``resumeMonitor``, ``weight`` is only settable on creation.
    * ``add`` with ``active: false`` prevents ``startMonitor()`` server-side.
"""

import json
import threading
from collections.abc import Callable
from typing import NotRequired, Self, TypedDict, cast

import socketio

# Fields that editMonitor reads from the payload (extracted from server.js 2.4.0).
# Everything else it silently ignores - a diff on those would forever report "changed".
EDITABLE_FIELDS: frozenset[str] = frozenset("""
    accepted_statuscodes authDomain authMethod authWorkstation basic_auth_pass basic_auth_user
    bearer_token body cacheBust conditions databaseConnectionString databaseQuery description
    dns_resolve_server dns_resolve_type docker_container docker_host domainExpiryNotification
    expectedTlsAlert expectedValue expiryNotification game gamedigGivenPortOnly gamedigToken
    grpcBody grpcEnableTls grpcMetadata grpcMethod grpcProtobuf grpcServiceName grpcUrl headers
    hostname httpBodyEncoding ignoreTls interval invertKeyword ipFamily jsonPath jsonPathOperator
    kafkaProducerAllowAutoTopicCreation kafkaProducerBrokers kafkaProducerMessage
    kafkaProducerSaslOptions kafkaProducerSsl kafkaProducerTopic keyword location manual_status
    maxredirects maxretries method mqttCheckType mqttPassword mqttSuccessMessage mqttTopic
    mqttUsername mqttWebsocketPath name notificationIDList oauth_audience oauth_auth_method
    oauth_client_id oauth_client_secret oauth_scopes oauth_token_url packetSize parent ping_count
    ping_numeric ping_per_request_timeout port protocol proxyId pushToken rabbitmqNodes
    rabbitmqPassword rabbitmqUsername radiusCalledStationId radiusCallingStationId radiusPassword
    radiusSecret radiusUsername remote_browser resendInterval responseMaxLength retryInterval
    retryOnlyOnStatusCodeFailure saveErrorResponse saveResponse smtpSecurity snmpOid snmpVersion
    subtype system_service_name timeout tlsCa tlsCert tlsKey type upsideDown url
    wsIgnoreSecWebsocketAcceptHeader wsSubprotocol
    """.split())

# Only applied on creation. editMonitor doesn't read it - a change would be a silent no-op,
# so we report it as not applicable instead of as "changed".
ADD_ONLY_FIELDS: frozenset[str] = frozenset({"weight"})

# Does not go via editMonitor, but via pauseMonitor/resumeMonitor.
PAUSE_RESUME_FIELD = "active"

type Payload = dict[str, object]


class KumaError(Exception):
    """The server answered a call with ``ok: false``."""


class MonitorDict(TypedDict, total=False):
    """A monitor as ``monitorList`` delivers it.

    Kuma delivers 115 fields depending on the monitor type. Only the ones this client
    accesses are listed here; the rest is passed through unchanged.

    Attributes:
        id: Monitor ID.
        name: Display name.
        type: Monitor type, e.g. ``mqtt``, ``group``, ``http``.
        parent: ID of the parent group or ``None``.
        active: Whether the monitor is running.
        weight: Sort weight (only settable on creation).
        notificationIDList: Linked notification IDs.
    """

    id: int
    name: str
    type: str
    parent: int | None
    active: bool
    weight: int
    notificationIDList: list[int]


class UpsertResult(TypedDict):
    """Result of an idempotent upsert, shaped like an Ansible return value.

    Attributes:
        changed: Whether something was changed (or in check_mode: would be).
        object_id: ID of the created/found object, ``None`` only in check_mode when
            creating.
        created: True if the object was newly created.
        diff: Fields that differ, as ``{field: {"before": ..., "after": ...}}``.
        not_applicable: Fields that differ but the server doesn't actually apply
            (see ADD_ONLY_FIELDS).
    """

    changed: bool
    object_id: int | None
    created: bool
    diff: dict[str, dict[str, object]]
    not_applicable: NotRequired[dict[str, dict[str, object]]]


class KumaClient:
    """Socket.IO client with guaranteed fresh reads.

    Attributes:
        url: Base URL of the instance.
        timeout: Seconds to wait for responses and pushes.
    """

    #: Events the server pushes on its own that we capture.
    PUSHED_EVENTS = ("monitorList", "notificationList")

    def __init__(self, url: str, timeout: int = 30) -> None:
        """Creates the client without connecting yet.

        Args:
            url: Base URL, e.g. ``http://127.0.0.1:3001``.
            timeout: Timeout for calls and expected pushes.
        """
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.sio = socketio.Client()
        self._data: dict[str, Payload] = {}
        self._arrived: dict[str, threading.Event] = {n: threading.Event() for n in self.PUSHED_EVENTS}
        for name in self.PUSHED_EVENTS:
            self.sio.on(name, self._make_handler(name))

    def _make_handler(self, name: str) -> Callable[[Payload], None]:
        """Builds the handler that captures a pushed event.

        Args:
            name: Event name.

        Returns:
            The handler function for python-socketio.
        """

        def handler(data: Payload) -> None:
            self._data[name] = data
            self._arrived[name].set()

        return handler

    def __enter__(self) -> Self:
        """Connects on entering the with block.

        Returns:
            The connected client.
        """
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        """Disconnects on leaving the with block."""
        self.close()

    def connect(self) -> None:
        """Establishes the Socket.IO connection (websocket, no polling detour)."""
        self.sio.connect(self.url, transports=["websocket"], wait_timeout=self.timeout)

    def close(self) -> None:
        """Disconnects without throwing on an already-dead connection."""
        try:
            self.sio.disconnect()
        except Exception:
            pass

    def call(self, event: str, *args: object) -> Payload:
        """Sends a socket call and unpacks the server response.

        Args:
            event: Event name, e.g. ``add``.
            *args: Arguments of the handler.

        Returns:
            The server's response.

        Raises:
            KumaError: If the server reports ``ok: false``.
        """
        data = args[0] if len(args) == 1 else tuple(args)
        result = cast(Payload, self.sio.call(event, data, timeout=self.timeout))
        if isinstance(result, dict) and result.get("ok") is False:
            raise KumaError(f"{event}: {result.get('msg')}")
        return result

    def login(self, username: str, password: str) -> None:
        """Logs in.

        The socket login accepts username+password only. API keys
        (``uk1_``/``uk2_``/``uk3_``) apply only to HTTP basic auth on ``/metrics``.

        Args:
            username: Username.
            password: Password.

        Raises:
            KumaError: On wrong credentials.
        """
        self.call("login", {"username": username, "password": password, "token": None})

    def need_setup(self) -> bool:
        """Checks whether the instance still has no user.

        Returns:
            True if ``setup()`` is still pending.
        """
        return bool(self.sio.call("needSetup", timeout=self.timeout))

    def setup(self, username: str, password: str) -> None:
        """Creates the first user of a fresh instance.

        Args:
            username: Desired username.
            password: Desired password (Kuma rejects weak ones).
        """
        self.call("setup", username, password)

    # ------------------------------------------------------------------ reads

    def _await_push(self, event: str) -> Payload:
        """Waits for the next push of an event.

        Args:
            event: Event name from :attr:`PUSHED_EVENTS`.

        Returns:
            The pushed data.

        Raises:
            KumaError: If nothing arrives within the timeout.
        """
        if not self._arrived[event].wait(self.timeout):
            raise KumaError(f"no {event} push within {self.timeout}s")
        return self._data[event]

    def monitors(self) -> dict[int, MonitorDict]:
        """Reads all monitors **fresh from the server**.

        Calls ``getMonitorList``, which triggers ``sendMonitorList`` server-side, and waits
        for the push sent in response. That is exactly what uptime-kuma-api doesn't do.

        The server returns ``notificationIDList`` raw as a dict with string keys
        (``{"1": true}``); we unify this to a sorted ID list so that reads and desired state
        have the same shape. :meth:`_wire` turns it back on write.

        Returns:
            All monitors, by ID.
        """
        self._arrived["monitorList"].clear()
        self.call("getMonitorList")
        pushed = self._await_push("monitorList")
        monitors: dict[int, MonitorDict] = {}
        for key, raw in pushed.items():
            monitor = cast(MonitorDict, dict(cast(Payload, raw)))
            monitor["notificationIDList"] = cast(
                list[int], self._normalize("notificationIDList", monitor.get("notificationIDList") or [])
            )
            monitors[int(key)] = monitor
        return monitors

    def monitor_by_name(self, name: str) -> MonitorDict | None:
        """Looks up a monitor by its name.

        Args:
            name: Display name.

        Returns:
            The monitor or ``None``.

        Raises:
            KumaError: If the name is assigned more than once - Kuma allows that, but then
                it's not a usable key for idempotent work.
        """
        hits = [m for m in self.monitors().values() if m.get("name") == name]
        if len(hits) > 1:
            raise KumaError(f"name {name!r} is assigned {len(hits)}x - not a unique key")
        return hits[0] if hits else None

    def notifications(self) -> list[Payload]:
        """Reads the notification providers, with unpacked config.

        There is no request event for this list; the server pushes it on login and after
        every mutation (``sendNotificationList``). We therefore read the last pushed state.

        The server pushes raw DB rows in which ``config`` is a **JSON string** holding the
        actual provider fields. We unpack it so it can be compared flat - and because
        ``addNotification`` expects exactly that flat form.

        Returns:
            All notification providers as flat dicts.
        """
        pushed = cast(list[Payload], self._await_push("notificationList"))
        result: list[Payload] = []
        for row in pushed:
            config = json.loads(cast(str, row.get("config") or "{}"))
            flat: Payload = {**config}
            flat["id"] = row["id"]
            flat["name"] = row["name"]
            flat["active"] = row.get("active")
            flat["isDefault"] = row.get("isDefault")
            result.append(flat)
        return result

    def notification_by_name(self, name: str) -> Payload | None:
        """Looks up a notification provider by its name.

        Args:
            name: Display name.

        Returns:
            The provider or ``None``.

        Raises:
            KumaError: If the name is assigned more than once.
        """
        hits = [n for n in self.notifications() if n.get("name") == name]
        if len(hits) > 1:
            raise KumaError(f"notification name {name!r} is assigned {len(hits)}x")
        return hits[0] if hits else None

    def tags(self) -> list[Payload]:
        """Reads all tags (a real request/response event).

        Returns:
            All tags.
        """
        return cast(list[Payload], self.call("getTags")["tags"])

    # ----------------------------------------------------------------- writes

    def save_notification(self, notification: Payload, notification_id: int | None = None) -> int:
        """Creates or updates a notification provider.

        ``addNotification`` is both: with ``notification_id`` the server updates the existing
        record, without it creates a new one.

        ``applyExisting`` is forced hard to False. It is not state but a one-shot UI trigger -
        if it were True, Kuma would attach the notification to *all* existing monitors and
        destroy the exact links. The server normalizes it to False on save, but evaluates it
        beforehand.

        Args:
            notification: Flat provider payload.
            notification_id: ID for an update, ``None`` for a create.

        Returns:
            The ID of the created/updated provider.
        """
        payload = {k: v for k, v in notification.items() if k not in ("id", "userId", "user_id")}
        payload["applyExisting"] = False
        return int(cast(int, self.call("addNotification", payload, notification_id)["id"]))

    def delete_notification(self, notification_id: int) -> None:
        """Deletes a notification provider.

        Args:
            notification_id: ID of the provider.
        """
        self.call("deleteNotification", notification_id)

    def add_monitor(self, monitor: Payload) -> int:
        """Creates a monitor.

        Args:
            monitor: Complete monitor payload. ``conditions`` and
                ``accepted_statuscodes`` must be set.

        Returns:
            The new monitor ID.
        """
        return int(cast(int, self.call("add", monitor)["monitorID"]))

    def edit_monitor(self, monitor: Payload) -> None:
        """Changes a monitor.

        Args:
            monitor: Payload including ``id``. ``active`` and ``weight`` are ignored by the
                server (see module docstring).
        """
        self.call("editMonitor", monitor)

    def delete_monitor(self, monitor_id: int) -> None:
        """Deletes a monitor.

        Args:
            monitor_id: ID of the monitor.
        """
        self.call("deleteMonitor", monitor_id)

    def pause_monitor(self, monitor_id: int) -> None:
        """Pauses a monitor.

        Args:
            monitor_id: ID of the monitor.
        """
        self.call("pauseMonitor", monitor_id)

    def resume_monitor(self, monitor_id: int) -> None:
        """Starts a paused monitor.

        Args:
            monitor_id: ID of the monitor.
        """
        self.call("resumeMonitor", monitor_id)

    # ----------------------------------------------------------------- upsert

    def _resolve_notifications(self, names: object) -> list[int]:
        """Resolves notification names to IDs.

        Args:
            names: List of notification names (or already IDs).

        Returns:
            The IDs, sorted - comparable with what the server delivers.

        Raises:
            KumaError: If a notification does not exist.
        """
        if not isinstance(names, list):
            raise KumaError("'notifications' must be a list of names")
        by_name = {n["name"]: int(cast(int, n["id"])) for n in self.notifications()}
        ids: list[int] = []
        for entry in names:
            if isinstance(entry, int):
                ids.append(entry)
            elif entry in by_name:
                ids.append(by_name[cast(str, entry)])
            else:
                raise KumaError(f"notification {entry!r} does not exist")
        return sorted(ids)

    @staticmethod
    def _normalize(key: str, value: object) -> object:
        """Brings values into a comparable form.

        ``notificationIDList`` comes from the server as a list ``[1, 3]``, but must be a dict
        ``{1: True}`` on write. Without normalization the comparison would pit list against
        dict and forever report "changed".

        Args:
            key: Field name.
            value: Raw value.

        Returns:
            The comparable value.
        """
        if key == "notificationIDList":
            if isinstance(value, dict):
                return sorted(int(k) for k, v in value.items() if v)
            if isinstance(value, list):
                return sorted(int(v) for v in value)
        return value

    @classmethod
    def _diff(cls, desired: Payload, actual: MonitorDict | Payload) -> dict[str, dict[str, object]]:
        """Compares only the fields the caller specifies.

        Args:
            desired: Desired state, may be arbitrarily incomplete.
            actual: Actual state from the server.

        Returns:
            ``{field: {"before": actual, "after": desired}}`` for all differences.
        """
        out: dict[str, dict[str, object]] = {}
        for key, want in desired.items():
            have = cls._normalize(key, actual.get(key))
            want_n = cls._normalize(key, want)
            if have != want_n:
                out[key] = {"before": have, "after": want_n}
        return out

    @staticmethod
    def _wire(payload: Payload) -> Payload:
        """Brings a payload into the form the server expects on write.

        Args:
            payload: Payload with ``notificationIDList`` as a list.

        Returns:
            Payload with ``notificationIDList`` as a dict ``{id: True}`` - the server does
            ``for (let id in ...)``, a list would yield the indices instead of the IDs.
        """
        out = dict(payload)
        ids = out.get("notificationIDList")
        if isinstance(ids, list):
            out["notificationIDList"] = {int(i): True for i in ids}
        return out

    def resolve_parent(self, parent: object) -> int | None:
        """Resolves a parent group by its name.

        IDs are useless for declarative creation - they differ per instance. Hence ``parent``
        may also be a group name.

        Args:
            parent: Group name, monitor ID, or ``None``.

        Returns:
            The monitor ID of the group or ``None``.

        Raises:
            KumaError: If no group of that name exists, the name is ambiguous, or the hit is
                not a ``group`` monitor.
        """
        if parent is None or isinstance(parent, int):
            return parent
        if not isinstance(parent, str):
            raise KumaError(f"parent must be name, id or None, not {type(parent).__name__}")

        hits = [m for m in self.monitors().values() if m.get("name") == parent]
        if not hits:
            raise KumaError(f"parent group {parent!r} does not exist")
        if len(hits) > 1:
            raise KumaError(f"parent name {parent!r} is assigned {len(hits)}x")
        if hits[0].get("type") != "group":
            raise KumaError(f"parent {parent!r} is not a group monitor, but {hits[0].get('type')!r}")
        return hits[0]["id"]

    def upsert_notification(self, desired: Payload, check_mode: bool = False) -> UpsertResult:
        """Creates a notification provider or reconciles it - idempotent.

        The key is ``name``. Unlike monitors there are no non-writable fields: the server
        stores the entire configuration as JSON.

        Args:
            desired: Desired state, must contain at least ``name`` and ``type``.
            check_mode: If True, nothing is written.

        Returns:
            What was changed (or would be).

        Raises:
            KumaError: If ``name`` is missing or assigned more than once.
        """
        name = desired.get("name")
        if not isinstance(name, str):
            raise KumaError("desired needs a 'name' field")

        existing = self.notification_by_name(name)

        if existing is None:
            diff = {k: {"before": None, "after": v} for k, v in desired.items()}
            if check_mode:
                return {"changed": True, "object_id": None, "created": True, "diff": diff}
            return {"changed": True, "object_id": self.save_notification(desired), "created": True, "diff": diff}

        notification_id = int(cast(int, existing["id"]))
        diff = self._diff(desired, cast(MonitorDict, existing))
        if not diff or check_mode:
            return {"changed": bool(diff), "object_id": notification_id, "created": False, "diff": diff}

        # The server replaces the config entirely - so take the actual state as the base and
        # only overwrite the desired fields.
        payload: Payload = {**existing, **desired}
        self.save_notification(payload, notification_id)
        return {"changed": True, "object_id": notification_id, "created": False, "diff": diff}

    def upsert_monitor(self, desired: Payload, check_mode: bool = False) -> UpsertResult:
        """Creates a monitor or reconciles it - idempotent.

        The key is ``name``. The actual state is read fresh from the server before *and* after
        writing, not from a cache.

        ``parent`` may be a group name and ``notifications`` a list of notification names -
        both are resolved against the instance, so the desired state gets by without
        instance-specific IDs.

        Args:
            desired: Desired state, must contain at least ``name``. On creation additionally
                ``type``.
            check_mode: If True, nothing is written - only what would happen is reported.

        Returns:
            What was changed (or would be).

        Raises:
            KumaError: If ``name`` is missing, assigned more than once, or a referenced
                group/notification does not exist.
        """
        name = desired.get("name")
        if not isinstance(name, str):
            raise KumaError("desired needs a 'name' field")

        desired = dict(desired)
        if "parent" in desired:
            desired["parent"] = self.resolve_parent(desired["parent"])
        if "notifications" in desired:
            desired["notificationIDList"] = self._resolve_notifications(desired.pop("notifications"))

        existing = self.monitor_by_name(name)

        if existing is None:
            diff = {k: {"before": None, "after": v} for k, v in desired.items()}
            if check_mode:
                return {"changed": True, "object_id": None, "created": True, "diff": diff}
            return {"changed": True, "object_id": self.add_monitor(self._wire(desired)), "created": True, "diff": diff}

        monitor_id = existing["id"]
        full_diff = self._diff(desired, existing)

        # Split off fields the server doesn't apply via editMonitor at all - otherwise the
        # module reports "changed" on every run without anything ever changing.
        not_applicable = {k: v for k, v in full_diff.items() if k in ADD_ONLY_FIELDS}
        wants_active = full_diff.pop(PAUSE_RESUME_FIELD, None)
        editable_diff = {k: v for k, v in full_diff.items() if k not in ADD_ONLY_FIELDS}

        changed = bool(editable_diff) or wants_active is not None
        result: UpsertResult = {
            "changed": changed,
            "object_id": monitor_id,
            "created": False,
            "diff": dict(editable_diff),
        }
        if wants_active is not None:
            result["diff"][PAUSE_RESUME_FIELD] = wants_active
        if not_applicable:
            result["not_applicable"] = not_applicable

        if check_mode or not changed:
            return result

        if editable_diff:
            # editMonitor assigns fields one by one and expects the full payload -
            # a partial payload would set the remaining fields to undefined.
            payload: Payload = {k: v for k, v in existing.items() if k in EDITABLE_FIELDS}
            payload.update({k: v for k, v in desired.items() if k in EDITABLE_FIELDS})
            payload["id"] = monitor_id
            self.edit_monitor(self._wire(payload))

        if wants_active is not None:
            if wants_active["after"]:
                self.resume_monitor(monitor_id)
            else:
                self.pause_monitor(monitor_id)

        return result
