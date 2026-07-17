"""Direkter Socket.IO-Client fuer Uptime Kuma 2.x - ohne uptime-kuma-api.

Das Fundament fuer idempotentes Provisionieren: lesen -> vergleichen -> schreiben -> neu lesen.
Nutzer ist :mod:`uptimekumastuff.uptimekuma_apply`; als Bibliothek direkt verwendbar.

Warum nicht uptime-kuma-api?
    Die Lib liest einen **Cache**, nicht den Server::

        # uptime_kuma_api/api.py, get_monitors()
        # TODO: replace with getMonitorList?
        r = list(self._get_event_data(Event.MONITOR_LIST).values())

    Sie ruft ``getMonitorList`` nie auf. Der Snapshot stammt vom Login-Push und wird nur
    passiv fortgeschrieben - nach eigenen Writes ist er veraltet. Genau daran ist
    ``delete_monitor()`` gescheitert ("monitor does not exist", obwohl er existierte).
    Fuer Idempotenz und check_mode ist das disqualifizierend: der gelesene Ist-Zustand
    muss echt sein.

    Dieser Client ruft ``getMonitorList`` und wartet auf den daraufhin gesendeten
    ``monitorList``-Push. python-socketio ist dabei keine neue Abhaengigkeit - die Lib
    baut selbst darauf auf.

Bekannte Server-Eigenheiten (alle am Server-Code von 2.4.0 verifiziert):
    * ``add`` braucht ``conditions`` (NOT NULL); fehlt es, schreibt der Server NULL.
    * ``notificationIDList`` muss beim Schreiben ein Dict ``{id: True}`` sein - der Server
      macht ``for (let id in ...)``, eine Liste liefert die Indizes statt der IDs.
    * ``active`` und ``weight`` liest ``editMonitor`` **nicht**: ``active`` laeuft ueber
      ``pauseMonitor``/``resumeMonitor``, ``weight`` ist nur beim Anlegen setzbar.
    * ``add`` mit ``active: false`` verhindert serverseitig ``startMonitor()``.
"""

import json
import threading
from collections.abc import Callable
from typing import NotRequired, Self, TypedDict, cast

import socketio

# Felder, die editMonitor aus dem Payload liest (aus server.js 2.4.0 extrahiert).
# Alles andere ignoriert er stillschweigend - ein Diff darauf wuerde ewig "changed" melden.
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

# Wird nur beim Anlegen uebernommen. editMonitor liest es nicht - eine Aenderung waere
# ein stiller No-Op, deshalb melden wir sie als nicht anwendbar statt als "changed".
ADD_ONLY_FIELDS: frozenset[str] = frozenset({"weight"})

# Laeuft nicht ueber editMonitor, sondern ueber pauseMonitor/resumeMonitor.
PAUSE_RESUME_FIELD = "active"

type Payload = dict[str, object]


class KumaError(Exception):
    """Der Server hat einen Call mit ``ok: false`` beantwortet."""


class MonitorDict(TypedDict, total=False):
    """Ein Monitor, wie ``monitorList`` ihn liefert.

    Kuma liefert 115 Felder je nach Monitor-Typ. Hier stehen nur die, auf die dieser
    Client zugreift; der Rest wird unveraendert durchgereicht.

    Attributes:
        id: Monitor-ID.
        name: Anzeigename.
        type: Monitor-Typ, z.B. ``mqtt``, ``group``, ``http``.
        parent: ID der Eltern-Gruppe oder ``None``.
        active: Ob der Monitor laeuft.
        weight: Sortiergewicht (nur beim Anlegen setzbar).
        notificationIDList: Verknuepfte Notification-IDs.
    """

    id: int
    name: str
    type: str
    parent: int | None
    active: bool
    weight: int
    notificationIDList: list[int]


