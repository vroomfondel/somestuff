"""Applies a declarative desired state from a YAML file to Uptime Kuma.

Unlike ``uptimekuma_simpleapi`` (full export/import into an empty instance), this module
idempotently reconciles a partial desired state against an existing instance - re-runnable,
with ``--check`` as a dry run.

    python3 -m uptimekumastuff.uptimekuma_apply --file kuma_state.local.yml
    python3 -m uptimekumastuff.uptimekuma_apply --file kuma_state.local.yml --check
    python3 -m uptimekumastuff.uptimekuma_apply --file kuma_state.local.yml --prune

Structure of the YAML::

    url: https://uptimekuma.example.lan

    notifications:
      - name: My Gotify Alarm (1)
        type: gotify
        gotifyserverurl: https://gotify.example.org
        gotifyapplicationToken: "{{ token }}"
        gotifyPriority: 8

    monitors:
      - name: outdoormesh
        type: group
      - name: husqvarna/automower/pongs
        type: mqtt
        parent: outdoormesh              # name, not ID
        notifications: [My Gotify Alarm (1)]
        hostname: mosquitto.mosquitto.svc.cluster.local
        port: 1883
        mqttTopic: husqvarna/automower/pongs
        active: true

Field names are Kuma's (camelCase), so an export from ``uptimekuma_simpleapi`` drops in here
without translation.

Credentials: ``uptimekuma.local.env`` or UPTIME_KUMA_USERNAME/UPTIME_KUMA_PASSWORD.
Template: ``uptimekuma.env.example``.

Warning:
    The YAML usually contains plaintext secrets (MQTT passwords, provider tokens). It does not
    belong in Git - pick a filename with ``.local.``, which is gitignored.
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

# On creation the server requires these fields; `conditions` is NOT NULL and an explicit
# NULL beats the DB default.
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
    """The desired state from the YAML.

    Attributes:
        url: Base URL of the instance. Overridable via --url.
        notifications: Notification providers, flat in Kuma field names.
        monitors: Monitors, in Kuma field names; ``parent``/``notifications`` by name.
    """

    url: str
    notifications: list[dict[str, object]]
    monitors: list[dict[str, object]]


def load_creds() -> tuple[str, str]:
    """Determines the credentials from the environment or the creds file.

    Returns:
        Username and password.

    Raises:
        SystemExit: If both are missing.
    """
    load_dotenv(CREDS_FILE)
    user = os.environ.get("UPTIME_KUMA_USERNAME")
    pw = os.environ.get("UPTIME_KUMA_PASSWORD")
    if not user or not pw:
        sys.exit(f"UPTIME_KUMA_USERNAME/UPTIME_KUMA_PASSWORD missing (neither environment nor {CREDS_FILE})")
    return user, pw


def order_parents_first(monitors: list[dict[str, object]]) -> list[dict[str, object]]:
    """Sorts monitors so that groups are created before their children.

    Groups are nestable, hence topological rather than just "groups first".

    Args:
        monitors: Monitors from the YAML.

    Returns:
        The same monitors, parents before children.

    Raises:
        KumaError: On a cycle in the hierarchy.
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
            # A parent that the YAML doesn't define itself must already exist on the server -
            # we don't check that here; the client does when resolving.
            if not parent or parent in placed or parent not in names:
                ordered.append(monitor)
                placed.add(name)
                progress = True
        if not progress:
            stuck = sorted(names - placed)
            raise KumaError(f"cycle in the group hierarchy, stuck at: {stuck}")
    return ordered


