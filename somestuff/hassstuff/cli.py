"""Command line access to Home Assistant over REST and WebSocket.

Read: states, history, logbook, templates, entity/device/area registry, traces, dashboards.
Write: service calls, entity registry updates, helpers, automations/scripts, dashboards, raw
WebSocket commands.
"""

import datetime
import difflib
import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from typing import Final
from typing import Literal

import httpx
import typer
import yaml
from websockets.exceptions import WebSocketException

from somestuff.hassstuff.client import FALLBACK_URL
from somestuff.hassstuff.client import TOKEN_FILE_NAME
from somestuff.hassstuff.client import URL_FILE_NAME
from somestuff.hassstuff.client import HassConfig
from somestuff.hassstuff.client import HassError
from somestuff.hassstuff.client import HassRest
from somestuff.hassstuff.client import HassWebSocket
from somestuff.hassstuff.client import JsonObject
from somestuff.hassstuff.client import JsonValue
from somestuff.hassstuff.client import default_token_file
from somestuff.hassstuff.client import default_url
from somestuff.hassstuff.client import load_token
from somestuff.hassstuff.models import EntityRegistryEntry
from somestuff.hassstuff.models import State

type ConfigKind = Literal["automation", "script"]

HELPER_TYPES: Final = (
    "input_boolean",
    "input_button",
    "input_datetime",
    "input_number",
    "input_select",
    "input_text",
    "counter",
    "timer",
    "schedule",
)

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
helper_app = typer.Typer(no_args_is_help=True, help="Storage helpers (input_*, counter, timer, schedule).")
automation_app = typer.Typer(no_args_is_help=True, help="Automations from the UI config store.")
script_app = typer.Typer(no_args_is_help=True, help="Scripts from the UI config store.")
trace_app = typer.Typer(no_args_is_help=True, help="Automation/script run traces.")
dashboard_app = typer.Typer(no_args_is_help=True, help="Lovelace dashboards.")
app.add_typer(helper_app, name="helper")
app.add_typer(automation_app, name="automation")
app.add_typer(script_app, name="script")
app.add_typer(trace_app, name="trace")
app.add_typer(dashboard_app, name="dashboard")

YamlOpt = Annotated[bool, typer.Option("--yaml", help="Print YAML instead of JSON.")]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Only show the diff, do not save.")]
KvArg = Annotated[list[str] | None, typer.Argument(help="key=value pairs; values are parsed as JSON if possible.")]
DataOpt = Annotated[str | None, typer.Option("--data", help="JSON object merged under the key=value pairs.")]


@dataclass(slots=True)
class ConnectionOptions:
    """Global options, resolved into a :class:`HassConfig` only when a command needs one.

    Attributes:
        url: ``--url``/``HASS_URL``; ``None`` falls back to :func:`default_url`.
        token: ``--token``/``HASS_TOKEN``.
        token_file: ``--token-file``/``HASS_TOKEN_FILE``; ``None`` falls back to :func:`default_token_file`.
        insecure: Skip TLS verification.
        timeout: Request timeout in seconds.
        resolved: Cached result of :meth:`config`.
    """

    url: str | None
    token: str | None
    token_file: Path | None
    insecure: bool
    timeout: float
    resolved: HassConfig | None = None

    def config(self) -> HassConfig:
        """Resolve URL and token on first use.

        Returns:
            Connection settings.
        """
        if self.resolved is None:
            self.resolved = HassConfig(
                url=(self.url or default_url()).rstrip("/"),
                token=load_token(self.token, self.token_file or default_token_file()),
                verify_tls=not self.insecure,
                timeout=self.timeout,
            )
        return self.resolved


@app.callback()
def main(
    ctx: typer.Context,
    url: Annotated[
        str | None,
        typer.Option(
            envvar="HASS_URL",
            show_default=f"{URL_FILE_NAME}, else {FALLBACK_URL}",
            help="Home Assistant base URL.",
        ),
    ] = None,
    token: Annotated[str | None, typer.Option(envvar="HASS_TOKEN", help="Long-lived access token.")] = None,
    token_file: Annotated[
        Path | None,
        typer.Option(
            envvar="HASS_TOKEN_FILE", show_default=TOKEN_FILE_NAME, help="File with the token, used without --token."
        ),
    ] = None,
    insecure: Annotated[bool, typer.Option("--insecure", help="Skip TLS certificate verification.")] = False,
    timeout: Annotated[float, typer.Option(help="Request timeout in seconds.")] = 30.0,
) -> None:
    """Store the global options; nothing is resolved until a command connects.

    Args:
        ctx: Typer context; receives the :class:`ConnectionOptions` as ``obj``.
        url: Home Assistant base URL.
        token: Access token.
        token_file: Token file used when ``token`` is empty.
        insecure: Disable TLS verification.
        timeout: Request timeout in seconds.
    """
    ctx.obj = ConnectionOptions(url=url, token=token, token_file=token_file, insecure=insecure, timeout=timeout)