class UpsertResult(TypedDict):
    """Ergebnis eines idempotenten Upserts, im Zuschnitt eines Ansible-Rueckgabewerts.

    Attributes:
        changed: Ob etwas geaendert wurde (bzw. in check_mode: wuerde).
        object_id: ID des angelegten/gefundenen Objekts, ``None`` nur in check_mode
            beim Neuanlegen.
        created: True, wenn das Objekt neu angelegt wurde.
        diff: Felder, die abweichen, als ``{feld: {"before": ..., "after": ...}}``.
        not_applicable: Felder, die abweichen, die der Server aber gar nicht uebernimmt
            (siehe ADD_ONLY_FIELDS).
    """

    changed: bool
    object_id: int | None
    created: bool
    diff: dict[str, dict[str, object]]
    not_applicable: NotRequired[dict[str, dict[str, object]]]


class KumaClient:
    """Socket.IO-Client mit garantiert frischen Reads.

    Attributes:
        url: Basis-URL der Instanz.
        timeout: Sekunden, die auf Antworten und Pushes gewartet wird.
    """

    #: Events, die der Server von sich aus pusht und die wir mitschneiden.
    PUSHED_EVENTS = ("monitorList", "notificationList")

    def __init__(self, url: str, timeout: int = 30) -> None:
        """Legt den Client an, ohne schon zu verbinden.

        Args:
            url: Basis-URL, z.B. ``http://127.0.0.1:3001``.
            timeout: Timeout fuer Calls und erwartete Pushes.
        """
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.sio = socketio.Client()
        self._data: dict[str, Payload] = {}
        self._arrived: dict[str, threading.Event] = {n: threading.Event() for n in self.PUSHED_EVENTS}
        for name in self.PUSHED_EVENTS:
            self.sio.on(name, self._make_handler(name))

    def _make_handler(self, name: str) -> Callable[[Payload], None]:
        """Baut den Handler, der einen gepushten Event mitschneidet.

        Args:
            name: Event-Name.

        Returns:
            Die Handler-Funktion fuer python-socketio.
        """

        def handler(data: Payload) -> None:
            self._data[name] = data
            self._arrived[name].set()

        return handler

    def __enter__(self) -> Self:
        """Verbindet beim Betreten des with-Blocks.

        Returns:
            Der verbundene Client.
        """
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        """Trennt die Verbindung beim Verlassen des with-Blocks."""
        self.close()

    def connect(self) -> None:
        """Baut die Socket.IO-Verbindung auf (websocket, kein polling-Umweg)."""
        self.sio.connect(self.url, transports=["websocket"], wait_timeout=self.timeout)

    def close(self) -> None:
        """Trennt die Verbindung, ohne bei bereits toter Verbindung zu werfen."""
        try:
            self.sio.disconnect()
        except Exception:
            pass

    def call(self, event: str, *args: object) -> Payload:
        """Sendet einen Socket-Call und packt die Server-Antwort aus.

        Args:
            event: Event-Name, z.B. ``add``.
            *args: Argumente des Handlers.

        Returns:
            Die Antwort des Servers.

        Raises:
            KumaError: Wenn der Server ``ok: false`` meldet.
        """
        data = args[0] if len(args) == 1 else tuple(args)
        result = cast(Payload, self.sio.call(event, data, timeout=self.timeout))
        if isinstance(result, dict) and result.get("ok") is False:
            raise KumaError(f"{event}: {result.get('msg')}")
        return result

    def login(self, username: str, password: str) -> None:
        """Meldet sich an.

        Der Socket-Login akzeptiert ausschliesslich Benutzername+Passwort. API-Keys
        (``uk1_``/``uk2_``/``uk3_``) gelten nur fuer HTTP-Basic-Auth auf ``/metrics``.

        Args:
            username: Benutzername.
            password: Passwort.

        Raises:
            KumaError: Bei falschen Zugangsdaten.
        """
        self.call("login", {"username": username, "password": password, "token": None})

    def need_setup(self) -> bool:
        """Prueft, ob die Instanz noch keinen Benutzer hat.

        Returns:
            True, wenn ``setup()`` noch aussteht.
        """
        return bool(self.sio.call("needSetup", timeout=self.timeout))

    def setup(self, username: str, password: str) -> None:
        """Legt den ersten Benutzer einer frischen Instanz an.

        Args:
            username: Gewuenschter Benutzername.
            password: Gewuenschtes Passwort (Kuma lehnt zu schwache ab).
        """
        self.call("setup", username, password)

    # ------------------------------------------------------------------ reads

    def _await_push(self, event: str) -> Payload:
        """Wartet auf den naechsten Push eines Events.

        Args:
            event: Event-Name aus :attr:`PUSHED_EVENTS`.

        Returns:
            Die gepushten Daten.

        Raises:
            KumaError: Wenn innerhalb des Timeouts nichts kommt.
        """
        if not self._arrived[event].wait(self.timeout):
            raise KumaError(f"kein {event}-push innerhalb von {self.timeout}s")
        return self._data[event]

    def monitors(self) -> dict[int, MonitorDict]:
        """Liest alle Monitore **frisch vom Server**.

        Ruft ``getMonitorList``, was serverseitig ``sendMonitorList`` ausloest, und wartet
        auf den daraufhin gesendeten Push. Genau das macht uptime-kuma-api nicht.

        ``notificationIDList`` liefert der Server roh als Dict mit String-Keys
        (``{"1": true}``); wir vereinheitlichen das zu einer sortierten ID-Liste, damit
        Lesen und Soll-Zustand dieselbe Form haben. :meth:`_wire` dreht es beim Schreiben
        wieder zurueck.

        Returns:
            Alle Monitore, nach ID.
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
        """Sucht einen Monitor ueber seinen Namen.

        Args:
            name: Anzeigename.

        Returns:
            Der Monitor oder ``None``.

        Raises:
            KumaError: Wenn der Name mehrfach vergeben ist - Kuma laesst das zu, fuer
                idempotentes Arbeiten ist er dann aber kein brauchbarer Schluessel.
        """
        hits = [m for m in self.monitors().values() if m.get("name") == name]
        if len(hits) > 1:
            raise KumaError(f"name {name!r} ist {len(hits)}x vergeben - kein eindeutiger schluessel")
        return hits[0] if hits else None

    def notifications(self) -> list[Payload]:
        """Liest die Notification-Provider, mit ausgepackter config.

        Fuer diese Liste gibt es kein Request-Event; der Server pusht sie beim Login und
        nach jeder Mutation (``sendNotificationList``). Wir lesen daher den zuletzt
        gepushten Stand.

        Der Server pusht rohe DB-Zeilen, in denen ``config`` ein **JSON-String** mit den
        eigentlichen Provider-Feldern ist. Wir packen ihn aus, damit sich flach
        vergleichen laesst - und weil ``addNotification`` genau die flache Form erwartet.

        Returns:
            Alle Notification-Provider als flache Dicts.
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
        """Sucht einen Notification-Provider ueber seinen Namen.

        Args:
            name: Anzeigename.

        Returns:
            Der Provider oder ``None``.

        Raises:
            KumaError: Wenn der Name mehrfach vergeben ist.
        """
        hits = [n for n in self.notifications() if n.get("name") == name]
        if len(hits) > 1:
            raise KumaError(f"notification-name {name!r} ist {len(hits)}x vergeben")
        return hits[0] if hits else None

    def tags(self) -> list[Payload]:
        """Liest alle Tags (echtes Request/Response-Event).

        Returns:
            Alle Tags.
        """
        return cast(list[Payload], self.call("getTags")["tags"])

    # ----------------------------------------------------------------- writes

    def save_notification(self, notification: Payload, notification_id: int | None = None) -> int:
        """Legt einen Notification-Provider an oder aktualisiert ihn.

        ``addNotification`` ist beides: mit ``notification_id`` aktualisiert der Server den
        bestehenden Datensatz, ohne legt er einen neuen an.

        ``applyExisting`` wird hart auf False gesetzt. Es ist kein Zustand, sondern ein
        einmaliger UI-Trigger - stuende es auf True, haengte Kuma die Notification an
        *alle* bestehenden Monitore und zerstoerte die exakten Verknuepfungen. Der Server
        normalisiert es beim Speichern auf False, wertet es davor aber aus.

        Args:
            notification: Flacher Provider-Payload.
            notification_id: ID fuer ein Update, ``None`` fuer ein Neuanlegen.

        Returns:
            Die ID des angelegten/aktualisierten Providers.
        """
        payload = {k: v for k, v in notification.items() if k not in ("id", "userId", "user_id")}
        payload["applyExisting"] = False
        return int(cast(int, self.call("addNotification", payload, notification_id)["id"]))

    def delete_notification(self, notification_id: int) -> None:
        """Loescht einen Notification-Provider.

        Args:
            notification_id: ID des Providers.
        """
        self.call("deleteNotification", notification_id)

    def add_monitor(self, monitor: Payload) -> int:
        """Legt einen Monitor an.

        Args:
            monitor: Vollstaendiger Monitor-Payload. ``conditions`` und
                ``accepted_statuscodes`` muessen gesetzt sein.

        Returns:
            Die neue Monitor-ID.
        """
        return int(cast(int, self.call("add", monitor)["monitorID"]))

    def edit_monitor(self, monitor: Payload) -> None:
        """Aendert einen Monitor.

        Args:
            monitor: Payload inklusive ``id``. ``active`` und ``weight`` werden vom Server
                ignoriert (siehe Modul-Docstring).
        """
        self.call("editMonitor", monitor)

    def delete_monitor(self, monitor_id: int) -> None:
        """Loescht einen Monitor.

        Args:
            monitor_id: ID des Monitors.
        """
        self.call("deleteMonitor", monitor_id)

    def pause_monitor(self, monitor_id: int) -> None:
        """Pausiert einen Monitor.

        Args:
            monitor_id: ID des Monitors.
        """
        self.call("pauseMonitor", monitor_id)

    def resume_monitor(self, monitor_id: int) -> None:
        """Startet einen pausierten Monitor.

        Args:
            monitor_id: ID des Monitors.
        """
        self.call("resumeMonitor", monitor_id)

    # ----------------------------------------------------------------- upsert

    def _resolve_notifications(self, names: object) -> list[int]:
        """Loest Notification-Namen zu IDs auf.

        Args:
            names: Liste von Notification-Namen (oder bereits IDs).

        Returns:
            Die IDs, sortiert - vergleichbar mit dem, was der Server liefert.

        Raises:
            KumaError: Wenn eine Notification nicht existiert.
        """
        if not isinstance(names, list):
            raise KumaError("'notifications' muss eine liste von namen sein")
        by_name = {n["name"]: int(cast(int, n["id"])) for n in self.notifications()}
        ids: list[int] = []
        for entry in names:
            if isinstance(entry, int):
                ids.append(entry)
            elif entry in by_name:
                ids.append(by_name[cast(str, entry)])
            else:
                raise KumaError(f"notification {entry!r} existiert nicht")
        return sorted(ids)

    @staticmethod
    def _normalize(key: str, value: object) -> object:
        """Bringt Werte in eine vergleichbare Form.

        ``notificationIDList`` kommt vom Server als Liste ``[1, 3]``, muss beim Schreiben
        aber ein Dict ``{1: True}`` sein. Ohne Normalisierung wuerde der Vergleich Liste
        gegen Dict stellen und ewig "changed" melden.

        Args:
            key: Feldname.
            value: Roher Wert.

        Returns:
            Der vergleichbare Wert.
        """
        if key == "notificationIDList":
            if isinstance(value, dict):
                return sorted(int(k) for k, v in value.items() if v)
            if isinstance(value, list):
                return sorted(int(v) for v in value)
        return value

    @classmethod
    def _diff(cls, desired: Payload, actual: MonitorDict | Payload) -> dict[str, dict[str, object]]:
        """Vergleicht nur die Felder, die der Aufrufer vorgibt.

        Args:
            desired: Soll-Zustand, darf beliebig unvollstaendig sein.
            actual: Ist-Zustand vom Server.

        Returns:
            ``{feld: {"before": ist, "after": soll}}`` fuer alle Abweichungen.
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
        """Bringt einen Payload in die Form, die der Server beim Schreiben erwartet.

        Args:
            payload: Payload mit ``notificationIDList`` als Liste.

        Returns:
            Payload mit ``notificationIDList`` als Dict ``{id: True}`` - der Server macht
            ``for (let id in ...)``, eine Liste lieferte die Indizes statt der IDs.
        """
        out = dict(payload)
        ids = out.get("notificationIDList")
        if isinstance(ids, list):
            out["notificationIDList"] = {int(i): True for i in ids}
        return out

    def resolve_parent(self, parent: object) -> int | None:
        """Loest eine Eltern-Gruppe ueber ihren Namen auf.

        IDs sind fuer deklaratives Anlegen unbrauchbar - sie unterscheiden sich je Instanz.
        Deshalb darf ``parent`` auch ein Gruppenname sein.

        Args:
            parent: Gruppenname, Monitor-ID oder ``None``.

        Returns:
            Die Monitor-ID der Gruppe oder ``None``.

        Raises:
            KumaError: Wenn keine Gruppe des Namens existiert, der Name mehrdeutig ist
                oder der Treffer kein ``group``-Monitor ist.
        """
        if parent is None or isinstance(parent, int):
            return parent
        if not isinstance(parent, str):
            raise KumaError(f"parent muss name, id oder None sein, nicht {type(parent).__name__}")

        hits = [m for m in self.monitors().values() if m.get("name") == parent]
        if not hits:
            raise KumaError(f"parent-gruppe {parent!r} existiert nicht")
        if len(hits) > 1:
            raise KumaError(f"parent-name {parent!r} ist {len(hits)}x vergeben")
        if hits[0].get("type") != "group":
            raise KumaError(f"parent {parent!r} ist kein group-monitor, sondern {hits[0].get('type')!r}")
        return hits[0]["id"]

    def upsert_notification(self, desired: Payload, check_mode: bool = False) -> UpsertResult:
        """Legt einen Notification-Provider an oder gleicht ihn ab - idempotent.

        Schluessel ist ``name``. Anders als bei Monitoren gibt es keine nicht-schreibbaren
        Felder: der Server legt die gesamte Konfiguration als JSON ab.

        Args:
            desired: Soll-Zustand, muss mindestens ``name`` und ``type`` enthalten.
            check_mode: Wenn True, wird nichts geschrieben.

        Returns:
            Was geaendert wurde bzw. wuerde.

        Raises:
            KumaError: Wenn ``name`` fehlt oder mehrfach vergeben ist.
        """
        name = desired.get("name")
        if not isinstance(name, str):
            raise KumaError("desired braucht ein 'name'-feld")

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

        # Der Server ersetzt die config komplett - deshalb Ist-Zustand als Basis nehmen
        # und nur die gewuenschten Felder ueberschreiben.
        payload: Payload = {**existing, **desired}
        self.save_notification(payload, notification_id)
        return {"changed": True, "object_id": notification_id, "created": False, "diff": diff}

    def upsert_monitor(self, desired: Payload, check_mode: bool = False) -> UpsertResult:
        """Legt einen Monitor an oder gleicht ihn ab - idempotent.

        Schluessel ist ``name``. Der Ist-Zustand wird vor *und* nach dem Schreiben frisch
        vom Server gelesen, nicht aus einem Cache.

        ``parent`` darf ein Gruppenname sein und ``notifications`` eine Liste von
        Notification-Namen - beides wird gegen die Instanz aufgeloest, damit der
        Soll-Zustand ohne instanzspezifische IDs auskommt.

        Args:
            desired: Soll-Zustand, muss mindestens ``name`` enthalten. Beim Neuanlegen
                zusaetzlich ``type``.
            check_mode: Wenn True, wird nichts geschrieben - nur berichtet, was passieren
                wuerde.

        Returns:
            Was geaendert wurde bzw. wuerde.

        Raises:
            KumaError: Wenn ``name`` fehlt, mehrfach vergeben ist, oder eine referenzierte
                Gruppe/Notification nicht existiert.
        """
        name = desired.get("name")
        if not isinstance(name, str):
            raise KumaError("desired braucht ein 'name'-feld")

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

        # Felder trennen, die der Server per editMonitor gar nicht uebernimmt - sonst
        # meldet das Modul bei jedem Lauf "changed", ohne dass sich je etwas aendert.
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
            # editMonitor weist Felder einzeln zu und erwartet den vollen Payload -
            # ein Teil-Payload wuerde die uebrigen Felder auf undefined setzen.
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
