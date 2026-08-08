#!/usr/bin/env python3
"""Export/import of the complete Uptime-Kuma state via the Socket.IO API.

Examples::

    python3 -m uptimekumastuff.uptimekuma_simpleapi export \\
        --url https://uptimekuma.example.lan --out state.local.json
    python3 -m uptimekumastuff.uptimekuma_simpleapi export \\
        --url https://uptimekuma.example.lan --out state.local.yml   # or --format yaml
    python3 -m uptimekumastuff.uptimekuma_simpleapi import \\
        --url http://127.0.0.1:3001 --in-file state.local.json --paused

The export format follows the extension of ``--out``; ``--format json|yaml`` forces it.
``import`` takes either, detected by content.

Why not just uptime-kuma-api?
    The lib (1.2.1, last released 2023) can read against Kuma 2.x, but not write:

    * ``add_monitor()`` -> "NOT NULL constraint failed: monitor.conditions". Kuma 2.x has
      the column ``conditions`` NOT NULL DEFAULT '[]'; the lib doesn't know the field, so the
      server writes an explicit NULL - and explicit NULL beats the default.
    * ``delete_monitor()`` -> "monitor does not exist", because it validates against a stale
      local cache.
    * ``get_status_page()`` -> KeyError 'incident'; the field no longer exists in 2.x.

    Reads therefore go through the lib, where they work, writes directly via ``_call()``.
    After every write run verify with a FRESH session - the session caches lie.

Credentials:
    ``uptimekuma.local.env`` (UPTIME_KUMA_USERNAME / UPTIME_KUMA_PASSWORD) or real
    environment variables, which take precedence. For a different target system there are
    ``--username`` / ``--password``. The file is searched first in the current working
    directory, then in the package directory; the first hit wins. ``UPTIME_KUMA_URL`` there
    replaces ``--url``. Template: ``uptimekuma.env.example``.

Warning:
    The export contains plaintext secrets (MQTT passwords, Telegram bot tokens, Gotify
    tokens, SMTP password). The file does not belong in Git - pick a filename with
    ``.local.``, which is gitignored.
"""

import json
import os
import sys
import urllib.request
from enum import Enum
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import typer
import yaml
from uptime_kuma_api import UptimeKumaApi

from uptimekumastuff import configure_logging, env_url, load_creds_env, print_banner

# On read Kuma delivers 115 fields, but the monitor table has only 111 columns. These are
# derived server-side or live in their own tables and would make bean.import() run into
# unknown columns on write.
DERIVED_MONITOR_FIELDS: frozenset[str] = frozenset(
    {
        "id",  # the target system assigns anew
        "childrenIDs",  # follows from the children's parent
        "path",  # follows from the parent chain
        "pathName",  # ditto
        "maintenance",  # runtime state
        "forceInactive",  # follows from the parent
        "includeSensitiveData",  # read flag of the API
        "screenshot",  # runtime artifact (column is called screenshot_delay)
        "dns_last_result",  # runtime result
        "tags",  # own table monitor_tag
        "notificationIDList",  # own table monitor_notification, gets remapped
    }
)

# A payload as the Socket API accepts and delivers it. `object` instead of `Any`:
# unknown values must be narrowed before use rather than silently slipping through.
type Payload = dict[str, object]


class MonitorTagLink(TypedDict):
    """Assignment tag -> monitor from ``monitor["tags"]``.

    Attributes:
        tag_id: ID of the tag in the old instance.
        value: Optional free-text value of the assignment.
    """

    tag_id: int
    value: NotRequired[str | None]