def _cfg(ctx: typer.Context) -> HassConfig:
    """Resolve the connection settings stored by :func:`main`.

    Args:
        ctx: Typer context.

    Returns:
        Connection settings.
    """
    options: ConnectionOptions = ctx.find_root().obj
    return options.config()


def _dump(value: object, as_yaml: bool = False) -> None:
    """Print a JSON value as JSON or YAML.

    Args:
        value: Value to print.
        as_yaml: Print YAML instead of JSON.
    """
    if as_yaml:
        typer.echo(yaml.safe_dump(value, allow_unicode=True, sort_keys=False, width=120), nl=False)
    else:
        typer.echo(json.dumps(value, indent=2, ensure_ascii=False))


def _table(rows: list[list[str]], header: list[str]) -> None:
    """Print rows as left-aligned columns.

    Args:
        rows: Table body.
        header: Column titles.
    """
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())


def _parse_kv(items: list[str] | None, data: str | None) -> JsonObject:
    """Build a JSON object from ``--data`` and ``key=value`` arguments.

    Args:
        items: ``key=value`` strings; values are JSON-decoded when valid JSON, else kept as text.
        data: JSON object string; ``key=value`` pairs override its keys.

    Returns:
        Merged object.

    Raises:
        typer.BadParameter: On malformed input.
    """
    result: JsonObject = {}
    if data:
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise typer.BadParameter("--data must be a JSON object")
        result.update(parsed)
    for item in items or []:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"expected key=value, got {item!r}")
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result


def _load_file(path: Path) -> JsonValue:
    """Load a YAML or JSON file (``-`` reads stdin).

    Args:
        path: Input file.

    Returns:
        Parsed content.
    """
    text = typer.get_text_stream("stdin").read() if str(path) == "-" else path.read_text(encoding="utf-8")
    loaded: JsonValue = yaml.safe_load(text)
    return loaded


def _show_diff(old: JsonValue, new: JsonValue, label: str) -> bool:
    """Print a unified YAML diff between two values.

    Args:
        old: Current value.
        new: Proposed value.
        label: Name shown in the diff header.

    Returns:
        Whether the values differ.
    """

    def render(value: JsonValue) -> list[str]:
        text: str = yaml.safe_dump(value, allow_unicode=True, sort_keys=True, width=120)
        return text.splitlines(keepends=True)

    diff = list(difflib.unified_diff(render(old), render(new), f"{label} (current)", f"{label} (new)"))
    typer.echo("".join(diff) if diff else f"{label}: no changes")
    return bool(diff)


def _matches(pattern: str | None, *fields: str | None) -> bool:
    """Case-insensitive substring or glob match over several fields.

    Args:
        pattern: Substring, or glob if it contains ``*``/``?``; ``None`` matches everything.
        *fields: Candidate strings.

    Returns:
        Whether any field matches.
    """
    if not pattern:
        return True
    pat = pattern.lower()
    glob = any(ch in pat for ch in "*?[")
    for field in fields:
        if field is None:
            continue
        value = field.lower()
        if (glob and fnmatch.fnmatchcase(value, pat)) or (not glob and pat in value):
            return True
    return False


def _since(hours: float) -> str:
    """ISO timestamp ``hours`` before now (UTC).

    Args:
        hours: Look-back window.

    Returns:
        ISO 8601 timestamp.
    """
    return (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)).isoformat()


def _as_objects(value: JsonValue) -> list[JsonObject]:
    """Narrow a JSON value to a list of objects.

    Args:
        value: Decoded API answer.

    Returns:
        The objects in ``value``.

    Raises:
        HassError: If ``value`` is not a list.
    """
    if not isinstance(value, list):
        raise HassError(f"expected a list, got {type(value).__name__}")
    return [item for item in value if isinstance(item, dict)]


def _s(value: JsonValue) -> str:
    """Render a scalar for table output.

    Args:
        value: Any JSON value.

    Returns:
        ``""`` for ``None``, else ``str(value)``.
    """
    return "" if value is None else str(value)


@app.command()
def info(ctx: typer.Context) -> None:
    """Show version, location and the authenticated user.

    Args:
        ctx: Typer context.
    """
    config = _cfg(ctx)
    with HassRest(config) as rest, HassWebSocket(config) as ws:
        cfg = rest.get_object("/config")
        user = ws.call_object("auth/current_user")
    _dump(
        {
            "url": config.url,
            "version": cfg.get("version"),
            "location_name": cfg.get("location_name"),
            "time_zone": cfg.get("time_zone"),
            "user": user.get("name"),
            "is_admin": user.get("is_admin"),
        }
    )


