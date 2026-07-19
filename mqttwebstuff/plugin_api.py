"""Contract between the mqttweb core and a mounted mapper plugin.

A mapper plugin is loaded at startup via :func:`load_plugin`, given either as
a plain Python *file* (in Kubernetes typically a ConfigMap mount) or as a
dotted *module name* on the import path (e.g.
``mqttwebstuff.plugins.oepnv_view``). It declares what to subscribe to and
turns every incoming MQTT message into a :class:`ViewEvent` (or ``None`` to
drop it). The core knows nothing about any concrete topic layout.

Template resolution is CWD-independent in both cases: ``TEMPLATE_DIR`` (and
its ``templates/`` default) resolves relative to the directory containing the
module's source file; template *names* in :class:`ViewEvent` are plain Jinja2
loader names looked up there first, then in the package built-ins.

Required module attributes::

    SUBSCRIPTIONS: list[str]                     # MQTT subscribe patterns, e.g. ["oepnv/#"]

    def map_message(topic: str, payload: Any) -> ViewEvent | None: ...

Optional module attributes::

    TITLE: str                                   # page title (default: plugin module name)
    PANELS: dict[str, str]                       # panel name -> heading; fixes the board order.
                                                 # Empty heading = plain panel (no title, no chrome)
    TEMPLATE_DIR: str                            # Jinja2 template dir, relative to the plugin's own
                                                 # directory (default: <plugin dir>/templates)

``payload`` is the already-decoded message: a ``dict``/``list`` when the raw
payload parses as JSON, otherwise the raw string.
"""

import importlib.util
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, TypeAlias

logger = logging.getLogger(__name__)

#: Fallback template used when a :class:`ViewEvent` names none — a generic
#: card showing the payload as pretty-printed JSON.
GENERIC_TEMPLATE = "generic_item.html.j2"

#: Built-in template for the hierarchical generic view: one indented row per
#: topic segment (branch rows for structure, leaf rows with the value).
GENERIC_TREE_TEMPLATE = "generic_tree_item.html.j2"


@dataclass(frozen=True, slots=True)
class ViewEvent:
    """One mapped MQTT message: what to show, where, and how.

    Attributes:
        panel: Board section the item belongs to (e.g. ``"departures"``).
        key: Stable identity *within* the panel — a message with the same
            ``(panel, key)`` replaces the previous item instead of appending,
            which is what makes the board idempotent under re-publishes.
        data: Template context, available as ``data`` inside the template.
        template: Jinja2 template name rendering this item; ``None`` falls back
            to the built-in :data:`GENERIC_TEMPLATE`.
        sort: Sort key within the panel (ascending, lexicographic); empty
            string sorts by ``key``. ISO timestamps sort naturally. With a
            ``group`` set, the key orders items *within* that group.
        group: Optional display label grouping items inside the panel (e.g.
            the stop name on a departure board). Grouped items render under a
            sub-heading per label (labels sorted alphabetically); ungrouped
            items come first. Empty string = ungrouped.
        title: Optional item heading, used by the generic template.
        ttl: Seconds after which the item silently vanishes from the board
            unless re-published — mirrors the ``retain=False`` semantics of a
            live stream (topics that stop being published disappear).
            ``None`` keeps the item forever.
    """

    panel: str
    key: str
    data: Any
    template: str | None = None
    sort: str = ""
    group: str = ""
    title: str | None = None
    ttl: float | None = 300.0


#: What ``map_message`` may return: one item, several items (e.g. a leaf plus
#: synthesized ancestor rows for a tree view), or ``None`` to drop the message.
MapResult: TypeAlias = "ViewEvent | Sequence[ViewEvent] | None"


@dataclass(slots=True)
class LoadedPlugin:
    """A validated mapper plugin, ready for the hub.

    Attributes:
        subscriptions: MQTT subscribe patterns.
        map_message: The plugin's mapping function.
        panels: Panel name → heading, in board order. Panels not declared here
            still work — they appear at the end of the board when their first
            item arrives.
        title: Page title.
        template_dir: Plugin-provided Jinja2 template directory, or ``None``.
    """

    subscriptions: tuple[str, ...]
    map_message: Callable[[str, Any], MapResult]
    panels: dict[str, str] = field(default_factory=dict)
    title: str = "mqttweb"
    template_dir: Path | None = None


def _import_file(path: Path) -> ModuleType:
    """Import a Python source file as a throwaway module.

    Args:
        path: The plugin file.

    Returns:
        The executed module.

    Raises:
        ValueError: If the file cannot be imported.
    """
    module_name = f"mqttweb_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot import plugin file {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses/typing introspection inside the plugin works.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _import_mapper(mapper: str | Path) -> tuple[ModuleType, Path | None]:
    """Import a mapper given as a file path OR a dotted module name.

    Args:
        mapper: Path to a ``.py`` file (mounted plugin) or an importable module
            name such as ``mqttwebstuff.plugins.oepnv_view``.

    Returns:
        The module and its *anchor* directory (the directory containing the
        module's source file; ``None`` for sourceless modules). All relative
        template lookups resolve against the anchor, never against the CWD.

    Raises:
        ValueError: If the mapper is neither an existing file nor importable.
    """
    path = Path(mapper)
    if path.suffix == ".py" or path.is_file():
        if not path.is_file():
            raise ValueError(f"plugin file not found: {path}")
        return _import_file(path), path.parent

    try:
        module = importlib.import_module(str(mapper))
    except ImportError as exc:
        raise ValueError(f"mapper {mapper!r} is neither a plugin file nor an importable module: {exc}")
    module_file = getattr(module, "__file__", None)
    return module, Path(module_file).parent if module_file else None