class MonitorDict(TypedDict, total=False):
    """A monitor as ``getMonitorList`` delivers it.

    Kuma delivers 115 fields that differ strongly per monitor type. Only the ones this script
    accesses are listed here - all others are passed through unchanged on import, without our
    having to know them.

    Attributes:
        id: Monitor ID in the source instance.
        name: Display name, serves as a stable key between instances.
        type: Monitor type, e.g. ``mqtt``, ``group``, ``http``.
        parent: ID of the parent group or ``None``.
        active: Whether the monitor is running.
        notificationIDList: Linked notification IDs of the source instance.
        tags: Tag assignments of the monitor.
    """

    id: int
    name: str
    type: str
    parent: int | None
    active: bool
    notificationIDList: list[int]
    tags: list[MonitorTagLink]


class NotificationDict(TypedDict, total=False):
    """A notification provider as ``getNotificationList`` delivers it.

    The remaining fields depend on the provider (smtpHost, telegramBotToken, ...) and are
    passed through unchanged.

    Attributes:
        id: Notification ID in the source instance.
        name: Display name.
        applyExisting: One-shot UI trigger, not state - see ``_add_notification``.
    """

    id: int
    name: str
    applyExisting: bool


class TagDict(TypedDict):
    """A tag as ``getTags`` delivers it.

    Attributes:
        id: Server-side tag ID.
        name: Display name.
        color: Hex color, e.g. ``#059669``.
    """

    id: int
    name: str
    color: str


class StatusPageMonitor(TypedDict):
    """Monitor entry within a status page group.

    Attributes:
        id: Monitor ID in the old instance.
        sendUrl: 0/1, whether the URL is shown publicly.
    """

    id: int
    sendUrl: NotRequired[int]


class StatusPageGroup(TypedDict):
    """Public monitor group of a status page.

    Attributes:
        name: Group name.
        weight: Sort weight.
        monitorList: Monitors of the group.
    """

    name: str
    weight: NotRequired[int]
    monitorList: list[StatusPageMonitor]


class StatusPageExport(TypedDict):
    """A status page as this script exports it.

    Attributes:
        config: The page config (slug, title, theme, customCSS, ...).
        publicGroupList: The public monitor groups.
    """

    config: Payload
    publicGroupList: list[StatusPageGroup]


class KumaState(TypedDict):
    """The complete exported state of an instance.

    Attributes:
        monitors: All monitors incl. groups.
        notifications: All notification providers.
        tags: All tags.
        status_pages: All status pages incl. groups.
    """

    monitors: list[MonitorDict]
    notifications: list[NotificationDict]
    tags: list[TagDict]
    status_pages: list[StatusPageExport]


class ImportReport(TypedDict):
    """Mapping old ID -> new ID per object type.

    Attributes:
        notifications: Mapping of the notification IDs.
        tags: Mapping of the tag IDs.
        monitors: Mapping of the monitor IDs.
    """

    notifications: dict[int, int]
    tags: dict[int, int]
    monitors: dict[int, int]


def jsonable(obj: object) -> object:
    """Recursively converts lib enums (``MonitorType`` & co.) into JSON-capable values.

    uptime-kuma-api returns ``type`` as a ``MonitorType`` enum, which ``json.dump`` cannot
    serialize.

    Args:
        obj: Any value from uptime-kuma-api.

    Returns:
        The same value, but with enums as their ``.value``.
    """
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [jsonable(v) for v in obj]
    return obj


class StateFormat(str, Enum):
    """Serialization format of the state file.

    Attributes:
        auto: Derive from the file extension of ``--out`` (.yml/.yaml -> YAML, else JSON).
        json: Force JSON.
        yaml: Force YAML.
    """

    auto = "auto"
    json = "json"
    yaml = "yaml"


YAML_SUFFIXES: frozenset[str] = frozenset({".yml", ".yaml"})


def dump_state(state: KumaState, out: Path, out_format: StateFormat) -> StateFormat:
    """Writes the state to a file as JSON or YAML.

    Args:
        state: The exported state.
        out: Target file.
        out_format: Desired format; ``auto`` derives it from ``out``'s extension.

    Returns:
        The format actually used - never ``auto``.
    """
    resolved = out_format
    if resolved is StateFormat.auto:
        resolved = StateFormat.yaml if out.suffix.lower() in YAML_SUFFIXES else StateFormat.json
    if resolved is StateFormat.yaml:
        # sort_keys=False keeps Kuma's field order, so a diff between two exports stays readable.
        out.write_text(yaml.safe_dump(state, sort_keys=False, allow_unicode=True, default_flow_style=False))
    else:
        out.write_text(json.dumps(state, indent=1))
    return resolved