@app.command()
def states(
    ctx: typer.Context,
    pattern: Annotated[str | None, typer.Argument(help="Substring or glob on entity_id/friendly_name.")] = None,
    domain: Annotated[str | None, typer.Option(help="Only this domain, e.g. light.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print full state objects.")] = False,
) -> None:
    """List current states.

    Args:
        ctx: Typer context.
        pattern: Filter on entity_id or friendly name.
        domain: Domain filter.
        as_json: Print the raw state objects.
    """
    with HassRest(_cfg(ctx)) as rest:
        items = rest.states()
    selected: list[State] = []
    for st in items:
        entity_id = _s(st.get("entity_id"))
        attrs = st.get("attributes")
        name = _s(attrs.get("friendly_name")) if isinstance(attrs, dict) else ""
        if domain and not entity_id.startswith(domain + "."):
            continue
        if _matches(pattern, entity_id, name):
            selected.append(st)
    if as_json:
        _dump(selected)
        return
    rows = []
    for st in sorted(selected, key=lambda s: _s(s.get("entity_id"))):
        attrs = st.get("attributes")
        unit = _s(attrs.get("unit_of_measurement")) if isinstance(attrs, dict) else ""
        name = _s(attrs.get("friendly_name")) if isinstance(attrs, dict) else ""
        rows.append([_s(st.get("entity_id")), f"{_s(st.get('state'))} {unit}".strip(), name])
    _table(rows, ["ENTITY_ID", "STATE", "NAME"])


@app.command()
def state(ctx: typer.Context, entity_id: str, as_yaml: YamlOpt = False) -> None:
    """Show the full state object of one entity.

    Args:
        ctx: Typer context.
        entity_id: Entity to show.
        as_yaml: Print YAML.
    """
    with HassRest(_cfg(ctx)) as rest:
        _dump(rest.get(f"/states/{entity_id}"), as_yaml)


@app.command()
def call(
    ctx: typer.Context,
    service: Annotated[str, typer.Argument(help="domain.service, e.g. light.turn_on.")],
    kv: KvArg = None,
    data: DataOpt = None,
    return_response: Annotated[bool, typer.Option(help="Request the service response.")] = False,
) -> None:
    """Call a service.

    Args:
        ctx: Typer context.
        service: ``domain.service``.
        kv: Service data as key=value pairs.
        data: Service data as JSON.
        return_response: Ask for the service response (services that support it).
    """
    domain, _, name = service.partition(".")
    if not name:
        raise typer.BadParameter("expected domain.service")
    params = {"return_response": "true"} if return_response else None
    with HassRest(_cfg(ctx)) as rest:
        _dump(rest.post(f"/services/{domain}/{name}", _parse_kv(kv, data), params))


@app.command()
def ws(
    ctx: typer.Context,
    msg_type: Annotated[str, typer.Argument(metavar="TYPE", help="Command type, e.g. config/area_registry/list.")],
    kv: KvArg = None,
    data: DataOpt = None,
    as_yaml: YamlOpt = False,
) -> None:
    """Send a raw WebSocket command and print its result.

    Args:
        ctx: Typer context.
        msg_type: WebSocket command type.
        kv: Command fields as key=value pairs.
        data: Command fields as JSON.
        as_yaml: Print YAML.
    """
    with HassWebSocket(_cfg(ctx)) as conn:
        _dump(conn.call(msg_type, **_parse_kv(kv, data)), as_yaml)


@app.command()
def template(ctx: typer.Context, text: Annotated[str, typer.Argument(help="Jinja template; '-' reads stdin.")]) -> None:
    """Render a template on the server.

    Args:
        ctx: Typer context.
        text: Template source.
    """
    source = typer.get_text_stream("stdin").read() if text == "-" else text
    with HassRest(_cfg(ctx)) as rest:
        typer.echo(_s(rest.post("/template", {"template": source})))


@app.command()
def history(
    ctx: typer.Context,
    entity_ids: Annotated[list[str], typer.Argument(help="One or more entity IDs.")],
    hours: Annotated[float, typer.Option(help="Look-back window.")] = 24.0,
    attributes: Annotated[bool, typer.Option(help="Include attributes (JSON output).")] = False,
) -> None:
    """Show recorded state changes.

    Args:
        ctx: Typer context.
        entity_ids: Entities to query.
        hours: Look-back window in hours.
        attributes: Print full JSON including attributes.
    """
    params = {"filter_entity_id": ",".join(entity_ids)}
    if not attributes:
        params |= {"minimal_response": "", "no_attributes": ""}
    with HassRest(_cfg(ctx)) as rest:
        result = rest.get(f"/history/period/{_since(hours)}", params)
    if attributes:
        _dump(result)
        return
    if not isinstance(result, list):
        raise HassError("unexpected history answer")
    for series in result:
        points = _as_objects(series)
        if not points:
            continue
        typer.echo(f"== {_s(points[0].get('entity_id'))}")
        for point in points:
            typer.echo(f"{_s(point.get('last_changed'))}  {_s(point.get('state'))}")


