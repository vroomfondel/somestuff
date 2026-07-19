#!/usr/bin/env python3
"""Check whether an open GTFS-Realtime stream carries actual realtime data.

Validates that real-time data (delays / actual times) for configurable transit
lines at a configurable station is present in an open GTFS-RT feed (default:
gtfs.de). "Blankenese" and the lines 1/12/22/189 are merely the *defaults* —
everything is overridable per CLI option or ``OEPNV_*`` environment variable
(CLI wins), so the same container image can watch any station/line set in k3s.

Two modes:

* One-shot check (default): check once, log a report, set the exit code.
  All human-facing output is emitted through loguru (via the stdlib-logging
  intercept installed by :func:`oepnvstuff.configure_logging`).
* ``--watch``: poll loop. GTFS-RT is NOT push/WebSocket but polled over HTTP;
  the loop uses conditional requests (ETag / If-Modified-Since, i.e. a
  ``304 Not Modified`` saves bandwidth) and detects stale feeds via
  ``FeedHeader.timestamp``.

Procedure:

1. Load the static GTFS feed (planned schedule) ONCE and find in it
   (a) the stops whose name contains the station query,
   (b) the routes of the target lines,
   (c) all trips belonging to those lines AND serving one of the found stops.
2. Fetch (or poll) the GTFS-RT feed (``.pb``, TripUpdates + ServiceAlerts).
3. Check whether TripUpdates with delay values exist for those target trips
   (``trip_id``) or target lines (``route_id``).
4. Report per line — on the console and, with ``--mqtt``, onto an MQTT broker
   (see :mod:`oepnvstuff.mqtt_bridge` for the topic layout).

Default sources (free, no registration, CC BY-SA 4.0):

* Static (German regional): https://download.gtfs.de/germany/nv_free/latest.zip
* Realtime:                 https://realtime.gtfs.de/realtime-free.pb

Both source options also accept local file paths (for testing).

Usage::

    python3 -m oepnvstuff.check_realtime
    python3 -m oepnvstuff.check_realtime --watch --interval 20
    python3 -m oepnvstuff.check_realtime --watch --mqtt --mqtt-host broker.example.org
    python3 -m oepnvstuff.check_realtime --lines 195,295,781,X95 --station Ellerbek   # non-default station
    python3 -m oepnvstuff.check_realtime --show-stops
    python3 -m oepnvstuff.check_realtime --show-stops --station Ellerbek --near 53.647,9.892,3

Exit codes:

* ``0`` — realtime found for at least one target line (or ``--watch`` ended
  cleanly).
* ``2`` — target trips exist in the static feed but NO realtime in the RT feed
  (or ``--watch`` stopped because of a stale feed via ``--stop-on-stale``).
* ``3`` — no matching stops/trips found in the static feed.
* ``1`` — technical error (download/parsing).
"""

import datetime
import logging
import signal
from pathlib import Path
from types import FrameType

import typer
from dotenv import load_dotenv

from oepnvstuff import configure_logging, print_banner
from oepnvstuff.gtfs_realtime import RealtimeFetcher, ServiceAlert
from oepnvstuff.gtfs_static import (
    NearFilter,
    StaticFeedIndex,
    StopDetails,
    build_index,
    build_stop_details,
    find_stops,
    obtain,
)
from oepnvstuff.monitor import CycleResult, LineStatus, RealtimeMonitor

logger = logging.getLogger(__name__)

DEFAULT_STATIC = "https://download.gtfs.de/germany/nv_free/latest.zip"
DEFAULT_REALTIME = "https://realtime.gtfs.de/realtime-free.pb"
DEFAULT_LINES = "1,12,22,189"
DEFAULT_STATION = "Blankenese"

#: Optional env files (gitignored via ``*.local.*``), searched in the current
#: working directory AND next to this module. ``load_dotenv`` never overrides
#: variables that are already set, so loading the CWD file first yields the
#: precedence: real environment > ``$CWD/oepnv.local.env`` > module-dir file.
CREDS_FILES = (Path.cwd() / "oepnv.local.env", Path(__file__).parent / "oepnv.local.env")
for _creds_file in CREDS_FILES:
    load_dotenv(_creds_file)


def _sigterm_to_keyboardinterrupt(signum: int, frame: FrameType | None) -> None:
    """Signal handler: translate ``SIGTERM`` into a :class:`KeyboardInterrupt`.

    Kubernetes stops a pod with ``SIGTERM``; raising ``KeyboardInterrupt`` makes
    the watch loop end exactly like CTRL+C (exit ``0``, MQTT bridge disconnected
    in the ``finally`` block) instead of dying mid-cycle.

    Args:
        signum: The delivered signal number (``SIGTERM``).
        frame: The interrupted stack frame (unused).

    Raises:
        KeyboardInterrupt: Always.
    """
    raise KeyboardInterrupt(f"signal {signum}")


