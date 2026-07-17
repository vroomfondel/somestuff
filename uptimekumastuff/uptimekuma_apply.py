"""Wendet einen deklarativen Soll-Zustand aus einer YAML-Datei auf Uptime Kuma an.

Anders als ``uptimekuma_simpleapi`` (kompletter Export/Import in eine leere Instanz) gleicht
dieses Modul einen Teil-Soll-Zustand idempotent gegen eine bestehende Instanz ab - mehrfach
ausfuehrbar, mit ``--check`` als Trockenlauf.

    python3 -m uptimekumastuff.uptimekuma_apply --file kuma_state.local.yml
    python3 -m uptimekumastuff.uptimekuma_apply --file kuma_state.local.yml --check
    python3 -m uptimekumastuff.uptimekuma_apply --file kuma_state.local.yml --prune

Aufbau der YAML::

    url: https://uptimekuma.example.lan

    notifications:
      - name: Mein Gotify Alarm (1)
        type: gotify
        gotifyserverurl: https://gotify.example.org
        gotifyapplicationToken: "{{ token }}"
        gotifyPriority: 8

    monitors:
      - name: outdoormesh
        type: group
      - name: husqvarna/automower/pongs
        type: mqtt
        parent: outdoormesh              # Name, keine ID
        notifications: [Mein Gotify Alarm (1)]
        hostname: mosquitto.mosquitto.svc.cluster.local
        port: 1883
        mqttTopic: husqvarna/automower/pongs
        active: true

Feldnamen sind die von Kuma (camelCase), so passt ein Export aus ``uptimekuma_simpleapi``
ohne Uebersetzung hier hinein.

Credentials: ``uptimekuma.local.env`` bzw. UPTIME_KUMA_USERNAME/UPTIME_KUMA_PASSWORD.
Vorlage: ``uptimekuma.env.example``.

Achtung:
    Die YAML enthaelt in der Regel Klartext-Secrets (MQTT-Passwoerter, Provider-Token).
    Sie gehoert nicht ins Git - Dateiname mit ``.local.`` waehlen, das ist gitignored.
"""

import os
import sys
from pathlib import Path
from typing import TypedDict, cast

import typer
import yaml
from dotenv import load_dotenv

from uptimekumastuff import configure_logging, print_banner
from uptimekumastuff.uptimekuma_client import KumaClient, KumaError

CREDS_FILE = Path(__file__).parent / "uptimekuma.local.env"

# Beim Anlegen verlangt der Server diese Felder; `conditions` ist NOT NULL und ein
# explizites NULL sticht den DB-Default.
CREATE_DEFAULTS: dict[str, object] = {
    "conditions": [],
    "accepted_statuscodes": ["200-299"],
    "interval": 60,
    "retryInterval": 60,
    "resendInterval": 0,
    "maxretries": 0,
    "timeout": 48,
    "upsideDown": False,
    "expiryNotification": False,
    "ignoreTls": False,
    "notificationIDList": [],
}


class DesiredState(TypedDict, total=False):
    """Der Soll-Zustand aus der YAML.

    Attributes:
        url: Basis-URL der Instanz. Per --url ueberschreibbar.
        notifications: Notification-Provider, flach in Kuma-Feldnamen.
        monitors: Monitore, in Kuma-Feldnamen; ``parent``/``notifications`` per Name.
    """

    url: str
    notifications: list[dict[str, object]]
    monitors: list[dict[str, object]]


def load_creds() -> tuple[str, str]:
    """Ermittelt die Zugangsdaten aus Umgebung oder Creds-Datei.

    Returns:
        Benutzername und Passwort.

    Raises:
        SystemExit: Wenn beides fehlt.
    """
    load_dotenv(CREDS_FILE)
    user = os.environ.get("UPTIME_KUMA_USERNAME")
    pw = os.environ.get("UPTIME_KUMA_PASSWORD")
    if not user or not pw:
        sys.exit(f"UPTIME_KUMA_USERNAME/UPTIME_KUMA_PASSWORD fehlen (weder Umgebung noch {CREDS_FILE})")
    return user, pw


def order_parents_first(monitors: list[dict[str, object]]) -> list[dict[str, object]]:
    """Sortiert Monitore so, dass Gruppen vor ihren Kindern angelegt werden.

    Gruppen sind schachtelbar, deshalb topologisch statt nur "groups first".

    Args:
        monitors: Monitore aus der YAML.

    Returns:
        Dieselben Monitore, Eltern vor Kindern.

    Raises:
        KumaError: Bei einem Zyklus in der Hierarchie.
    """
    names = {cast(str, m["name"]) for m in monitors}
    ordered: list[dict[str, object]] = []
    placed: set[str] = set()

    while len(ordered) < len(monitors):
        progress = False
        for monitor in monitors:
            name = cast(str, monitor["name"])
            if name in placed:
                continue
            parent = monitor.get("parent")
            # Ein parent, den die YAML nicht selbst definiert, muss schon am Server
            # existieren - das pruefen wir nicht hier, sondern der Client beim Aufloesen.
            if not parent or parent in placed or parent not in names:
                ordered.append(monitor)
                placed.add(name)
                progress = True
        if not progress:
            stuck = sorted(names - placed)
            raise KumaError(f"Zyklus in der Gruppen-Hierarchie, haengen geblieben bei: {stuck}")
    return ordered