@app.command()
def logbook(
    ctx: typer.Context,
    entity_id: Annotated[str | None, typer.Argument(help="Limit to one entity.")] = None,
    hours: Annotated[float, typer.Option(help="Look-back window.")] = 24.0,
) -> None:
    """Show logbook entries (who/what changed something).

    Args:
        ctx: Typer context.
        entity_id: Optional entity filter.
        hours: Look-back window in hours.
    """
    params = {"entity": entity_id} if entity_id else None
    with HassRest(_cfg(ctx)) as rest:
        entries = rest.get_list(f"/logbook/{_since(hours)}", params)
    for e in entries:
        context = " ".join(
            f"{k}={_s(e.get(k))}"
            for k in (
                "context_user_id",
                "context_event_type",
                "context_domain",
                "context_service",
                "context_entity_id",
                "context_name",
            )
            if e.get(k)
        )
        line = f"{_s(e.get('when'))}  {_s(e.get('entity_id'))}  {_s(e.get('state') or e.get('message'))}"
        typer.echo(f"{line}  [{context}]" if context else line)


def _area_names(conn: HassWebSocket) -> dict[str, str]:
    """Map area IDs to names.

    Args:
        conn: Open WebSocket connection.

    Returns:
        ``{area_id: name}``.
    """
    return {_s(a.get("area_id")): _s(a.get("name")) for a in conn.area_registry()}


def _resolve_area(conn: HassWebSocket, area: str) -> str | None:
    """Resolve an area name or ID; empty string clears the area.

    Args:
        conn: Open WebSocket connection.
        area: Area name or ID (case-insensitive), or ``""``.

    Returns:
        The area ID, or ``None`` to clear.

    Raises:
        typer.BadParameter: If no area matches.
    """
    if area == "":
        return None
    for area_id, name in _area_names(conn).items():
        if area.lower() in (area_id.lower(), name.lower()):
            return area_id
    raise typer.BadParameter(f"unknown area {area!r}")


@app.command()
def areas(ctx: typer.Context) -> None:
    """List areas.

    Args:
        ctx: Typer context.
    """
    with HassWebSocket(_cfg(ctx)) as conn:
        rows = [[a, n] for a, n in sorted(_area_names(conn).items(), key=lambda x: x[1].lower())]
    _table(rows, ["AREA_ID", "NAME"])


@app.command()
def devices(
    ctx: typer.Context,
    pattern: Annotated[str | None, typer.Argument(help="Substring or glob on name/manufacturer/model.")] = None,
) -> None:
    """List devices from the device registry.

    Args:
        ctx: Typer context.
        pattern: Filter.
    """
    with HassWebSocket(_cfg(ctx)) as conn:
        area_names = _area_names(conn)
        items = conn.device_registry()
    rows = []
    for d in items:
        name = _s(d.get("name_by_user") or d.get("name"))
        if not _matches(pattern, name, _s(d.get("manufacturer")), _s(d.get("model"))):
            continue
        area = area_names.get(_s(d.get("area_id")), "")
        rows.append([_s(d.get("id")), name, area, f"{_s(d.get('manufacturer'))} {_s(d.get('model'))}".strip()])
    _table(sorted(rows, key=lambda r: r[1].lower()), ["DEVICE_ID", "NAME", "AREA", "MODEL"])


