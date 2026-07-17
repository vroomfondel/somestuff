#!/usr/bin/env python3
"""Export/Import des kompletten Uptime-Kuma-Stands ueber die Socket.IO-API.

Beispiele::

    python3 -m uptimekumastuff.uptimekuma_simpleapi export \\
        --url https://uptimekuma.example.lan --out state.local.json
    python3 -m uptimekumastuff.uptimekuma_simpleapi import \\
        --url http://127.0.0.1:3001 --in-file state.local.json --paused

Warum nicht einfach uptime-kuma-api?
    Die Lib (1.2.1, letztes Release 2023) kann gegen Kuma 2.x lesen, aber nicht schreiben:

    * ``add_monitor()`` -> "NOT NULL constraint failed: monitor.conditions". Kuma 2.x hat
      die Spalte ``conditions`` NOT NULL DEFAULT '[]'; die Lib kennt das Feld nicht, der
      Server schreibt deshalb explizit NULL - und explizites NULL sticht den Default.
    * ``delete_monitor()`` -> "monitor does not exist", weil gegen einen stale lokalen
      Cache validiert wird.
    * ``get_status_page()`` -> KeyError 'incident'; das Feld gibt es in 2.x nicht mehr.

    Reads laufen daher ueber die Lib, wo sie funktionieren, Writes direkt per ``_call()``.
    Nach jedem Write-Lauf mit FRISCHER Session verifizieren - die Session-Caches luegen.

Credentials:
    ``uptimekuma.local.env`` (UPTIME_KUMA_USERNAME / UPTIME_KUMA_PASSWORD) oder echte
    Umgebungsvariablen, die haben Vorrang. Fuer ein abweichendes Ziel-System gibt es
    ``--username`` / ``--password``. Vorlage: ``uptimekuma.env.example``.

Achtung:
    Der Export enthaelt Klartext-Secrets (MQTT-Passwoerter, Telegram-Bot-Token,
    Gotify-Token, SMTP-Passwort). Die Datei gehoert nicht ins Git - Dateiname mit
    ``.local.`` waehlen, das ist gitignored.
"""

import json
import os
import sys
import urllib.request
from enum import Enum
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import typer
from dotenv import load_dotenv
from uptime_kuma_api import UptimeKumaApi

from uptimekumastuff import configure_logging, print_banner

CREDS_FILE = Path(__file__).parent / "uptimekuma.local.env"

# Beim Lesen liefert Kuma 115 Felder, die monitor-Tabelle hat aber nur 111 Spalten.
# Diese hier werden serverseitig abgeleitet bzw. liegen in eigenen Tabellen und wuerden
# bean.import() beim Schreiben auf unbekannte Spalten laufen lassen.
DERIVED_MONITOR_FIELDS: frozenset[str] = frozenset(
    {
        "id",  # vergibt das Ziel-System neu
        "childrenIDs",  # ergibt sich aus parent der Kinder
        "path",  # ergibt sich aus der parent-Kette
        "pathName",  # dito
        "maintenance",  # Laufzeitzustand
        "forceInactive",  # ergibt sich aus dem parent
        "includeSensitiveData",  # Lese-Flag der API
        "screenshot",  # Laufzeit-Artefakt (Spalte heisst screenshot_delay)
        "dns_last_result",  # Laufzeit-Ergebnis
        "tags",  # eigene Tabelle monitor_tag
        "notificationIDList",  # eigene Tabelle monitor_notification, wird remapped
    }
)

# Ein Payload, wie ihn die Socket-API entgegennimmt bzw. liefert. `object` statt `Any`:
# unbekannte Werte muessen so vor Gebrauch verengt werden, statt still durchzurutschen.
type Payload = dict[str, object]


class MonitorTagLink(TypedDict):
    """Zuordnung Tag -> Monitor aus ``monitor["tags"]``.

    Attributes:
        tag_id: ID des Tags in der alten Instanz.
        value: Optionaler Freitextwert der Zuordnung.
    """

    tag_id: int
    value: NotRequired[str | None]


