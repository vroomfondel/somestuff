#!/usr/bin/env python3
"""Minimal HTTP API in front of the ``*_ac_*.sh`` Broadlink helper scripts.

Wraps the three per-room shell scripts (``activate_``/``deactivate_``/
``gettemp_ac_<room>.sh``) as REST endpoints. Rooms are **discovered** from the
script directory, so adding ``activate_ac_kueche.sh`` & friends is enough — no
code change here. The scripts themselves are whatever drives the blaster: a
``broadlinkhelper`` call, a ``podman run``, anything that exits 0 on success.

Routes:

* ``GET  /healthz``           — liveness + how many rooms were discovered.
* ``GET  /rooms``             — discovered rooms and which actions they support.
* ``GET  /rooms/{room}/temp`` — sensor readout (parsed from the script's JSON).
* ``POST /rooms/{room}/on``   — activate.
* ``POST /rooms/{room}/off``  — deactivate.

Each script is one process, i.e. seconds, not milliseconds, and talks to a
single IR blaster per room. Calls are therefore serialised **per room** by a
lock — parallel requests queue instead of interleaving two IR bursts at the
same device. Different rooms still run concurrently.

Usage::

    broadlink-ac-api                                     # 127.0.0.1:8099, scripts from ~/.local/bin
    python3 -m somestuff.broadlinkstuff.ac_api --listen-host 0.0.0.0 --listen-port 8099
    AC_API_SCRIPT_DIR=/opt/ac-scripts python3 -m somestuff.broadlinkstuff.ac_api

    curl -s localhost:8099/rooms | jq
    curl -s localhost:8099/rooms/lounge/temp | jq
    curl -sX POST localhost:8099/rooms/lounge/on | jq

There is no authentication — bind to localhost (the default) or put it behind
something that has any.
"""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import typer
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from somestuff.broadlinkstuff import __version__, configure_logging, print_banner

logger = logging.getLogger(__name__)

# activate_ac_lounge.sh -> ("activate", "lounge")
_SCRIPT_RE = re.compile(r"^(?P<action>activate|deactivate|gettemp)_ac_(?P<room>[a-z0-9_-]+)\.sh$")

ACTION_ROUTE = {"on": "activate", "off": "deactivate", "temp": "gettemp"}


class Room(BaseModel):
    """One discovered room and the actions its scripts provide."""

    name: str = Field(description="room slug as used in the URL path")
    actions: list[str] = Field(description="subset of on/off/temp")


class CommandResult(BaseModel):
    """Outcome of a switching command."""

    room: str
    action: str
    ok: bool
    duration_seconds: float
    stdout: str = ""


class Reading(BaseModel):
    """Sensor readout of one room."""

    room: str
    temperature: float | None = None
    humidity: float | None = None
    duration_seconds: float = 0.0
    raw: dict[str, object] = Field(default_factory=dict, description="unmodified script JSON")


class Health(BaseModel):
    """Liveness payload."""

    ok: bool
    script_dir: str
    rooms: int


def discover_rooms(script_dir: Path) -> dict[str, dict[str, Path]]:
    """Scan a directory for ``<action>_ac_<room>.sh`` scripts.

    Args:
        script_dir: Directory holding the helper scripts.

    Returns:
        Mapping ``room -> {"activate"|"deactivate"|"gettemp": path}``, only for
        scripts that are actually executable files.
    """
    rooms: dict[str, dict[str, Path]] = {}
    if not script_dir.is_dir():
        logger.warning(f"script dir {script_dir} does not exist — no rooms")
        return rooms
    for path in sorted(script_dir.iterdir()):
        match = _SCRIPT_RE.match(path.name)
        if match is None or not path.is_file() or not os.access(path, os.X_OK):
            continue
        rooms.setdefault(match["room"], {})[match["action"]] = path
    return rooms


async def run_script(path: Path, timeout: float) -> tuple[int, str, str, float]:
    """Run one helper script and capture its output.

    Args:
        path: The script to execute.
        timeout: Seconds after which the process is killed.

    Returns:
        ``(returncode, stdout, stderr, duration_seconds)``. On timeout the
        returncode is ``-1`` and stderr carries a note (the script's own output
        up to that point is lost with the killed process).
    """
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"timeout after {timeout:g}s", time.monotonic() - started
    return (
        proc.returncode or 0,
        out.decode(errors="replace").strip(),
        err.decode(errors="replace").strip(),
        time.monotonic() - started,
    )


def _resolve(request: Request, room: str, route_action: str) -> Path:
    """Look up the script for a room/action, or raise the matching HTTP error.

    Args:
        request: Incoming request (carries the app state).
        room: Room slug from the path.
        route_action: ``on``, ``off`` or ``temp``.

    Returns:
        Path of the script to execute.

    Raises:
        HTTPException: 404 if the room or that action's script is unknown.
    """
    rooms: dict[str, dict[str, Path]] = request.app.state.rooms
    scripts = rooms.get(room.lower())
    if scripts is None:
        raise HTTPException(status_code=404, detail=f"unknown room {room!r}; have: {sorted(rooms)}")
    script = scripts.get(ACTION_ROUTE[route_action])
    if script is None:
        raise HTTPException(status_code=404, detail=f"room {room!r} has no {route_action!r} script")
    return script