@app.command()
def entities(
    ctx: typer.Context,
    pattern: Annotated[str | None, typer.Argument(help="Substring or glob on entity_id/name.")] = None,
    area: Annotated[str | None, typer.Option(help="Only this area (name or ID, incl. device area).")] = None,
    platform: Annotated[str | None, typer.Option(help="Only this integration, e.g. hue.")] = None,
    include_disabled: Annotated[bool, typer.Option(help="Also list disabled entities.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print raw registry entries.")] = False,
) -> None:
    """List the entity registry with resolved areas.

    Args:
        ctx: Typer context.
        pattern: Filter on entity_id or name.
        area: Area filter; an entity without own area inherits its device's area.
        platform: Integration filter.
        include_disabled: Include disabled entities.
        as_json: Print raw registry entries.
    """
    with HassWebSocket(_cfg(ctx)) as conn:
        area_names = _area_names(conn)
        device_area = {_s(d.get("id")): _s(d.get("area_id")) for d in conn.device_registry()}
        items = conn.entity_registry()
        area_id = _resolve_area(conn, area) if area else None
    selected: list[EntityRegistryEntry] = []
    rows = []
    for e in items:
        if e.get("disabled_by") and not include_disabled:
            continue
        if platform and e.get("platform") != platform:
            continue
        eff_area = _s(e.get("area_id")) or device_area.get(_s(e.get("device_id")), "")
        if area and eff_area != area_id:
            continue
        name = _s(e.get("name") or e.get("original_name"))
        if not _matches(pattern, _s(e.get("entity_id")), name):
            continue
        selected.append(e)
        flags = ",".join(f for f, k in (("disabled", "disabled_by"), ("hidden", "hidden_by")) if e.get(k))
        rows.append([_s(e.get("entity_id")), name, area_names.get(eff_area, ""), _s(e.get("platform")), flags])
    if as_json:
        _dump(selected)
        return
    _table(sorted(rows), ["ENTITY_ID", "NAME", "AREA", "PLATFORM", "FLAGS"])


@app.command("entity-update")
def entity_update(
    ctx: typer.Context,
    entity_id: str,
    name: Annotated[str | None, typer.Option(help="New display name; '' resets to the default.")] = None,
    icon: Annotated[str | None, typer.Option(help="mdi:… icon; '' resets.")] = None,
    area: Annotated[str | None, typer.Option(help="Area name or ID; '' clears (device area applies).")] = None,
    new_entity_id: Annotated[str | None, typer.Option(help="Rename the entity_id.")] = None,
    enabled: Annotated[bool | None, typer.Option("--enable/--disable", help="Enable or disable.")] = None,
    visible: Annotated[bool | None, typer.Option("--unhide/--hide", help="Show or hide.")] = None,
    label: Annotated[list[str] | None, typer.Option(help="Replace labels (repeatable label IDs).")] = None,
) -> None:
    """Change an entity registry entry.

    Args:
        ctx: Typer context.
        entity_id: Entity to change.
        name: Display name.
        icon: Icon.
        area: Area name or ID.
        new_entity_id: New entity ID.
        enabled: Enable or disable the entity.
        visible: Unhide or hide the entity.
        label: Label IDs replacing the current set.
    """
    with HassWebSocket(_cfg(ctx)) as conn:
        changes: JsonObject = {}
        if name is not None:
            changes["name"] = name or None
        if icon is not None:
            changes["icon"] = icon or None
        if area is not None:
            changes["area_id"] = _resolve_area(conn, area)
        if new_entity_id:
            changes["new_entity_id"] = new_entity_id
        if enabled is not None:
            changes["disabled_by"] = None if enabled else "user"
        if visible is not None:
            changes["hidden_by"] = None if visible else "user"
        if label is not None:
            changes["labels"] = list[JsonValue](label)
        if not changes:
            raise typer.BadParameter("nothing to change")
        _dump(conn.call("config/entity_registry/update", entity_id=entity_id, **changes))


def _check_helper_type(helper_type: str) -> str:
    """Validate a helper type.

    Args:
        helper_type: Requested type.

    Returns:
        The type.

    Raises:
        typer.BadParameter: For unsupported types.
    """
    if helper_type not in HELPER_TYPES:
        raise typer.BadParameter(f"supported: {', '.join(HELPER_TYPES)}")
    return helper_type


HelperTypeArg = Annotated[str, typer.Argument(help=f"One of: {', '.join(HELPER_TYPES)}.", callback=_check_helper_type)]


@helper_app.command("list")
def helper_list(ctx: typer.Context, helper_type: HelperTypeArg, as_yaml: YamlOpt = False) -> None:
    """List storage helpers of one type.

    Args:
        ctx: Typer context.
        helper_type: Helper type.
        as_yaml: Print YAML.
    """
    with HassWebSocket(_cfg(ctx)) as conn:
        _dump(conn.call(f"{helper_type}/list"), as_yaml)


@helper_app.command("create")
def helper_create(ctx: typer.Context, helper_type: HelperTypeArg, kv: KvArg = None, data: DataOpt = None) -> None:
    """Create a helper, e.g. ``helper create input_number name=Soll min=0 max=300 step=5``.

    Args:
        ctx: Typer context.
        helper_type: Helper type.
        kv: Fields as key=value pairs (``name`` is required).
        data: Fields as JSON.
    """
    fields = _parse_kv(kv, data)
    if "name" not in fields:
        raise typer.BadParameter("name=… is required")
    with HassWebSocket(_cfg(ctx)) as conn:
        _dump(conn.call(f"{helper_type}/create", **fields))