class MonitorDict(TypedDict, total=False):
    """Ein Monitor, wie ``getMonitorList`` ihn liefert.

    Kuma liefert 115 Felder, die sich je Monitor-Typ stark unterscheiden. Hier stehen nur
    die, auf die dieses Script zugreift - alle uebrigen werden beim Import unveraendert
    durchgereicht, ohne dass wir sie kennen muessen.

    Attributes:
        id: Monitor-ID in der Quell-Instanz.
        name: Anzeigename, dient als stabiler Schluessel zwischen den Instanzen.
        type: Monitor-Typ, z.B. ``mqtt``, ``group``, ``http``.
        parent: ID der Eltern-Gruppe oder ``None``.
        active: Ob der Monitor laeuft.
        notificationIDList: Verknuepfte Notification-IDs der Quell-Instanz.
        tags: Tag-Zuordnungen des Monitors.
    """

    id: int
    name: str
    type: str
    parent: int | None
    active: bool
    notificationIDList: list[int]
    tags: list[MonitorTagLink]


class NotificationDict(TypedDict, total=False):
    """Ein Notification-Provider, wie ``getNotificationList`` ihn liefert.

    Die uebrigen Felder haengen vom Provider ab (smtpHost, telegramBotToken, ...) und
    werden unveraendert durchgereicht.

    Attributes:
        id: Notification-ID in der Quell-Instanz.
        name: Anzeigename.
        applyExisting: Einmaliger UI-Trigger, kein Zustand - siehe ``_add_notification``.
    """

    id: int
    name: str
    applyExisting: bool


class TagDict(TypedDict):
    """Ein Tag, wie ``getTags`` ihn liefert.

    Attributes:
        id: Server-seitige Tag-ID.
        name: Anzeigename.
        color: Hex-Farbe, z.B. ``#059669``.
    """

    id: int
    name: str
    color: str


class StatusPageMonitor(TypedDict):
    """Monitor-Eintrag innerhalb einer Status-Page-Gruppe.

    Attributes:
        id: Monitor-ID in der alten Instanz.
        sendUrl: 0/1, ob die URL oeffentlich angezeigt wird.
    """

    id: int
    sendUrl: NotRequired[int]


class StatusPageGroup(TypedDict):
    """Oeffentliche Monitor-Gruppe einer Status Page.

    Attributes:
        name: Gruppenname.
        weight: Sortiergewicht.
        monitorList: Monitore der Gruppe.
    """

    name: str
    weight: NotRequired[int]
    monitorList: list[StatusPageMonitor]


class StatusPageExport(TypedDict):
    """Eine Status Page, wie dieses Script sie exportiert.

    Attributes:
        config: Die Page-Config (slug, title, theme, customCSS, ...).
        publicGroupList: Die oeffentlichen Monitor-Gruppen.
    """

    config: Payload
    publicGroupList: list[StatusPageGroup]


class KumaState(TypedDict):
    """Der komplette exportierte Stand einer Instanz.

    Attributes:
        monitors: Alle Monitore inkl. Gruppen.
        notifications: Alle Notification-Provider.
        tags: Alle Tags.
        status_pages: Alle Status Pages inkl. Gruppen.
    """

    monitors: list[MonitorDict]
    notifications: list[NotificationDict]
    tags: list[TagDict]
    status_pages: list[StatusPageExport]


class ImportReport(TypedDict):
    """Abbildung alte ID -> neue ID je Objekttyp.

    Attributes:
        notifications: Mapping der Notification-IDs.
        tags: Mapping der Tag-IDs.
        monitors: Mapping der Monitor-IDs.
    """

    notifications: dict[int, int]
    tags: dict[int, int]
    monitors: dict[int, int]


def jsonable(obj: object) -> object:
    """Wandelt Lib-Enums (``MonitorType`` & Co.) rekursiv in JSON-taugliche Werte.

    Die uptime-kuma-api gibt ``type`` als ``MonitorType``-Enum zurueck, das ``json.dump``
    nicht serialisieren kann.

    Args:
        obj: Beliebiger Wert aus der uptime-kuma-api.

    Returns:
        Derselbe Wert, aber mit Enums als deren ``.value``.
    """
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [jsonable(v) for v in obj]
    return obj


def load_creds(username: str | None, password: str | None) -> tuple[str, str]:
    """Ermittelt die Zugangsdaten aus CLI-Argumenten, Umgebung oder Creds-Datei.

    Args:
        username: Explizit uebergebener Benutzername oder ``None``.
        password: Explizit uebergebenes Passwort oder ``None``.

    Returns:
        Tupel aus Benutzername und Passwort.

    Raises:
        SystemExit: Wenn weder CLI, Umgebung noch Creds-Datei beides liefern.
    """
    if username and password:
        return username, password
    load_dotenv(CREDS_FILE)
    user = username or os.environ.get("UPTIME_KUMA_USERNAME")
    pw = password or os.environ.get("UPTIME_KUMA_PASSWORD")
    if not user or not pw:
        sys.exit(
            f"Credentials fehlen: UPTIME_KUMA_USERNAME/UPTIME_KUMA_PASSWORD "
            f"(weder in der Umgebung noch in {CREDS_FILE})"
        )
    return user, pw