def apply_cmd(
    file: Path = typer.Option(..., "--file", "-f", help="YAML mit dem Soll-Zustand"),
    url: str | None = typer.Option(None, help="ueberschreibt die url aus der YAML"),
    check: bool = typer.Option(False, "--check", help="nichts schreiben, nur zeigen was waere"),
    prune: bool = typer.Option(False, "--prune", help="Monitore loeschen, die die YAML nicht kennt"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG-Logging"),
) -> None:
    """Wendet den Soll-Zustand an - idempotent, mehrfach ausfuehrbar.

    Args:
        file: Pfad der YAML mit dem Soll-Zustand.
        url: Optionale Ueberschreibung der Ziel-URL.
        check: Wenn True, wird nichts geschrieben.
        prune: Wenn True, werden nicht deklarierte Monitore geloescht.
        verbose: Wenn True, wird auf DEBUG-Level geloggt.

    Raises:
        SystemExit: Bei fehlender URL oder Fehlern gegen die Instanz.
    """
    configure_logging(verbose=verbose)
    print_banner()

    state = cast(DesiredState, yaml.safe_load(file.read_text()) or {})
    target = url or state.get("url")
    if not target:
        sys.exit("keine url - weder per --url noch in der YAML")

    user, pw = load_creds()
    changes = 0
    prefix = "[check] " if check else ""

    with KumaClient(target) as client:
        client.login(user, pw)

        # In --check entsteht nichts wirklich. Objekte, die erst dieser Lauf anlegen wuerde,
        # koennen deshalb auch nicht als parent/notification aufgeloest werden. Wir merken
        # sie vor und melden Abhaengige als "anlegen", statt an der Aufloesung zu scheitern.
        on_server = {cast(str, m["name"]) for m in client.monitors().values()}
        notifs_on_server = {cast(str, n["name"]) for n in client.notifications()}
        pending_monitors = {cast(str, m["name"]) for m in state.get("monitors") or [] if m["name"] not in on_server}
        pending_notifs = {
            cast(str, n["name"]) for n in state.get("notifications") or [] if n["name"] not in notifs_on_server
        }

        for notification in state.get("notifications") or []:
            result = client.upsert_notification(notification, check_mode=check)
            if result["changed"]:
                changes += 1
                verb = "anlegen" if result["created"] else "aendern"
                typer.echo(f"{prefix}notification {notification['name']!r}: {verb} {_fmt(result['diff'])}")

        for monitor in order_parents_first(list(state.get("monitors") or [])):
            name = cast(str, monitor["name"])

            if check and _has_pending_refs(monitor, pending_monitors, pending_notifs):
                changes += 1
                typer.echo(
                    f"{prefix}monitor {name!r}: anlegen "
                    f"(referenzen entstehen erst im echten lauf, daher hier nicht aufgeloest)"
                )
                continue

            existing = client.monitor_by_name(name)
            desired = monitor if existing else {**CREATE_DEFAULTS, **monitor}
            result = client.upsert_monitor(desired, check_mode=check)
            if result["changed"]:
                changes += 1
                verb = "anlegen" if result["created"] else "aendern"
                typer.echo(f"{prefix}monitor {monitor['name']!r}: {verb} {_fmt(result['diff'])}")
            if result.get("not_applicable"):
                typer.secho(
                    f"  hinweis: {monitor['name']!r} - vom server nach dem anlegen nicht mehr "
                    f"aenderbar: {_fmt(result['not_applicable'])}",
                    fg=typer.colors.YELLOW,
                )

        if prune:
            declared = {cast(str, m["name"]) for m in state.get("monitors") or []}
            for existing_monitor in list(client.monitors().values()):
                name = cast(str, existing_monitor["name"])
                if name in declared:
                    continue
                changes += 1
                typer.echo(f"{prefix}monitor {name!r}: loeschen (nicht in der YAML)")
                if not check:
                    client.delete_monitor(existing_monitor["id"])

    if changes:
        typer.secho(f"{prefix}{changes} aenderung(en)", fg=typer.colors.YELLOW)
    else:
        typer.secho("nichts zu tun - soll == ist", fg=typer.colors.GREEN)


def _has_pending_refs(monitor: dict[str, object], pending_monitors: set[str], pending_notifs: set[str]) -> bool:
    """Prueft, ob ein Monitor auf etwas zeigt, das erst dieser Lauf anlegen wuerde.

    Args:
        monitor: Der Monitor aus der YAML.
        pending_monitors: Monitor-Namen, die es am Server noch nicht gibt.
        pending_notifs: Notification-Namen, die es am Server noch nicht gibt.

    Returns:
        True, wenn parent oder eine Notification noch nicht existiert.
    """
    if monitor.get("parent") in pending_monitors:
        return True
    refs = monitor.get("notifications") or []
    return isinstance(refs, list) and any(r in pending_notifs for r in refs)


def _fmt(diff: dict[str, dict[str, object]]) -> str:
    """Kuerzt ein Diff auf eine einzeilige Anzeige.

    Args:
        diff: Diff aus dem Upsert.

    Returns:
        Kompakte Darstellung, Secrets als Feldname ohne Wert.
    """
    parts: list[str] = []
    for key, change in sorted(diff.items()):
        if any(s in key.lower() for s in ("password", "token", "secret")):
            parts.append(f"{key}=<geheim>")
        else:
            parts.append(f"{key}: {change['before']!r} -> {change['after']!r}")
    return "(" + ", ".join(parts) + ")" if parts else ""


if __name__ == "__main__":
    typer.run(apply_cmd)