def load_state(in_file: Path) -> KumaState:
    """Reads a state file, whether it holds JSON or YAML.

    JSON is tried first on purpose: PyYAML implements YAML 1.1, which resolves a few JSON
    scalars differently (``1e5`` becomes a string, not a number). For a JSON file the JSON
    parser is therefore the correct one, and YAML is only the fallback.

    Args:
        in_file: The file to read.

    Returns:
        The state contained in it.
    """
    text = in_file.read_text()
    try:
        return cast(KumaState, json.loads(text))
    except json.JSONDecodeError:
        return cast(KumaState, yaml.safe_load(text))


def load_creds(username: str | None, password: str | None) -> tuple[str, str]:
    """Determines the credentials from CLI arguments, environment or creds file.

    Searched in this order: CLI arguments, real environment variables,
    ``uptimekuma.local.env`` in the current working directory, then the one in the
    package directory.

    Args:
        username: Explicitly passed username or ``None``.
        password: Explicitly passed password or ``None``.

    Returns:
        Tuple of username and password.

    Raises:
        SystemExit: If neither CLI, environment nor creds file provides both.
    """
    if username and password:
        return username, password
    searched = load_creds_env()
    user = username or os.environ.get("UPTIME_KUMA_USERNAME")
    pw = password or os.environ.get("UPTIME_KUMA_PASSWORD")
    if not user or not pw:
        locations = " or ".join(str(path) for path in searched)
        sys.exit(
            f"Credentials missing: UPTIME_KUMA_USERNAME/UPTIME_KUMA_PASSWORD "
            f"(neither in the environment nor in {locations})"
        )
    return user, pw