def _parse_lines(raw: str) -> list[str]:
    """Split a lines specification into individual line names.

    Args:
        raw: Comma- and/or whitespace-separated line names, e.g.
            ``"195,295 781,X95"``.

    Returns:
        The non-empty line names in their original order and casing.
    """
    return [part for chunk in raw.split(",") for part in chunk.split() if part]


def _parse_near(raw: str) -> NearFilter | None:
    """Parse a ``--near`` specification into a :class:`NearFilter`.

    Args:
        raw: ``"lat,lon,radius_km"`` in decimal degrees / kilometres
            (e.g. ``"53.647,9.892,3"``), or an empty string for "no filter".

    Returns:
        The parsed filter, or ``None`` for an empty input.

    Raises:
        ValueError: The specification is not three comma-separated numbers or
            the radius is not positive.
    """
    if not raw.strip():
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 3:
        raise ValueError(f"--near expects 'lat,lon,radius_km', got {raw!r}")
    lat, lon, radius_km = (float(p) for p in parts)
    if radius_km <= 0:
        raise ValueError(f"--near radius must be positive, got {radius_km!r}")
    return NearFilter(lat=lat, lon=lon, radius_km=radius_km)


def _format_stop_details(details: dict[str, StopDetails]) -> str:
    """Render the enriched ``--show-stops`` listing as a multi-line block.

    Args:
        details: ``stop_id`` → enrichment, as returned by
            :func:`~oepnvstuff.gtfs_static.build_stop_details`.

    Returns:
        One block of text: per stop its name, id, coordinates, serving lines
        (with agency) and the headsigns (directions) seen there.
    """
    out: list[str] = []
    for d in sorted(details.values(), key=lambda d: (d.stop.name, d.stop.stop_id)):
        stop = d.stop
        coords = f"{stop.lat:.5f},{stop.lon:.5f}" if stop.lat is not None and stop.lon is not None else "?,?"
        out.append(f"  {stop.name}  [stop_id={stop.stop_id}]  ({coords})")
        if d.lines:
            lines_txt = ", ".join(f"{sl.line or '?'} ({sl.agency})" if sl.agency else sl.line or "?" for sl in d.lines)
            out.append(f"      lines: {lines_txt}")
        else:
            out.append("      lines: - (no scheduled visits; probably a parent station)")
        if d.headsigns:
            out.append(f"      towards: {' | '.join(d.headsigns)}")
    return "\n".join(out) or "  (none)"


# ── console reporting ────────────────────────────────────────────────────────
#
# All human-facing output goes through the stdlib logger: configure_logging()
# installs the loguru intercept handler, so everything ends up formatted and
# sinked by loguru. Multi-line blocks (report, stop list) are emitted as ONE
# log record each, so loguru prefixes them once instead of per line.


def _log_report(index: StaticFeedIndex, cycle: CycleResult) -> None:
    """Log the full one-shot report as a single multi-line record.

    Args:
        index: The static schedule index (stops, lines, target trips).
        cycle: The evaluated realtime cycle to report on.
    """
    lines_txt = ", ".join(index.target_lines)
    out: list[str] = []
    out.append("=" * 68)
    out.append(f" REALTIME VALIDATION  -  station: '{index.station_query}'  lines: {lines_txt}")
    out.append("=" * 68)

    if index.stop_names:
        out.append(f"Matched stops ({len(index.stop_names)}):")
        for sid, name in sorted(index.stop_names.items(), key=lambda x: x[1]):
            out.append(f"  - {name}  [stop_id={sid}]")
    else:
        out.append(f"[!] No stop matching '{index.station_query}' found in the static feed.")

    snapshot = cycle.snapshot
    if snapshot and snapshot.feed_timestamp:
        out.append(
            f"RT feed timestamp: {datetime.datetime.fromtimestamp(snapshot.feed_timestamp)}  "
            f"(TripUpdates in the whole feed: {snapshot.total_trip_updates})"
        )
    else:
        out.append(f"RT feed TripUpdates in total: {snapshot.total_trip_updates if snapshot else 0}")

    out.append("Result per line:")
    for line in index.target_lines:
        s = cycle.per_line[line]
        if s.has_realtime:
            if s.delays:
                assert s.avg_delay is not None
                dtxt = (
                    f"delay: min {min(s.delays):+d}s, max {max(s.delays):+d}s, "
                    f"avg {s.avg_delay:+.0f}s over {len(s.delays)} stop updates"
                )
            else:
                dtxt = "TripUpdates present, but without concrete delay values"
            out.append(f"  ✅ line {line}: REALTIME present ({s.updates} trip updates). {dtxt}")
        else:
            status = "no realtime in the RT feed"
            if s.static_trips == 0:
                status = "no target trips in the static feed (check line/stop)"
            out.append(f"  ❌ line {line}: {status} (static target trips: {s.static_trips})")

    if snapshot and snapshot.alerts:
        out.append("Service alerts for target lines:")
        for alert in snapshot.alerts[:10]:
            out.append(f"  ! {alert.entity}: {alert.text[:100]}")

    out.append("-" * 68)
    if cycle.any_realtime:
        out.append("VERDICT: real actual-time data exists for at least one of your lines.")
    elif index.target_trip_ids:
        out.append(
            "VERDICT: schedule (planned) data exists, but NO actual-time data for\n"
            "        these lines/this stop in the RT feed -> open-data realtime is\n"
            "        not (yet) sufficient here; consider Geofox-GTI."
        )
    else:
        out.append("VERDICT: neither target trips nor realtime found. Check line numbers/station name/feed.")
    out.append("-" * 68)
    report = "\n".join(out)
    logger.info(f"\n{report}")