async def _execute(request: Request, room: str, script: Path) -> tuple[str, str, float]:
    """Run a script under the room's lock and turn failures into HTTP errors.

    Args:
        request: Incoming request (carries app state: locks, timeout).
        room: Room slug, used to pick the lock.
        script: The script to run.

    Returns:
        ``(stdout, stderr, duration_seconds)``.

    Raises:
        HTTPException: 504 on timeout, 502 on any non-zero exit status.
    """
    lock: asyncio.Lock = request.app.state.locks.setdefault(room, asyncio.Lock())
    async with lock:
        rc, out, err, duration = await run_script(script, request.app.state.timeout)
    logger.info(f"{script.name} -> rc={rc} in {duration:.1f}s")
    if rc == -1:
        raise HTTPException(status_code=504, detail=f"{script.name}: {err}")
    if rc != 0:
        raise HTTPException(status_code=502, detail=f"{script.name} exited {rc}: {err or out or 'no output'}")
    return out, err, duration


def create_app(script_dir: Path, timeout: float) -> FastAPI:
    """Build the FastAPI application.

    Args:
        script_dir: Directory scanned for the helper scripts (once, at startup).
        timeout: Per-script timeout in seconds.

    Returns:
        The configured app.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.rooms = discover_rooms(script_dir)
        app.state.locks = {}
        app.state.timeout = timeout
        logger.info(f"rooms in {script_dir}: {sorted(app.state.rooms) or 'none'}")
        yield

    app = FastAPI(title="AC API", version=__version__, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz(request: Request) -> Health:
        """Report liveness and the number of discovered rooms."""
        return Health(ok=True, script_dir=str(script_dir), rooms=len(request.app.state.rooms))

    @app.get("/rooms")
    async def list_rooms(request: Request) -> list[Room]:
        """List every discovered room with the actions it supports."""
        rooms: dict[str, dict[str, Path]] = request.app.state.rooms
        return [
            Room(name=name, actions=[route for route, act in ACTION_ROUTE.items() if act in scripts])
            for name, scripts in sorted(rooms.items())
        ]

    @app.post("/rooms/{room}/on")
    async def switch_on(request: Request, room: str) -> CommandResult:
        """Activate the AC of one room."""
        out, _, duration = await _execute(request, room.lower(), _resolve(request, room, "on"))
        return CommandResult(room=room.lower(), action="on", ok=True, duration_seconds=duration, stdout=out)

    @app.post("/rooms/{room}/off")
    async def switch_off(request: Request, room: str) -> CommandResult:
        """Deactivate the AC of one room."""
        out, _, duration = await _execute(request, room.lower(), _resolve(request, room, "off"))
        return CommandResult(room=room.lower(), action="off", ok=True, duration_seconds=duration, stdout=out)

    @app.get("/rooms/{room}/temp")
    async def get_temp(request: Request, room: str) -> Reading:
        """Read temperature and humidity of one room's sensor.

        The script prints ``{"Lounge": {"temperature": ..., "humidity": ...}}``;
        the device key is dropped in favour of the URL's room slug.
        """
        out, err, duration = await _execute(request, room.lower(), _resolve(request, room, "temp"))
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:
            # An empty stdout with rc=0 is the usual shape of "sensor did not answer".
            raise HTTPException(
                status_code=502, detail=f"no JSON from script: {exc}; got {out[:200]!r} {err[:200]!r}"
            ) from exc
        first = next(iter(payload.values()), None) if isinstance(payload, dict) else None
        values: dict[str, Any] = first if isinstance(first, dict) else {}
        return Reading(
            room=room.lower(),
            temperature=values.get("temperature"),
            humidity=values.get("humidity"),
            duration_seconds=duration,
            raw=payload if isinstance(payload, dict) else {},
        )

    return app


app = typer.Typer(add_completion=False, rich_markup_mode=None)


@app.command()
def serve(
    listen_host: str = typer.Option("127.0.0.1", "--listen-host", envvar="AC_API_LISTEN_HOST", help="bind address"),
    listen_port: int = typer.Option(8099, "--listen-port", envvar="AC_API_LISTEN_PORT", help="bind port"),
    script_dir: Path = typer.Option(
        Path.home() / ".local" / "bin",
        "--script-dir",
        envvar="AC_API_SCRIPT_DIR",
        help="directory holding activate_/deactivate_/gettemp_ac_<room>.sh",
    ),
    timeout: float = typer.Option(
        90.0,
        "--timeout",
        envvar="AC_API_TIMEOUT",
        help="seconds before a script is killed",
    ),
    log_level: str = typer.Option("info", "--log-level", envvar="AC_API_LOG_LEVEL", help="debug/info/warning/error"),
) -> None:
    """Serve the AC API."""
    level = log_level.upper()
    if level not in logging.getLevelNamesMapping():
        raise typer.BadParameter(f"unknown log level {log_level!r}", param_hint="--log-level")
    os.environ["LOGURU_LEVEL"] = level
    configure_logging()
    print_banner("ac_api")
    script_dir = script_dir.expanduser()
    found = discover_rooms(script_dir)
    if not found:
        logger.warning(f"no *_ac_<room>.sh scripts in {script_dir} — the API will serve an empty room list")
    # log_config=None keeps uvicorn on the root logger, which configure_logging intercepts into loguru.
    uvicorn.run(
        create_app(script_dir, timeout),
        host=listen_host,
        port=listen_port,
        log_config=None,
        access_log=level == "DEBUG",
    )


if __name__ == "__main__":
    app()