def apply_cmd(
    file: Path = typer.Option(..., "--file", "-f", help="YAML with the desired state"),
    url: str | None = typer.Option(None, help="overrides the url from the YAML"),
    check: bool = typer.Option(False, "--check", help="write nothing, only show what would happen"),
    prune: bool = typer.Option(False, "--prune", help="delete monitors the YAML doesn't know"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG logging"),
) -> None:
    """Applies the desired state - idempotent, re-runnable.

    Args:
        file: Path of the YAML with the desired state.
        url: Optional override of the target URL.
        check: If True, nothing is written.
        prune: If True, monitors not declared are deleted.
        verbose: If True, logs at DEBUG level.

    Raises:
        SystemExit: On a missing URL or errors against the instance.
    """
    configure_logging(verbose=verbose)
    print_banner()

    state = cast(DesiredState, yaml.safe_load(file.read_text()) or {})
    target = url or state.get("url")
    if not target:
        sys.exit("no url - neither via --url nor in the YAML")

    user, pw = load_creds()
    changes = 0
    prefix = "[check] " if check else ""

    with KumaClient(target) as client:
        client.login(user, pw)

        # In --check nothing is actually created. Objects this run would create for the first
        # time therefore can't be resolved as parent/notification either. We note them up front
        # and report dependents as "create" instead of failing on resolution.
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
                verb = "create" if result["created"] else "change"
                typer.echo(f"{prefix}notification {notification['name']!r}: {verb} {_fmt(result['diff'])}")

        for monitor in order_parents_first(list(state.get("monitors") or [])):
            name = cast(str, monitor["name"])

            if check and _has_pending_refs(monitor, pending_monitors, pending_notifs):
                changes += 1
                typer.echo(
                    f"{prefix}monitor {name!r}: create "
                    f"(references only come into existence in the real run, so not resolved here)"
                )
                continue

            existing = client.monitor_by_name(name)
            desired = monitor if existing else {**CREATE_DEFAULTS, **monitor}
            result = client.upsert_monitor(desired, check_mode=check)
            if result["changed"]:
                changes += 1
                verb = "create" if result["created"] else "change"
                typer.echo(f"{prefix}monitor {monitor['name']!r}: {verb} {_fmt(result['diff'])}")
            if result.get("not_applicable"):
                typer.secho(
                    f"  note: {monitor['name']!r} - no longer changeable by the server after "
                    f"creation: {_fmt(result['not_applicable'])}",
                    fg=typer.colors.YELLOW,
                )

        if prune:
            declared = {cast(str, m["name"]) for m in state.get("monitors") or []}
            for existing_monitor in list(client.monitors().values()):
                name = cast(str, existing_monitor["name"])
                if name in declared:
                    continue
                changes += 1
                typer.echo(f"{prefix}monitor {name!r}: delete (not in the YAML)")
                if not check:
                    client.delete_monitor(existing_monitor["id"])

    if changes:
        typer.secho(f"{prefix}{changes} change(s)", fg=typer.colors.YELLOW)
    else:
        typer.secho("nothing to do - desired == actual", fg=typer.colors.GREEN)


def _has_pending_refs(monitor: dict[str, object], pending_monitors: set[str], pending_notifs: set[str]) -> bool:
    """Checks whether a monitor points at something this run would only create now.

    Args:
        monitor: The monitor from the YAML.
        pending_monitors: Monitor names that don't yet exist on the server.
        pending_notifs: Notification names that don't yet exist on the server.

    Returns:
        True if parent or a notification does not exist yet.
    """
    if monitor.get("parent") in pending_monitors:
        return True
    refs = monitor.get("notifications") or []
    return isinstance(refs, list) and any(r in pending_notifs for r in refs)


def _fmt(diff: dict[str, dict[str, object]]) -> str:
    """Shortens a diff to a single-line display.

    Args:
        diff: Diff from the upsert.

    Returns:
        Compact representation, secrets shown as field name without value.
    """
    parts: list[str] = []
    for key, change in sorted(diff.items()):
        if any(s in key.lower() for s in ("password", "token", "secret")):
            parts.append(f"{key}=<secret>")
        else:
            parts.append(f"{key}: {change['before']!r} -> {change['after']!r}")
    return "(" + ", ".join(parts) + ")" if parts else ""


if __name__ == "__main__":
    typer.run(apply_cmd)