def _console_cycle_line(cycle: CycleResult) -> None:
    """CycleHandler: log one compact status line per watch cycle.

    Args:
        cycle: The completed cycle delivered by the monitor.
    """
    snapshot = cycle.snapshot
    if snapshot is None:
        logger.info("(304 unchanged) no data yet")
        return

    parts: list[str] = []
    for line, s in cycle.per_line.items():
        if s.has_realtime:
            avg = f" avg{s.avg_delay:+.0f}s" if s.avg_delay is not None else ""
            parts.append(f"{line}: {s.updates}upd{avg}")
        else:
            parts.append(f"{line}: -")

    if snapshot.feed_timestamp:
        fts = datetime.datetime.fromtimestamp(snapshot.feed_timestamp).strftime("%H:%M:%S")
    else:
        fts = "??:??:??"
    age_txt = f"age {cycle.feed_age}s" if cycle.feed_age is not None else "no feed ts"
    note = f"{cycle.payload_bytes / 1024:.0f} KB" if cycle.payload_bytes is not None else "304 unchanged"
    flag = "  !!STALE" if cycle.stale else ""
    logger.info(f"feed_ts={fts} {age_txt} ({note}) | {' | '.join(parts)}{flag}")


def _console_alert(alert: ServiceAlert) -> None:
    """AlertHandler: log one new service alert.

    Args:
        alert: The newly seen service alert.
    """
    logger.warning(f"alert {alert.entity}: {alert.text[:80]}")


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(
    static_source: str = typer.Option(
        DEFAULT_STATIC, "--static", envvar="OEPNV_STATIC_URL", help="static GTFS feed (URL or .zip path)"
    ),
    realtime_source: str = typer.Option(
        DEFAULT_REALTIME, "--realtime", envvar="OEPNV_REALTIME_URL", help="GTFS-RT feed (URL or .pb path)"
    ),
    lines: str = typer.Option(
        DEFAULT_LINES,
        "--lines",
        "-l",
        envvar="OEPNV_LINES",
        help="target lines (route_short_name), comma/space separated, e.g. '1,12,22,189'",
    ),
    station: str = typer.Option(
        DEFAULT_STATION, "--station", "-s", envvar="OEPNV_STATION", help="substring of the stop name"
    ),
    near: str = typer.Option(
        "",
        "--near",
        envvar="OEPNV_NEAR",
        help="only stops within 'lat,lon,radius_km' (e.g. '53.647,9.892,3') — GTFS has no postal codes, "
        "so this disambiguates same-named stations in different towns",
    ),
    cache_dir: str = typer.Option(
        ".gtfs_cache",
        "--cache-dir",
        envvar="OEPNV_CACHE_DIR",
        help="directory for downloaded feeds + validator metadata",
    ),
    force_refresh: bool = typer.Option(
        False, "--force-refresh", envvar="OEPNV_FORCE_REFRESH", help="re-download the static zip even on 304"
    ),
    show_stops: bool = typer.Option(False, "--show-stops", help="only list the matching stops, then exit"),
    watch: bool = typer.Option(
        False, "--watch", "-w", envvar="OEPNV_WATCH", help="poll loop instead of one-shot check"
    ),
    interval: float = typer.Option(
        20.0, "--interval", envvar="OEPNV_INTERVAL", help="poll interval in seconds (gtfs.de updates every 10s)"
    ),
    max_age: int = typer.Option(
        120, "--max-age", envvar="OEPNV_MAX_AGE", help="feed counts as stale when feed_ts is older than N s (0 = off)"
    ),
    stale_cycles: int = typer.Option(
        6, "--stale-cycles", envvar="OEPNV_STALE_CYCLES", help="feed counts as stale after N unchanged cycles"
    ),
    stop_on_stale: bool = typer.Option(
        False, "--stop-on-stale", envvar="OEPNV_STOP_ON_STALE", help="stop the watch loop on a stale feed (exit 2)"
    ),
    mqtt: bool = typer.Option(False, "--mqtt/--no-mqtt", envvar="OEPNV_MQTT_ENABLE", help="publish results to MQTT"),
    mqtt_host: str = typer.Option(
        "mosquitto.mosquitto.svc.cluster.local", "--mqtt-host", envvar="OEPNV_MQTT_HOST", help="MQTT broker host"
    ),
    mqtt_port: int = typer.Option(1883, "--mqtt-port", envvar="OEPNV_MQTT_PORT", help="MQTT broker port"),
    mqtt_username: str = typer.Option(
        "", "--mqtt-user", envvar="OEPNV_MQTT_USER", help="MQTT username ('' = anonymous)"
    ),
    mqtt_password: str = typer.Option("", "--mqtt-password", envvar="OEPNV_MQTT_PASSWORD", help="MQTT password"),
    mqtt_base_topic: str = typer.Option(
        "oepnv", "--mqtt-base-topic", envvar="OEPNV_MQTT_BASE_TOPIC", help="root of the MQTT topic tree"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", envvar="OEPNV_VERBOSE", help="DEBUG logging"),
) -> None:
    """Validate GTFS-Realtime coverage for the target lines at a station.

    \f
    Everything below the ``\\f`` is hidden from ``--help`` (click convention).

    Raises:
        typer.Exit: Always — with the module docstring's exit codes.
    """
    configure_logging(verbose=verbose)
    print_banner()
    # k8s stops pods with SIGTERM — treat it like CTRL+C for a clean shutdown.
    signal.signal(signal.SIGTERM, _sigterm_to_keyboardinterrupt)

    target_lines = _parse_lines(lines)
    if not target_lines:
        logger.error("no target lines given (--lines / OEPNV_LINES)")
        raise typer.Exit(code=1)

    try:
        near_filter = _parse_near(near)
    except ValueError as exc:
        logger.error(f"{exc}")
        raise typer.Exit(code=1)

    bridge = None
    try:
        static_path = obtain(static_source, cache_dir, force_refresh=force_refresh)

        if show_stops:
            stops = find_stops(static_path, station, near_filter)
            logger.info("enriching stops with lines and directions (streams stop_times.txt) ...")
            details = build_stop_details(static_path, stops)
            logger.info(f"matched stops ({len(details)}):\n{_format_stop_details(details)}")
            raise typer.Exit(code=0)

        logger.info("indexing static feed (stop_times.txt can be large) ...")
        index = build_index(static_path, target_lines, station, near_filter)

        if not index.target_trip_ids:
            empty_cycle = CycleResult(
                wall_time=datetime.datetime.now(),
                fetch_status=None,
                snapshot=None,
                per_line={
                    line: LineStatus(line=line, updates=0, delays=(), static_trips=index.static_trip_count(line))
                    for line in target_lines
                },
            )
            _log_report(index, empty_cycle)
            raise typer.Exit(code=3)

        monitor = RealtimeMonitor(
            RealtimeFetcher(realtime_source),
            index,
            interval=interval,
            max_age=max_age,
            stale_cycles=stale_cycles,
            stop_on_stale=stop_on_stale,
        )

        if mqtt:
            from oepnvstuff.mqtt_bridge import GtfsMqttBridge, attach_bridge

            bridge = GtfsMqttBridge(
                host=mqtt_host,
                port=mqtt_port,
                username=mqtt_username or None,
                password=mqtt_password or None,
                base_topic=mqtt_base_topic,
            )
            attach_bridge(monitor, bridge)
            bridge.start()

        if watch:
            monitor.add_cycle_handler(_console_cycle_line)
            monitor.add_alert_handler(_console_alert)
            raise typer.Exit(code=monitor.watch())

        # One-shot check.
        logger.info("fetching & parsing realtime feed ...")
        cycle = monitor.check_once()
        _log_report(index, cycle)
        raise typer.Exit(code=0 if cycle.any_realtime else 2)

    except typer.Exit:
        raise
    except Exception as exc:
        logger.error(f"{type(exc).__name__}: {exc}")
        raise typer.Exit(code=1)
    finally:
        if bridge is not None:
            bridge.stop()


if __name__ == "__main__":
    typer.run(main)