class SimpleKumaApi:
    """Duenne Huelle um uptime-kuma-api: Reads via Lib, Writes via ``_call()``.

    Attributes:
        url: Basis-URL der Instanz.
        api: Die zugrundeliegende uptime-kuma-api-Session.
    """

    def __init__(self, url: str, username: str, password: str, timeout: int = 30) -> None:
        """Verbindet sich und loggt ein.

        Args:
            url: Basis-URL, z.B. ``http://127.0.0.1:3001``.
            username: Benutzername (der Socket-Login akzeptiert keine API-Keys).
            password: Passwort.
            timeout: Socket-Timeout in Sekunden.
        """
        self.url = url.rstrip("/")
        self.api = UptimeKumaApi(self.url, timeout=timeout)
        self.api.login(username, password)

    def close(self) -> None:
        """Trennt die Socket-Verbindung."""
        self.api.disconnect()

    # ---------------------------------------------------------------- export

    def _export_status_page(self, slug: str) -> StatusPageExport:
        """Liest eine Status Page vollstaendig aus.

        ``api.get_status_page()`` ist gegen 2.x kaputt (KeyError 'incident'), und der rohe
        Socket-Call liefert nur die Config - die Monitor-Gruppen haengen am oeffentlichen
        HTTP-Endpoint.

        Args:
            slug: Slug der Status Page.

        Returns:
            Config und oeffentliche Gruppenliste.
        """
        config = cast(Payload, self.api._call("getStatusPage", slug)["config"])
        with urllib.request.urlopen(f"{self.url}/api/status-page/{slug}") as resp:
            public = json.loads(resp.read())
        return {
            "config": config,
            "publicGroupList": public.get("publicGroupList") or [],
        }

    def export_state(self) -> KumaState:
        """Zieht Monitore, Notifications, Tags und Status Pages aus der Instanz.

        Returns:
            Der komplette Stand, JSON-serialisierbar.
        """
        pages = [self._export_status_page(str(s["slug"])) for s in self.api.get_status_pages()]
        return {
            "monitors": cast(list[MonitorDict], jsonable(self.api.get_monitors())),
            "notifications": cast(list[NotificationDict], jsonable(self.api.get_notifications())),
            "tags": cast(list[TagDict], jsonable(self.api.get_tags())),
            "status_pages": cast(list[StatusPageExport], jsonable(pages)),
        }

    # ---------------------------------------------------------------- import

    def _add_notification(self, notification: NotificationDict) -> int:
        """Legt einen Notification-Provider an.

        ``applyExisting`` wird bewusst hart auf False gesetzt: Es ist kein Zustand, sondern
        ein einmaliger UI-Trigger ("an alle bestehenden Monitore haengen"). Der Server
        normalisiert es beim Speichern selbst auf False, wertet es davor aber aus - stuende
        es auf True und gaebe es schon Monitore, wuerde die Notification an *alle* gehaengt
        und die exakten Verknuepfungen ueberschrieben. In alten Prod-DBs steht dort noch True.

        Args:
            notification: Notification-Dict aus dem Export.

        Returns:
            Die neue Notification-ID.
        """
        payload: Payload = {k: v for k, v in notification.items() if k not in ("id", "userId")}
        payload["applyExisting"] = False
        result = self.api._call("addNotification", (payload, None))
        return int(result["id"])

    def _add_tag(self, tag: TagDict) -> int:
        """Legt einen Tag an.

        Args:
            tag: Tag-Dict aus dem Export.

        Returns:
            Die neue Tag-ID.
        """
        result = self.api._call("addTag", {"name": tag["name"], "color": tag["color"]})
        return int(result["tag"]["id"])

    def _add_monitor(
        self,
        monitor: MonitorDict,
        notif_map: dict[int, int],
        parent_map: dict[int, int],
        paused: bool,
    ) -> int:
        """Legt einen Monitor an und biegt dabei alle Fremd-IDs um.

        Args:
            monitor: Monitor-Dict aus dem Export.
            notif_map: Mapping alte -> neue Notification-ID.
            parent_map: Mapping alte -> neue Monitor-ID (fuer ``parent``).
            paused: Wenn True, wird der Monitor inaktiv angelegt.

        Returns:
            Die neue Monitor-ID.
        """
        data: Payload = {k: v for k, v in monitor.items() if k not in DERIVED_MONITOR_FIELDS}

        parent = monitor.get("parent")
        if parent:
            data["parent"] = parent_map[parent]

        # Der Server macht `for (let id in notificationIDList)` und prueft auf truthy.
        # Bei einer Liste [1, 3] waeren das die Indizes 0, 1 -> falsche Verknuepfung.
        # Es muss ein Dict {neue_id: True} sein.
        data["notificationIDList"] = {
            notif_map[old]: True for old in monitor.get("notificationIDList") or [] if old in notif_map
        }

        if paused:
            data["active"] = False  # verhindert serverseitig startMonitor()

        result = self.api._call("add", data)
        return int(result["monitorID"])

    def _add_status_page(self, page: StatusPageExport, monitor_map: dict[int, int]) -> None:
        """Legt eine Status Page an und verknuepft ihre Monitor-Gruppen neu.

        Args:
            page: Status Page aus dem Export.
            monitor_map: Mapping alte -> neue Monitor-ID.
        """
        cfg = page["config"]
        slug = str(cfg["slug"])
        self.api._call("addStatusPage", (cfg["title"], slug))

        groups: list[StatusPageGroup] = []
        for group in page["publicGroupList"]:
            monitors: list[StatusPageMonitor] = [
                {"id": monitor_map[mon["id"]], "sendUrl": mon.get("sendUrl", 0)}
                for mon in group["monitorList"]
                if mon["id"] in monitor_map
            ]
            groups.append({"name": group["name"], "weight": group.get("weight", 1), "monitorList": monitors})

        # imgDataUrl muss ein String sein - der Server ruft blind .startsWith("data:")
        # darauf auf. Ist es keine data-URI, wird der Wert als config.logo uebernommen;
        # wir reichen deshalb den bestehenden Icon-Pfad durch (so macht es das Frontend).
        icon = cfg.get("icon") or "/icon.svg"
        self.api._call("saveStatusPage", (slug, cfg, icon, groups))

    @staticmethod
    def parents_first(monitors: list[MonitorDict]) -> list[MonitorDict]:
        """Sortiert Monitore so, dass jede Gruppe vor ihren Kindern kommt.

        Kuma erlaubt Gruppen in Gruppen, deshalb topologisch statt nur "groups first".

        Args:
            monitors: Alle Monitore aus dem Export.

        Returns:
            Dieselben Monitore, Eltern vor Kindern.

        Raises:
            RuntimeError: Bei einem Zyklus in der Hierarchie - lieber abbrechen, als
                stillschweigend Monitore zu verlieren.
        """
        by_id = {m["id"]: m for m in monitors}
        ordered: list[MonitorDict] = []
        placed: set[int] = set()

        while len(ordered) < len(monitors):
            progress = False
            for monitor in monitors:
                if monitor["id"] in placed:
                    continue
                parent = monitor.get("parent")
                if not parent or parent in placed or parent not in by_id:
                    ordered.append(monitor)
                    placed.add(monitor["id"])
                    progress = True
            if not progress:
                stuck = [m["name"] for m in monitors if m["id"] not in placed]
                raise RuntimeError(f"Zyklus in der Gruppen-Hierarchie, haengen geblieben bei: {stuck}")
        return ordered

    def import_state(self, state: KumaState, paused: bool = False) -> ImportReport:
        """Schreibt einen exportierten Stand in die Instanz.

        Reihenfolge ist zwingend: Notifications und Tags zuerst (Monitore referenzieren
        sie), dann Monitore Eltern-vor-Kind, dann Tag-Zuordnungen, dann Status Pages.

        Args:
            state: Der zu schreibende Stand.
            paused: Wenn True, werden alle Monitore inaktiv angelegt - keine Checks,
                keine Alarme. Fuer Testinstanzen dringend empfohlen.

        Returns:
            Die ID-Mappings alt -> neu.
        """
        report: ImportReport = {"notifications": {}, "tags": {}, "monitors": {}}

        for notification in state["notifications"]:
            report["notifications"][notification["id"]] = self._add_notification(notification)
        typer.echo(f"notifications: {len(report['notifications'])} angelegt")

        for tag in state["tags"]:
            report["tags"][tag["id"]] = self._add_tag(tag)
        typer.echo(f"tags: {len(report['tags'])} angelegt")

        for monitor in self.parents_first(state["monitors"]):
            report["monitors"][monitor["id"]] = self._add_monitor(
                monitor, report["notifications"], report["monitors"], paused
            )
        typer.echo(f"monitore: {len(report['monitors'])} angelegt{' (pausiert)' if paused else ''}")

        links = 0
        for monitor in state["monitors"]:
            for link in monitor.get("tags") or []:
                self.api._call(
                    "addMonitorTag",
                    (
                        report["tags"][link["tag_id"]],
                        report["monitors"][monitor["id"]],
                        link.get("value") or "",
                    ),
                )
                links += 1
        if links:
            typer.echo(f"tag-zuordnungen: {links} angelegt")

        for page in state["status_pages"]:
            self._add_status_page(page, report["monitors"])
        if state["status_pages"]:
            typer.echo(f"status pages: {len(state['status_pages'])} angelegt")

        return report


