"""FastAPI app: initial server-side render plus the SSE delta stream.

Routes:

* ``GET /`` — the board, fully rendered from the hub's cache (a fresh tab
  shows the current state immediately, despite the stream being
  ``retain=False``).
* ``GET /stream`` — Server-Sent Events; one event per panel repaint
  (``panel:<name>``), plus ``board`` when a new panel appears. htmx's SSE
  extension swaps them in — no hand-written JavaScript.
* ``GET /healthz`` — liveness/readiness JSON, including broker connectivity.

Template resolution: a plugin-provided template directory (if any) is searched
first, the package's built-in templates second — so a plugin can override even
``base.html`` without forking the core.
"""

import asyncio
import datetime
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import jinja2
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from mqttwebstuff.hub import ViewHub, anchor_slug
from mqttwebstuff.plugin_api import LoadedPlugin

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).parent
TEMPLATES_DIR = _PACKAGE_DIR / "templates"
STATIC_DIR = _PACKAGE_DIR / "static"

#: Seconds between SSE keepalive comments — well below common proxy/ingress
#: idle timeouts (usually 60 s).
_KEEPALIVE_SECONDS = 15.0


def _filter_hhmm(value: object) -> str:
    """Jinja2 filter: ISO datetime string → ``HH:MM`` (fallback: unchanged).

    Args:
        value: An ISO-8601 datetime string (as published on the MQTT stream).

    Returns:
        The wall-clock time, or the input as string when unparsable.
    """
    try:
        return datetime.datetime.fromisoformat(str(value)).strftime("%H:%M")
    except ValueError:
        return str(value)


def build_environment(plugin: LoadedPlugin) -> jinja2.Environment:
    """Build the Jinja2 environment: plugin templates first, built-ins second.

    Args:
        plugin: The loaded mapper plugin (its ``template_dir`` may be ``None``).

    Returns:
        An autoescaping environment with the extra filters (``hhmm``,
        ``anchor`` for URL-fragment-safe anchor ids).
    """
    loaders: list[jinja2.BaseLoader] = []
    if plugin.template_dir is not None:
        loaders.append(jinja2.FileSystemLoader(plugin.template_dir))
    loaders.append(jinja2.FileSystemLoader(TEMPLATES_DIR))
    env = jinja2.Environment(loader=jinja2.ChoiceLoader(loaders), autoescape=True)
    env.filters["hhmm"] = _filter_hhmm
    env.filters["anchor"] = anchor_slug
    return env


def _sse_frame(event: str, fragment: str) -> str:
    """Format one Server-Sent Event.

    Args:
        event: The SSE event name.
        fragment: HTML payload; newlines are split over ``data:`` lines as the
            SSE wire format requires.

    Returns:
        The complete frame including the terminating blank line.
    """
    data_lines = fragment.splitlines() or [""]
    return f"event: {event}\n" + "".join(f"data: {line}\n" for line in data_lines) + "\n"


def create_app(
    hub: ViewHub,
    env: jinja2.Environment,
    *,
    is_connected: Callable[[], bool] | None = None,
    on_startup: Callable[[], Awaitable[None]] | None = None,
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    """Build the FastAPI application around a hub.

    Args:
        hub: The view hub (cache + SSE fan-out).
        env: Jinja2 environment (see :func:`build_environment`).
        is_connected: Optional probe for broker connectivity, surfaced in
            ``/healthz``.
        on_startup: Optional coroutine run after the hub is attached to the
            event loop — the place to start the MQTT client, so no message can
            arrive before the hub can accept it.
        on_shutdown: Optional coroutine run on shutdown (e.g. MQTT disconnect).

    Returns:
        The configured application.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        hub.attach_loop(asyncio.get_running_loop())
        sweeper = asyncio.create_task(hub.sweeper())
        if on_startup is not None:
            await on_startup()
        try:
            yield
        finally:
            sweeper.cancel()
            if on_shutdown is not None:
                await on_shutdown()

    app = FastAPI(title=hub.plugin.title, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        page = env.get_template("base.html.j2").render(title=hub.plugin.title, board=hub.render_board())
        return HTMLResponse(page)

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            queue = hub.subscribe()
            try:
                # Ask the browser to reconnect quickly after a drop; the page
                # itself still holds the last state, the next repaint heals it.
                yield "retry: 3000\n\n"
                while True:
                    try:
                        event, fragment = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield _sse_frame(event, fragment)
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"ok": True, "mqtt_connected": is_connected() if is_connected is not None else None}

    return app