def load_plugin(mapper: str | Path) -> LoadedPlugin:
    """Load and validate a mapper plugin.

    Args:
        mapper: Path to the plugin's ``.py`` file or a dotted module name (see
            module docstring for the expected attributes). Template resolution
            is identical either way: relative to the module's own directory.

    Returns:
        The validated plugin.

    Raises:
        ValueError: If the mapper is missing, not importable, or violates the
            contract (no ``SUBSCRIPTIONS``, no callable ``map_message``).
    """
    module, anchor = _import_mapper(mapper)

    subscriptions = getattr(module, "SUBSCRIPTIONS", None)
    if not isinstance(subscriptions, (list, tuple)) or not subscriptions:
        raise ValueError(f"plugin {mapper} must define a non-empty SUBSCRIPTIONS list")
    if not all(isinstance(s, str) and s for s in subscriptions):
        raise ValueError(f"plugin {mapper}: SUBSCRIPTIONS must contain non-empty strings")

    map_message = getattr(module, "map_message", None)
    if not callable(map_message):
        raise ValueError(f"plugin {mapper} must define map_message(topic, payload)")

    raw_panels = getattr(module, "PANELS", {})
    if isinstance(raw_panels, dict):
        panels = {str(k): str(v) for k, v in raw_panels.items()}
    elif isinstance(raw_panels, (list, tuple)):
        panels = {str(name): str(name) for name in raw_panels}
    else:
        raise ValueError(f"plugin {mapper}: PANELS must be a dict or list")

    # TEMPLATE_DIR (or the "templates" default) is anchored at the module's own
    # directory, so a mounted plugin brings its templates along regardless of
    # the process CWD; absolute paths pass through unchanged.
    template_dir_raw = getattr(module, "TEMPLATE_DIR", None)
    template_dir = Path(template_dir_raw) if template_dir_raw else Path("templates")
    if not template_dir.is_absolute():
        template_dir = anchor / template_dir if anchor is not None else template_dir
    resolved_template_dir = template_dir if template_dir.is_dir() else None

    plugin = LoadedPlugin(
        subscriptions=tuple(subscriptions),
        map_message=map_message,
        panels=panels,
        title=str(getattr(module, "TITLE", module.__name__.rsplit(".", 1)[-1].removeprefix("mqttweb_plugin_"))),
        template_dir=resolved_template_dir,
    )
    logger.info(
        f"loaded plugin {mapper} (title={plugin.title!r}, subscriptions={list(plugin.subscriptions)}, "
        f"panels={list(plugin.panels)}, templates={resolved_template_dir})"
    )
    return plugin


def generic_plugin(
    topics: list[str], *, title: str = "mqttweb", ttl: float | None = 900.0, hierarchical: bool = True
) -> LoadedPlugin:
    """Build the fallback plugin used when no mapper file is given.

    The panel is always the topic's first segment; each topic occupies one
    stable slot (new payloads replace the old one). Two layouts:

    * ``hierarchical`` (default): the topic tree as indented rows — one muted
      *branch* row per intermediate segment, one *leaf* row per topic carrying
      the latest value (scalars inline, JSON documents collapsible). Branch
      rows are synthesized from every message below them, so they stay alive
      exactly as long as their subtree does.
    * flat: one JSON card per topic, headed by the full topic path.

    Args:
        topics: MQTT subscribe patterns.
        title: Page title.
        ttl: Per-item lifetime in seconds (``None`` = forever).
        hierarchical: Choose the indented tree layout over flat cards.

    Returns:
        The ready-to-use plugin.

    Raises:
        ValueError: If ``topics`` is empty.
    """
    if not topics:
        raise ValueError("generic plugin needs at least one topic pattern")

    def _map_flat(topic: str, payload: Any) -> MapResult:
        panel = topic.split("/", 1)[0] or "messages"
        return ViewEvent(panel=panel, key=topic, data=payload, title=topic, ttl=ttl)

    def _map_tree(topic: str, payload: Any) -> MapResult:
        parts = [p for p in topic.split("/") if p]
        if not parts:
            return None
        panel = parts[0]
        events: list[ViewEvent] = []
        # One branch row per intermediate level; key/sort end in "/" so a
        # branch sorts directly before its children (and never collides with a
        # leaf published on the same path).
        for depth, segment in enumerate(parts[1:-1]):
            path = "/".join(parts[: depth + 2]) + "/"
            events.append(
                ViewEvent(
                    panel=panel,
                    key=path,
                    data={"label": segment, "depth": depth, "branch": True},
                    template=GENERIC_TREE_TEMPLATE,
                    sort=path,
                    ttl=ttl,
                )
            )
        events.append(
            ViewEvent(
                panel=panel,
                key=topic,
                data={"label": parts[-1], "depth": max(len(parts) - 2, 0), "branch": False, "value": payload},
                template=GENERIC_TREE_TEMPLATE,
                sort=topic,
                ttl=ttl,
            )
        )
        return events

    return LoadedPlugin(subscriptions=tuple(topics), map_message=_map_tree if hierarchical else _map_flat, title=title)