class SimpleKumaApi:
    """Thin shell around uptime-kuma-api: reads via the lib, writes via ``_call()``.

    Attributes:
        url: Base URL of the instance.
        api: The underlying uptime-kuma-api session.
    """

    def __init__(self, url: str, username: str, password: str, timeout: int = 30) -> None:
        """Connects and logs in.

        Args:
            url: Base URL, e.g. ``http://127.0.0.1:3001``.
            username: Username (the socket login accepts no API keys).
            password: Password.
            timeout: Socket timeout in seconds.
        """
        self.url = url.rstrip("/")
        self.api = UptimeKumaApi(self.url, timeout=timeout)
        self.api.login(username, password)

    def close(self) -> None:
        """Closes the socket connection."""
        self.api.disconnect()

    # ---------------------------------------------------------------- export

    def _export_status_page(self, slug: str) -> StatusPageExport:
        """Reads a status page in full.

        ``api.get_status_page()`` is broken against 2.x (KeyError 'incident'), and the raw
        socket call only delivers the config - the monitor groups hang off the public HTTP
        endpoint.

        Args:
            slug: Slug of the status page.

        Returns:
            Config and public group list.
        """
        config = cast(Payload, self.api._call("getStatusPage", slug)["config"])
        with urllib.request.urlopen(f"{self.url}/api/status-page/{slug}") as resp:
            public = json.loads(resp.read())
        return {
            "config": config,
            "publicGroupList": public.get("publicGroupList") or [],
        }

    def export_state(self) -> KumaState:
        """Pulls monitors, notifications, tags and status pages from the instance.

        Returns:
            The complete state, JSON-serializable.
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
        """Creates a notification provider.

        ``applyExisting`` is deliberately forced hard to False: it is not state but a one-shot
        UI trigger ("attach to all existing monitors"). The server normalizes it to False on
        save itself, but evaluates it beforehand - were it True and monitors already existed,
        the notification would be attached to *all* of them and the exact links overwritten.
        In old prod DBs it is still True.

        Args:
            notification: Notification dict from the export.

        Returns:
            The new notification ID.
        """
        payload: Payload = {k: v for k, v in notification.items() if k not in ("id", "userId")}
        payload["applyExisting"] = False
        result = self.api._call("addNotification", (payload, None))
        return int(result["id"])

    def _add_tag(self, tag: TagDict) -> int:
        """Creates a tag.

        Args:
            tag: Tag dict from the export.

        Returns:
            The new tag ID.
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
        """Creates a monitor and remaps all foreign IDs in the process.

        Args:
            monitor: Monitor dict from the export.
            notif_map: Mapping old -> new notification ID.
            parent_map: Mapping old -> new monitor ID (for ``parent``).
            paused: If True, the monitor is created inactive.

        Returns:
            The new monitor ID.
        """
        data: Payload = {k: v for k, v in monitor.items() if k not in DERIVED_MONITOR_FIELDS}

        parent = monitor.get("parent")
        if parent:
            data["parent"] = parent_map[parent]

        # The server does `for (let id in notificationIDList)` and checks for truthy.
        # With a list [1, 3] those would be the indices 0, 1 -> wrong link.
        # It must be a dict {new_id: True}.
        data["notificationIDList"] = {
            notif_map[old]: True for old in monitor.get("notificationIDList") or [] if old in notif_map
        }

        if paused:
            data["active"] = False  # prevents startMonitor() server-side

        result = self.api._call("add", data)
        return int(result["monitorID"])

    def _add_status_page(self, page: StatusPageExport, monitor_map: dict[int, int]) -> None:
        """Creates a status page and re-links its monitor groups.

        Args:
            page: Status page from the export.
            monitor_map: Mapping old -> new monitor ID.
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

        # imgDataUrl must be a string - the server blindly calls .startsWith("data:") on it.
        # If it isn't a data URI, the value is taken over as config.logo; we therefore pass
        # through the existing icon path (as the frontend does).
        icon = cfg.get("icon") or "/icon.svg"
        self.api._call("saveStatusPage", (slug, cfg, icon, groups))

    @staticmethod
    def parents_first(monitors: list[MonitorDict]) -> list[MonitorDict]:
        """Sorts monitors so that every group comes before its children.

        Kuma allows groups within groups, hence topological rather than just "groups first".

        Args:
            monitors: All monitors from the export.

        Returns:
            The same monitors, parents before children.

        Raises:
            RuntimeError: On a cycle in the hierarchy - better to abort than to silently lose
                monitors.
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
                raise RuntimeError(f"cycle in the group hierarchy, stuck at: {stuck}")
        return ordered

    def import_state(self, state: KumaState, paused: bool = False) -> ImportReport:
        """Writes an exported state into the instance.

        The order is mandatory: notifications and tags first (monitors reference them), then
        monitors parents-before-children, then tag assignments, then status pages.

        Args:
            state: The state to write.
            paused: If True, all monitors are created inactive - no checks, no alarms.
                Strongly recommended for test instances.

        Returns:
            The ID mappings old -> new.
        """
        report: ImportReport = {"notifications": {}, "tags": {}, "monitors": {}}

        for notification in state["notifications"]:
            report["notifications"][notification["id"]] = self._add_notification(notification)
        typer.echo(f"notifications: {len(report['notifications'])} created")

        for tag in state["tags"]:
            report["tags"][tag["id"]] = self._add_tag(tag)
        typer.echo(f"tags: {len(report['tags'])} created")

        for monitor in self.parents_first(state["monitors"]):
            report["monitors"][monitor["id"]] = self._add_monitor(
                monitor, report["notifications"], report["monitors"], paused
            )
        typer.echo(f"monitors: {len(report['monitors'])} created{' (paused)' if paused else ''}")

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
            typer.echo(f"tag assignments: {links} created")

        for page in state["status_pages"]:
            self._add_status_page(page, report["monitors"])
        if state["status_pages"]:
            typer.echo(f"status pages: {len(state['status_pages'])} created")

        return report


