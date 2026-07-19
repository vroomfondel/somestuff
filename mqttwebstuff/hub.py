"""The view hub: last-value cache + SSE fan-out between MQTT and the browsers.

The hub is the piece that turns a *non-retained* live stream into something a
freshly opened browser tab can show immediately: every mapped message is
rendered once and kept in an in-memory cache per ``(panel, key)``, so the
initial page load renders the full current board server-side and the SSE
stream afterwards only carries deltas.

Threading model: paho-mqtt delivers messages on its own network thread;
:meth:`ViewHub.submit` is the only thread-safe entry point and trampolines
into the asyncio event loop (``call_soon_threadsafe``). Everything else —
ingest, rendering, cache, subscriber queues — runs single-threaded on the
event loop, so no locks are needed.

Update granularity: one SSE event per *panel* (the panel's full body is
re-rendered from cache and swapped via htmx ``sse-swap``). At the message
rates of a poll-based stream this is trivially cheap and makes ordering,
replacement and expiry correct by construction. Only when a message creates a
panel the initial page did not know about is the whole board re-sent once
(event ``board``) so the new section materializes without a reload.
"""

import asyncio
import html
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import jinja2

from mqttwebstuff.plugin_api import GENERIC_TEMPLATE, LoadedPlugin, ViewEvent

logger = logging.getLogger(__name__)

#: Per-subscriber queue depth; a browser that cannot drain this many panel
#: updates gets the oldest ones dropped (the next update repaints the panel
#: fully anyway, so drops only cost intermediate states).
_QUEUE_MAXSIZE = 256


@dataclass(slots=True)
class _Entry:
    """One cached board item: pre-rendered HTML plus ordering/expiry."""

    html: str
    sort: str
    expires: float | None