app = typer.Typer(add_completion=False, help=__doc__)


@app.command("export")
def export_cmd(
    url: str = typer.Option(..., help="Basis-URL der Quell-Instanz"),
    out: Path = typer.Option(..., help="Zieldatei fuer den JSON-Export"),
    username: str | None = typer.Option(None, help="ueberschreibt uptimekuma.local.env"),
    password: str | None = typer.Option(None, help="ueberschreibt uptimekuma.local.env"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG-Logging"),
) -> None:
    """Zieht den kompletten Stand einer Instanz in eine JSON-Datei.

    Args:
        url: Basis-URL der Quell-Instanz.
        out: Pfad der zu schreibenden JSON-Datei.
        username: Optionaler Benutzername, sonst aus Umgebung/Creds-Datei.
        password: Optionales Passwort, sonst aus Umgebung/Creds-Datei.
        verbose: Wenn True, wird auf DEBUG-Level geloggt.
    """
    configure_logging(verbose=verbose)
    print_banner()

    user, pw = load_creds(username, password)
    api = SimpleKumaApi(url, user, pw)
    try:
        state = api.export_state()
    finally:
        api.close()

    out.write_text(json.dumps(state, indent=1))
    typer.echo(
        f"export -> {out}: {len(state['monitors'])} monitore, "
        f"{len(state['notifications'])} notifications, {len(state['tags'])} tags, "
        f"{len(state['status_pages'])} status pages"
    )
    typer.secho("ACHTUNG: enthaelt Klartext-Secrets - nicht committen.", fg=typer.colors.YELLOW)


@app.command("import")
def import_cmd(
    url: str = typer.Option(..., help="Basis-URL der Ziel-Instanz"),
    in_file: Path = typer.Option(..., "--in-file", help="JSON-Export, der geschrieben wird"),
    paused: bool = typer.Option(False, help="Monitore inaktiv anlegen: keine Checks, keine Alarme"),
    dry_run: bool = typer.Option(False, help="nur zeigen, was angelegt wuerde"),
    username: str | None = typer.Option(None, help="Creds der ZIEL-Instanz"),
    password: str | None = typer.Option(None, help="Creds der ZIEL-Instanz"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG-Logging"),
) -> None:
    """Schreibt einen JSON-Export in eine (leere) Instanz.

    Args:
        url: Basis-URL der Ziel-Instanz.
        in_file: Pfad des einzulesenden JSON-Exports.
        paused: Monitore inaktiv anlegen.
        dry_run: Nichts schreiben, nur die geplanten Objekte ausgeben.
        username: Optionaler Benutzername der Ziel-Instanz.
        password: Optionales Passwort der Ziel-Instanz.
        verbose: Wenn True, wird auf DEBUG-Level geloggt.
    """
    configure_logging(verbose=verbose)
    print_banner()

    state = cast(KumaState, json.loads(in_file.read_text()))

    if dry_run:
        groups = [m for m in state["monitors"] if m.get("type") == "group"]
        typer.echo(
            f"[dry-run] wuerde anlegen: {len(state['notifications'])} notifications, "
            f"{len(state['tags'])} tags, {len(state['monitors'])} monitore "
            f"({len(groups)} davon gruppen), {len(state['status_pages'])} status pages"
        )
        return

    user, pw = load_creds(username, password)
    api = SimpleKumaApi(url, user, pw)
    try:
        api.import_state(state, paused=paused)
    finally:
        api.close()


if __name__ == "__main__":
    app()