app = typer.Typer(add_completion=False, help=__doc__)


@app.command("export")
def export_cmd(
    url: str | None = typer.Option(None, help="Base URL of the source instance; else UPTIME_KUMA_URL"),
    out: Path = typer.Option(..., help="Target file for the export"),
    out_format: StateFormat = typer.Option(
        StateFormat.auto, "--format", help="output format; auto derives it from --out's extension"
    ),
    username: str | None = typer.Option(None, help="overrides uptimekuma.local.env"),
    password: str | None = typer.Option(None, help="overrides uptimekuma.local.env"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG logging"),
) -> None:
    """Pulls the complete state of an instance into a JSON or YAML file.

    Args:
        url: Base URL of the source instance, otherwise from UPTIME_KUMA_URL.
        out: Path of the file to write.
        out_format: JSON, YAML, or auto (from the extension of ``out``).
        username: Optional username, otherwise from environment/creds file.
        password: Optional password, otherwise from environment/creds file.
        verbose: If True, logs at DEBUG level.

    Raises:
        SystemExit: On a missing URL.
    """
    configure_logging(verbose=verbose)
    print_banner()

    target = url or env_url()
    if not target:
        sys.exit("no url - neither via --url nor as UPTIME_KUMA_URL")

    user, pw = load_creds(username, password)
    api = SimpleKumaApi(target, user, pw)
    try:
        state = api.export_state()
    finally:
        api.close()

    used = dump_state(state, out, out_format)
    typer.echo(
        f"export -> {out} ({used.value}): {len(state['monitors'])} monitors, "
        f"{len(state['notifications'])} notifications, {len(state['tags'])} tags, "
        f"{len(state['status_pages'])} status pages"
    )
    typer.secho("WARNING: contains plaintext secrets - do not commit.", fg=typer.colors.YELLOW)


@app.command("import")
def import_cmd(
    url: str | None = typer.Option(None, help="Base URL of the target instance; else UPTIME_KUMA_URL"),
    in_file: Path = typer.Option(..., "--in-file", help="JSON or YAML export that gets written"),
    paused: bool = typer.Option(False, help="create monitors inactive: no checks, no alarms"),
    dry_run: bool = typer.Option(False, help="only show what would be created"),
    username: str | None = typer.Option(None, help="creds of the TARGET instance"),
    password: str | None = typer.Option(None, help="creds of the TARGET instance"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG logging"),
) -> None:
    """Writes a JSON or YAML export into an (empty) instance.

    Args:
        url: Base URL of the target instance, otherwise from UPTIME_KUMA_URL.
        in_file: Path of the export to read in; JSON or YAML, detected by content.
        paused: Create monitors inactive.
        dry_run: Write nothing, only print the planned objects.
        username: Optional username of the target instance.
        password: Optional password of the target instance.
        verbose: If True, logs at DEBUG level.

    Raises:
        SystemExit: On a missing URL.
    """
    configure_logging(verbose=verbose)
    print_banner()

    state = load_state(in_file)

    if dry_run:
        groups = [m for m in state["monitors"] if m.get("type") == "group"]
        typer.echo(
            f"[dry-run] would create: {len(state['notifications'])} notifications, "
            f"{len(state['tags'])} tags, {len(state['monitors'])} monitors "
            f"({len(groups)} of them groups), {len(state['status_pages'])} status pages"
        )
        return

    target = url or env_url()
    if not target:
        sys.exit("no url - neither via --url nor as UPTIME_KUMA_URL")

    user, pw = load_creds(username, password)
    api = SimpleKumaApi(target, user, pw)
    try:
        api.import_state(state, paused=paused)
    finally:
        api.close()


if __name__ == "__main__":
    app()