@helper_app.command("update")
def helper_update(
    ctx: typer.Context, helper_type: HelperTypeArg, helper_id: str, kv: KvArg = None, data: DataOpt = None
) -> None:
    """Update a helper by its storage ID (the ``id`` from ``helper list``).

    Args:
        ctx: Typer context.
        helper_type: Helper type.
        helper_id: Storage ID.
        kv: Changed fields as key=value pairs.
        data: Changed fields as JSON.
    """
    with HassWebSocket(_cfg(ctx)) as conn:
        _dump(conn.call(f"{helper_type}/update", **{f"{helper_type}_id": helper_id}, **_parse_kv(kv, data)))


@helper_app.command("delete")
def helper_delete(ctx: typer.Context, helper_type: HelperTypeArg, helper_id: str, yes: YesOpt = False) -> None:
    """Delete a helper by its storage ID.

    Args:
        ctx: Typer context.
        helper_type: Helper type.
        helper_id: Storage ID.
        yes: Skip confirmation.
    """
    if not yes:
        typer.confirm(f"delete {helper_type} {helper_id}?", abort=True)
    with HassWebSocket(_cfg(ctx)) as conn:
        _dump(conn.call(f"{helper_type}/delete", **{f"{helper_type}_id": helper_id}))


def _config_id(rest: HassRest, kind: ConfigKind, ref: str) -> str:
    """Map an entity ID or config ID to the config-store ID.

    Args:
        rest: REST client.
        kind: ``automation`` or ``script``.
        ref: ``automation.foo``/``script.foo`` or the bare config ID.

    Returns:
        The ID used by ``/api/config/<kind>/config/<id>``.

    Raises:
        HassError: If an automation entity has no ``id`` attribute (YAML-defined).
    """
    if not ref.startswith(kind + "."):
        return ref
    if kind == "script":
        return ref.removeprefix("script.")
    attrs = rest.state(ref).get("attributes")
    config_id = attrs.get("id") if attrs else None
    if not config_id:
        raise HassError(f"{ref} has no id attribute (not managed by the UI config store)")
    return str(config_id)


def _config_list(ctx: typer.Context, kind: ConfigKind, pattern: str | None) -> None:
    """List automations or scripts with their config IDs.

    Args:
        ctx: Typer context.
        kind: ``automation`` or ``script``.
        pattern: Filter on entity_id/name.
    """
    with HassRest(_cfg(ctx)) as rest:
        items = rest.states()
    rows = []
    for st in items:
        entity_id = _s(st.get("entity_id"))
        if not entity_id.startswith(kind + "."):
            continue
        attrs = st.get("attributes")
        attrs = attrs if isinstance(attrs, dict) else {}
        name = _s(attrs.get("friendly_name"))
        if not _matches(pattern, entity_id, name):
            continue
        config_id = _s(attrs.get("id")) if kind == "automation" else entity_id.removeprefix("script.")
        rows.append([entity_id, config_id, _s(st.get("state")), _s(attrs.get("last_triggered")), name])
    _table(sorted(rows), ["ENTITY_ID", "CONFIG_ID", "STATE", "LAST_TRIGGERED", "NAME"])


def _config_get(ctx: typer.Context, kind: ConfigKind, ref: str, as_yaml: bool) -> None:
    """Print one automation/script config.

    Args:
        ctx: Typer context.
        kind: ``automation`` or ``script``.
        ref: Entity ID or config ID.
        as_yaml: Print YAML.
    """
    with HassRest(_cfg(ctx)) as rest:
        _dump(rest.get(f"/config/{kind}/config/{_config_id(rest, kind, ref)}"), as_yaml)


def _config_put(ctx: typer.Context, kind: ConfigKind, ref: str, file: Path, dry_run: bool) -> None:
    """Create or replace an automation/script from a YAML/JSON file, showing the diff first.

    Args:
        ctx: Typer context.
        kind: ``automation`` or ``script``.
        ref: Entity ID or config ID; a new config ID creates a new item.
        file: YAML/JSON file (``-`` = stdin).
        dry_run: Only show the diff.

    Raises:
        typer.BadParameter: If the file is not a mapping.
    """
    new = _load_file(file)
    if not isinstance(new, dict):
        raise typer.BadParameter("file must contain a mapping")
    with HassRest(_cfg(ctx)) as rest:
        config_id = _config_id(rest, kind, ref)
        if kind == "automation":
            new.setdefault("id", config_id)
        try:
            old: JsonValue = rest.get(f"/config/{kind}/config/{config_id}")
        except HassError as err:
            if err.status_code != 404:
                raise
            old = None
        if not _show_diff(old, new, f"{kind}/{config_id}") or dry_run:
            return
        _dump(rest.post(f"/config/{kind}/config/{config_id}", new))