def decode_payload(raw: str | bytes) -> Any:
    """Decode a raw MQTT payload: JSON when it parses, the string otherwise.

    Args:
        raw: The payload as delivered by the broker.

    Returns:
        ``dict``/``list``/scalar for valid JSON, otherwise the decoded string.
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return text


class ViewHub:
    """Cache + broadcast hub; single-threaded on the asyncio event loop.

    Args:
        env: Jinja2 environment used to render item templates.
        plugin: The mapper plugin deciding filter/panel/template per message.
    """

    def __init__(self, env: jinja2.Environment, plugin: LoadedPlugin) -> None:
        self._env = env
        self._plugin = plugin
        # Declared panels exist from the start (stable board layout even while
        # empty); undeclared ones are appended when their first item arrives.
        self._panels: dict[str, dict[str, _Entry]] = {name: {} for name in plugin.panels}
        self._subscribers: set[asyncio.Queue[tuple[str, str]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def plugin(self) -> LoadedPlugin:
        """The mapper plugin this hub runs."""
        return self._plugin

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the hub to the running event loop (called from app startup).

        Args:
            loop: The loop all ingest/broadcast work must run on.
        """
        self._loop = loop

    # ── thread-safe entry point (paho network thread) ───────────────────────

    def submit(self, topic: str, raw: str | bytes) -> None:
        """Hand one MQTT message over to the event loop; safe from any thread.

        Messages arriving before the loop is attached (or after shutdown) are
        dropped — with a non-retained live stream the next cycle re-delivers
        the current state anyway.

        Args:
            topic: Full MQTT topic of the message.
            raw: Raw payload.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        payload = decode_payload(raw)
        try:
            loop.call_soon_threadsafe(self.ingest, topic, payload)
        except RuntimeError:
            # Loop shut down between the check and the call — shutdown race, drop.
            pass

    # ── event-loop side ─────────────────────────────────────────────────────

    def ingest(self, topic: str, payload: Any) -> None:
        """Map, render and cache one message, then broadcast the delta.

        A crashing mapper or template must never take the stream down: errors
        are logged and the message is dropped.

        Args:
            topic: Full MQTT topic of the message.
            payload: Decoded payload (see :func:`decode_payload`).
        """
        try:
            event = self._plugin.map_message(topic, payload)
        except Exception:
            logger.exception(f"mapper failed for topic {topic}")
            return
        if event is None:
            return
        try:
            rendered = self._render_item(event, topic)
        except Exception:
            logger.exception(f"template {event.template or GENERIC_TEMPLATE} failed for topic {topic}")
            return

        new_panel = event.panel not in self._panels
        panel = self._panels.setdefault(event.panel, {})
        panel[event.key] = _Entry(
            html=rendered,
            sort=event.sort or event.key,
            expires=(time.monotonic() + event.ttl) if event.ttl is not None else None,
        )
        if new_panel:
            self._broadcast("board", self.render_board())
        else:
            self._broadcast(f"panel:{event.panel}", self.render_panel_body(event.panel))

    def _render_item(self, event: ViewEvent, topic: str) -> str:
        """Render one item to its HTML fragment.

        Args:
            event: The mapped view event.
            topic: Originating topic, exposed to the template as ``topic``.

        Returns:
            The rendered fragment.
        """
        template = self._env.get_template(event.template or GENERIC_TEMPLATE)
        return template.render(data=event.data, topic=topic, key=event.key, panel=event.panel, title=event.title)

    def render_panel_body(self, name: str) -> str:
        """Render a panel's body: all items in sort order, or a placeholder.

        Args:
            name: Panel name.

        Returns:
            HTML for the panel's inner container.
        """
        entries = self._panels.get(name, {})
        if not entries:
            return '<p class="mqttweb-empty">– keine Daten –</p>'
        ordered = sorted(entries.values(), key=lambda e: e.sort)
        return "\n".join(e.html for e in ordered)

    def render_board(self) -> str:
        """Render the whole board (all panels) from cache.

        Declared panels keep their declared order; panels created at runtime
        follow alphabetically.

        Returns:
            HTML for the board container.
        """
        declared = [name for name in self._plugin.panels if name in self._panels]
        extra = sorted(name for name in self._panels if name not in self._plugin.panels)
        sections: list[str] = []
        for name in declared + extra:
            heading = self._plugin.panels.get(name, name)
            sections.append(
                f'<section class="mqttweb-panel" id="panel-{html.escape(name, quote=True)}">'
                f"<h2>{html.escape(heading)}</h2>"
                f'<div class="mqttweb-panel-body" sse-swap="panel:{html.escape(name, quote=True)}">'
                f"{self.render_panel_body(name)}</div></section>"
            )
        return "\n".join(sections)

    # ── expiry ──────────────────────────────────────────────────────────────

    def sweep_once(self, now: float | None = None) -> list[str]:
        """Drop expired items and broadcast every panel that changed.

        Args:
            now: Reference monotonic time (defaults to ``time.monotonic()``;
                injectable for tests).

        Returns:
            Names of the panels that lost at least one item.
        """
        now = time.monotonic() if now is None else now
        changed: list[str] = []
        for name, entries in self._panels.items():
            expired = [key for key, entry in entries.items() if entry.expires is not None and entry.expires <= now]
            if not expired:
                continue
            for key in expired:
                del entries[key]
            changed.append(name)
            self._broadcast(f"panel:{name}", self.render_panel_body(name))
        return changed

    async def sweeper(self, interval: float = 5.0) -> None:
        """Run :meth:`sweep_once` forever; cancelled on app shutdown.

        Args:
            interval: Seconds between sweeps.
        """
        while True:
            await asyncio.sleep(interval)
            self.sweep_once()

    # ── SSE subscribers ─────────────────────────────────────────────────────

    def subscribe(self) -> "asyncio.Queue[tuple[str, str]]":
        """Register a new SSE subscriber.

        Returns:
            The subscriber's ``(event_name, html)`` queue.
        """
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[tuple[str, str]]") -> None:
        """Remove an SSE subscriber.

        Args:
            queue: The queue previously returned by :meth:`subscribe`.
        """
        self._subscribers.discard(queue)

    def _broadcast(self, event: str, fragment: str) -> None:
        """Fan one event out to all subscribers, dropping oldest on backlog.

        Args:
            event: SSE event name (``panel:<name>`` or ``board``).
            fragment: The rendered HTML payload.
        """
        for queue in self._subscribers:
            try:
                queue.put_nowait((event, fragment))
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait((event, fragment))
                except asyncio.QueueFull:
                    pass