def _config_delete(ctx: typer.Context, kind: ConfigKind, ref: str, yes: bool) -> None:
    """Delete an automation/script from the config store.

    Args:
        ctx: Typer context.
        kind: ``automation`` or ``script``.
        ref: Entity ID or config ID.
        yes: Skip confirmation.
    """
    with HassRest(_cfg(ctx)) as rest:
        config_id = _config_id(rest, kind, ref)
        if not yes:
            typer.confirm(f"delete {kind} {config_id}?", abort=True)
        _dump(rest.delete(f"/config/{kind}/config/{config_id}"))


PatternArg = Annotated[str | None, typer.Argument(help="Substring or glob on entity_id/name.")]
RefArg = Annotated[str, typer.Argument(help="Entity ID (automation.x / script.x) or config ID.")]
FileArg = Annotated[Path, typer.Argument(help="YAML or JSON file, '-' for stdin.")]


@automation_app.command("list")
def automation_list(ctx: typer.Context, pattern: PatternArg = None) -> None:
    """List automations.

    Args:
        ctx: Typer context.
        pattern: Filter.
    """
    _config_list(ctx, "automation", pattern)


@automation_app.command("get")
def automation_get(ctx: typer.Context, ref: RefArg, as_yaml: YamlOpt = False) -> None:
    """Print an automation config.

    Args:
        ctx: Typer context.
        ref: Entity ID or config ID.
        as_yaml: Print YAML.
    """
    _config_get(ctx, "automation", ref, as_yaml)


@automation_app.command("put")
def automation_put(ctx: typer.Context, ref: RefArg, file: FileArg, dry_run: DryRunOpt = False) -> None:
    """Create or replace an automation (diff is shown first).

    Args:
        ctx: Typer context.
        ref: Entity ID or config ID.
        file: YAML/JSON file.
        dry_run: Only show the diff.
    """
    _config_put(ctx, "automation", ref, file, dry_run)


@automation_app.command("delete")
def automation_delete(ctx: typer.Context, ref: RefArg, yes: YesOpt = False) -> None:
    """Delete an automation.

    Args:
        ctx: Typer context.
        ref: Entity ID or config ID.
        yes: Skip confirmation.
    """
    _config_delete(ctx, "automation", ref, yes)


@script_app.command("list")
def script_list(ctx: typer.Context, pattern: PatternArg = None) -> None:
    """List scripts.

    Args:
        ctx: Typer context.
        pattern: Filter.
    """
    _config_list(ctx, "script", pattern)


@script_app.command("get")
def script_get(ctx: typer.Context, ref: RefArg, as_yaml: YamlOpt = False) -> None:
    """Print a script config.

    Args:
        ctx: Typer context.
        ref: Entity ID or config ID.
        as_yaml: Print YAML.
    """
    _config_get(ctx, "script", ref, as_yaml)


@script_app.command("put")
def script_put(ctx: typer.Context, ref: RefArg, file: FileArg, dry_run: DryRunOpt = False) -> None:
    """Create or replace a script (diff is shown first).

    Args:
        ctx: Typer context.
        ref: Entity ID or config ID.
        file: YAML/JSON file.
        dry_run: Only show the diff.
    """
    _config_put(ctx, "script", ref, file, dry_run)


@script_app.command("delete")
def script_delete(ctx: typer.Context, ref: RefArg, yes: YesOpt = False) -> None:
    """Delete a script.

    Args:
        ctx: Typer context.
        ref: Entity ID or config ID.
        yes: Skip confirmation.
    """
    _config_delete(ctx, "script", ref, yes)


TraceDomainOpt = Annotated[str, typer.Option(help="automation or script.")]


def _trace_item_id(conn_rest: HassRest, domain: str, ref: str) -> str:
    """Map an entity ID to the trace item ID.

    Args:
        conn_rest: REST client.
        domain: ``automation`` or ``script``.
        ref: Entity ID or item ID.

    Returns:
        Item ID as used by ``trace/*``.

    Raises:
        typer.BadParameter: For other domains.
    """
    if domain not in ("automation", "script"):
        raise typer.BadParameter("domain must be automation or script")
    kind: ConfigKind = "automation" if domain == "automation" else "script"
    return _config_id(conn_rest, kind, ref)


def _trace_start(trace: JsonObject) -> str:
    """Start timestamp of a trace summary, for sorting.

    Args:
        trace: Entry from ``trace/list``.

    Returns:
        ISO timestamp or ``""``.
    """
    ts = trace.get("timestamp")
    return _s(ts.get("start")) if isinstance(ts, dict) else ""


@trace_app.command("list")
def trace_list(
    ctx: typer.Context,
    ref: Annotated[str | None, typer.Argument(help="Entity ID or item ID; all items if omitted.")] = None,
    domain: TraceDomainOpt = "automation",
) -> None:
    """List stored traces (newest last).

    Args:
        ctx: Typer context.
        ref: Optional automation/script.
        domain: ``automation`` or ``script``.
    """
    config = _cfg(ctx)
    with HassRest(config) as rest, HassWebSocket(config) as conn:
        payload: JsonObject = {"domain": domain}
        if ref:
            payload["item_id"] = _trace_item_id(rest, domain, ref)
        traces = conn.call_list("trace/list", **payload)
    rows = []
    for t in traces:
        error = _s(t.get("error"))
        rows.append(
            [
                _trace_start(t),
                _s(t.get("item_id")),
                _s(t.get("run_id")),
                _s(t.get("script_execution")),
                _s(t.get("last_step")),
                _s(t.get("trigger")),
                error,
            ]
        )
    _table(sorted(rows), ["START", "ITEM_ID", "RUN_ID", "RESULT", "LAST_STEP", "TRIGGER", "ERROR"])


@trace_app.command("get")
def trace_get(
    ctx: typer.Context,
    ref: RefArg,
    run_id: Annotated[str | None, typer.Argument(help="Run ID; newest run if omitted.")] = None,
    domain: TraceDomainOpt = "automation",
    as_yaml: YamlOpt = False,
) -> None:
    """Print one full trace (trigger, condition results, every step with variables).

    Args:
        ctx: Typer context.
        ref: Entity ID or item ID.
        run_id: Run to show.
        domain: ``automation`` or ``script``.
        as_yaml: Print YAML.

    Raises:
        HassError: If no trace exists.
    """
    config = _cfg(ctx)
    with HassRest(config) as rest, HassWebSocket(config) as conn:
        item_id = _trace_item_id(rest, domain, ref)
        if run_id is None:
            traces = conn.call_list("trace/list", domain=domain, item_id=item_id)
            if not traces:
                raise HassError(f"no traces for {domain} {item_id}")
            run_id = _s(max(traces, key=_trace_start).get("run_id"))
        _dump(conn.call("trace/get", domain=domain, item_id=item_id, run_id=run_id), as_yaml)


DashboardArg = Annotated[str | None, typer.Argument(help="url_path; default dashboard if omitted.")]


@dashboard_app.command("list")
def dashboard_list(ctx: typer.Context) -> None:
    """List dashboards.

    Args:
        ctx: Typer context.
    """
    with HassWebSocket(_cfg(ctx)) as conn:
        items = conn.call_list("lovelace/dashboards/list")
    rows = [[_s(d.get("url_path")), _s(d.get("title")), _s(d.get("mode")), _s(d.get("id"))] for d in items]
    _table(sorted(rows), ["URL_PATH", "TITLE", "MODE", "ID"])


@dashboard_app.command("get")
def dashboard_get(ctx: typer.Context, url_path: DashboardArg = None, as_yaml: YamlOpt = False) -> None:
    """Print a dashboard config.

    Args:
        ctx: Typer context.
        url_path: Dashboard URL path.
        as_yaml: Print YAML.
    """
    with HassWebSocket(_cfg(ctx)) as conn:
        _dump(conn.call("lovelace/config", url_path=url_path), as_yaml)


@dashboard_app.command("save")
def dashboard_save(
    ctx: typer.Context,
    file: FileArg,
    url_path: DashboardArg = None,
    dry_run: DryRunOpt = False,
) -> None:
    """Replace a storage-mode dashboard config (diff is shown first).

    Args:
        ctx: Typer context.
        file: YAML/JSON file.
        url_path: Dashboard URL path.
        dry_run: Only show the diff.

    Raises:
        typer.BadParameter: If the file is not a mapping.
    """
    new = _load_file(file)
    if not isinstance(new, dict):
        raise typer.BadParameter("file must contain a mapping")
    with HassWebSocket(_cfg(ctx)) as conn:
        old = conn.call("lovelace/config", url_path=url_path)
        if not _show_diff(old, new, f"dashboard/{url_path or 'default'}") or dry_run:
            return
        conn.call("lovelace/config/save", url_path=url_path, config=new)
    typer.echo("saved")


def run() -> None:
    """Run the CLI and turn API, HTTP, WebSocket and OS errors into exit code 1."""
    try:
        app()
    except (HassError, httpx.HTTPError, WebSocketException, OSError) as err:
        message = str(err) if isinstance(err, HassError) else f"{type(err).__name__}: {err}"
        typer.echo(f"error: {message}", err=True)
        raise SystemExit(1) from err


if __name__ == "__main__":
    run()
