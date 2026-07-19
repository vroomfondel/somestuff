"""Static GTFS feed handling: cached download and schedule indexing.

Two responsibilities, both free of any realtime knowledge:

* :func:`obtain` — resolve a feed source (local path or URL) to a local file,
  with persistent HTTP-validator caching: next to the downloaded file a sidecar
  ``<name>.meta.json`` stores the server's ``ETag`` / ``Last-Modified``. Every
  run performs a conditional GET — a ``304 Not Modified`` reuses the cached file
  without re-downloading the (large) body, so no TTL is needed.
* :func:`build_index` — read the schedule zip once and reduce it to a
  :class:`StaticFeedIndex`: the stops matching a station query, the routes of
  the target lines, and the trips that belong to those lines AND stop at one of
  the matched stops.

Stop matching supports an optional :class:`NearFilter` (centre + radius),
because GTFS knows no postal codes or municipalities — the same station name
can exist in several towns ("Ellerbek" near Pinneberg vs. the Kiel city
district), and coordinates are the only field that separates them.
:func:`find_stops` / :func:`build_stop_details` power the enriched
``--show-stops`` (coordinates, serving lines incl. agency, headsigns).
"""

import csv
import datetime
import io
import json
import logging
import math
import os
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: GTFS files that :func:`build_index` requires inside the static feed zip.
REQUIRED_MEMBERS: tuple[str, ...] = ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt")

#: Mean earth radius in kilometres (haversine distance).
_EARTH_RADIUS_KM = 6371.0


@dataclass(frozen=True)
class NearFilter:
    """Geographic stop filter: keep only stops within a radius of a centre.

    GTFS carries no postal code or municipality, so coordinates are the only
    way to tell same-named stations in different towns apart.

    Attributes:
        lat: Centre latitude in decimal degrees (WGS84).
        lon: Centre longitude in decimal degrees (WGS84).
        radius_km: Radius around the centre in kilometres.
    """

    lat: float
    lon: float
    radius_km: float

    def distance_km(self, lat: float, lon: float) -> float:
        """Compute the haversine great-circle distance to the centre.

        Args:
            lat: Latitude of the other point in decimal degrees.
            lon: Longitude of the other point in decimal degrees.

        Returns:
            Distance in kilometres.
        """
        phi1, phi2 = math.radians(self.lat), math.radians(lat)
        dphi = math.radians(lat - self.lat)
        dlambda = math.radians(lon - self.lon)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))

    def contains(self, lat: float, lon: float) -> bool:
        """Whether a point lies within :attr:`radius_km` of the centre.

        Args:
            lat: Latitude of the point in decimal degrees.
            lon: Longitude of the point in decimal degrees.

        Returns:
            ``True`` if the point is inside the radius.
        """
        return self.distance_km(lat, lon) <= self.radius_km


@dataclass(frozen=True)
class MatchedStop:
    """One stop whose name matched the station query.

    Attributes:
        stop_id: The GTFS stop id.
        name: The stop's display name (``stop_name``).
        lat: Latitude in decimal degrees, or ``None`` if unparsable/absent.
        lon: Longitude in decimal degrees, or ``None`` if unparsable/absent.
    """

    stop_id: str
    name: str
    lat: float | None
    lon: float | None


@dataclass(frozen=True)
class StopLine:
    """One line serving a stop, with its operating agency.

    Attributes:
        line: The line name (``route_short_name``), possibly empty.
        agency: The operating agency's name (``agency_name``), possibly empty.
    """

    line: str
    agency: str


@dataclass(frozen=True)
class StopDetails:
    """Enrichment of one matched stop for the ``--show-stops`` listing.

    Attributes:
        stop: The matched stop (name, id, coordinates).
        lines: The lines serving this stop (sorted, deduplicated). Empty for
            parent stations — their traffic hangs off the child platforms.
        headsigns: The ``stop_headsign`` values seen at this stop (sorted,
            deduplicated) — this feed carries direction info here, not in
            ``trips.txt``.
    """

    stop: MatchedStop
    lines: tuple[StopLine, ...] = ()
    headsigns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScheduledDeparture:
    """One scheduled departure of a target trip at a matched stop.

    Attributes:
        trip_id: The GTFS trip this departure belongs to.
        route_id: The trip's route (resolves to the line name via
            :attr:`StaticFeedIndex.route_shortname`).
        stop_id: The matched stop the departure happens at.
        departure_seconds: Departure time as seconds since service-day
            midnight. GTFS allows values beyond 24h (e.g. ``25:10:00`` for
            01:10 the next calendar day, still on the previous service day).
        headsign: The direction shown at this stop (``stop_headsign``),
            possibly empty.
    """

    trip_id: str
    route_id: str
    stop_id: str
    departure_seconds: int
    headsign: str


@dataclass(frozen=True)
class ServicePeriod:
    """Weekly pattern and validity range of one service (``calendar.txt`` row).

    Attributes:
        weekdays: Monday..Sunday flags (index 0 = Monday).
        start: First service day (inclusive).
        end: Last service day (inclusive).
    """

    weekdays: tuple[bool, bool, bool, bool, bool, bool, bool]
    start: datetime.date
    end: datetime.date


@dataclass(frozen=True)
class ServiceCalendar:
    """Service-day rules: weekly patterns plus per-date exceptions.

    Combines ``calendar.txt`` (weekly base pattern) and ``calendar_dates.txt``
    (per-date add/remove exceptions). Either file may be absent — some feeds
    express every service purely as ``calendar_dates`` exceptions.

    Attributes:
        periods: ``service_id`` → weekly pattern with validity range.
        exceptions: ``service_id`` → (date → ``True`` for added, ``False`` for
            removed service on that date).
    """

    periods: dict[str, ServicePeriod] = field(default_factory=dict)
    exceptions: dict[str, dict[datetime.date, bool]] = field(default_factory=dict)

    def runs_on(self, service_id: str, day: datetime.date) -> bool:
        """Decide whether a service operates on a given day.

        Args:
            service_id: The GTFS service id to check.
            day: The service day in question.

        Returns:
            ``True`` if the service runs that day: a per-date exception wins,
            otherwise the weekly pattern within its validity range decides.
        """
        exception = self.exceptions.get(service_id, {}).get(day)
        if exception is not None:
            return exception
        period = self.periods.get(service_id)
        if period is None:
            return False
        return period.start <= day <= period.end and period.weekdays[day.weekday()]


def _parse_gtfs_time(value: str) -> int | None:
    """Parse a GTFS time (``H:MM:SS``, hours may exceed 23) into seconds.

    Args:
        value: The raw time string from ``stop_times.txt``.

    Returns:
        Seconds since service-day midnight, or ``None`` for an empty or
        malformed value.
    """
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def _parse_gtfs_date(value: str) -> datetime.date | None:
    """Parse a GTFS date (``YYYYMMDD``) into a :class:`datetime.date`.

    Args:
        value: The raw date string from ``calendar.txt`` / ``calendar_dates.txt``.

    Returns:
        The parsed date, or ``None`` for an empty or malformed value.
    """
    v = value.strip()
    if len(v) != 8 or not v.isdigit():
        return None
    try:
        return datetime.date(int(v[:4]), int(v[4:6]), int(v[6:8]))
    except ValueError:
        return None


@dataclass(frozen=True)
class StaticFeedIndex:
    """Schedule (planned service) reduced to the target lines and station.

    Produced once per run by :func:`build_index` and consumed by the realtime
    matching in :mod:`oepnvstuff.monitor`.

    Attributes:
        station_query: The (case-insensitive) substring the stop names were
            matched against.
        target_lines: The line names (``route_short_name``) that were searched
            for, in the caller's original casing and order.
        stop_names: ``stop_id`` → ``stop_name`` for every stop whose name
            contains one of :attr:`station_queries`.
        route_shortname: ``route_id`` → ``route_short_name`` for every route
            belonging to one of the target lines AND actually running through a
            matched stop (same-named routes elsewhere in the feed are dropped,
            so route-level realtime matches can't produce false hits).
        route_ids_of_line: Upper-cased line name → set of its ``route_id`` s
            (one line may map to several routes, e.g. per agency or direction;
            restricted to station-serving routes like :attr:`route_shortname`).
        trip_route: ``trip_id`` → ``route_id``, restricted to trips that belong
            to a target line AND serve one of the matched stops.
        trip_service: ``trip_id`` → ``service_id`` for the same target trips —
            resolved against :attr:`calendar` to decide whether a trip runs on
            a given day (needed for next-departure computation).
        departures: Scheduled departures of the target trips at the matched
            stops (time, direction), the static half of next-departure mode.
        calendar: Service-day rules (``calendar.txt`` + ``calendar_dates.txt``)
            for the target trips' services.
    """

    station_queries: tuple[str, ...]
    target_lines: tuple[str, ...]
    stop_names: dict[str, str] = field(default_factory=dict)
    route_shortname: dict[str, str] = field(default_factory=dict)
    route_ids_of_line: dict[str, set[str]] = field(default_factory=dict)
    trip_route: dict[str, str] = field(default_factory=dict)
    trip_service: dict[str, str] = field(default_factory=dict)
    departures: tuple["ScheduledDeparture", ...] = ()
    calendar: "ServiceCalendar" = field(default_factory=lambda: ServiceCalendar())

    @property
    def target_stop_ids(self) -> set[str]:
        """Stop ids whose name matched one of :attr:`station_queries`."""
        return set(self.stop_names)

    @property
    def target_route_ids(self) -> set[str]:
        """Route ids belonging to any of the target lines."""
        return set(self.route_shortname)

    @property
    def target_trip_ids(self) -> set[str]:
        """Trip ids of target-line trips that serve one of the matched stops."""
        return set(self.trip_route)

    def static_trip_count(self, line: str) -> int:
        """Count the scheduled target trips of one line.

        Args:
            line: Line name (``route_short_name``), matched case-insensitively.

        Returns:
            Number of trips of that line which serve one of the matched stops.
        """
        rids = self.route_ids_of_line.get(line.upper(), set())
        return sum(1 for rid in self.trip_route.values() if rid in rids)


def _meta_path(target: str) -> str:
    """Return the sidecar path holding the HTTP validators for ``target``.

    Args:
        target: Path of the cached download.

    Returns:
        Path of the ``<target>.meta.json`` sidecar file.
    """
    return target + ".meta.json"


def _read_meta(target: str) -> dict[str, str]:
    """Load the persisted HTTP validators for a cached download.

    Args:
        target: Path of the cached download.

    Returns:
        The sidecar's contents (``etag`` / ``last_modified`` / ``url`` /
        ``saved_at``), or an empty dict if the sidecar is missing or unreadable.
    """
    try:
        with open(_meta_path(target), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def _write_meta(target: str, etag: str | None, last_modified: str | None, url: str) -> None:
    """Persist the HTTP validators of a fresh download, atomically.

    Args:
        target: Path of the cached download the sidecar belongs to.
        etag: The response's ``ETag`` header, if any.
        last_modified: The response's ``Last-Modified`` header, if any.
        url: The URL the file was downloaded from.
    """
    data = {
        "etag": etag,
        "last_modified": last_modified,
        "url": url,
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    tmp = _meta_path(target) + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _meta_path(target))


def obtain(source: str, cache_dir: str, force_refresh: bool = False) -> str:
    """Resolve a feed source to a local file path, downloading if necessary.

    A local path is returned as-is. A URL is fetched with a conditional GET
    using the persisted ``ETag`` / ``Last-Modified`` validators; on
    ``304 Not Modified`` the cached file is reused without a body download,
    on ``200`` the cache is replaced atomically and the validators updated.

    Args:
        source: Local file path or ``http(s)`` URL of the feed.
        cache_dir: Directory for downloaded feeds and their validator sidecars;
            created if missing.
        force_refresh: Re-download even if the server would answer ``304``.

    Returns:
        Path to a local copy of the feed.

    Raises:
        FileNotFoundError: ``source`` is neither an existing path nor a URL.
        requests.HTTPError: The server answered with an error status.
        requests.RequestException: The download failed on the transport level.
    """
    if os.path.exists(source):
        return source
    if not (source.startswith("http://") or source.startswith("https://")):
        raise FileNotFoundError(f"source not found and not a URL: {source}")

    import requests

    os.makedirs(cache_dir, exist_ok=True)
    fname = source.rstrip("/").split("/")[-1] or "download.bin"
    target = os.path.join(cache_dir, fname)

    # Only send conditional headers if both the cache file AND validators exist.
    headers: dict[str, str] = {}
    if os.path.exists(target) and not force_refresh:
        meta = _read_meta(target)
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]

    if headers:
        logger.info(f"checking {source} (conditional: {', '.join(headers)})")
    else:
        logger.info(f"downloading {source}{' (force-refresh)' if force_refresh else ''}")

    with requests.get(source, headers=headers, stream=True, timeout=120) as r:
        if r.status_code == 304:
            size = os.path.getsize(target) / 1e6
            logger.info(f"304 Not Modified -> using cache ({size:.1f} MB) {target}")
            return target
        r.raise_for_status()

        total = int(r.headers.get("Content-Length", 0))
        got = 0
        tmp = target + ".part"
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
                    got += len(chunk)
        os.replace(tmp, target)  # atomic: never a half download in the cache
        _write_meta(target, r.headers.get("ETag"), r.headers.get("Last-Modified"), source)
        val = r.headers.get("ETag") or r.headers.get("Last-Modified") or "none"
        total_txt = f" of {total / 1e6:.1f} MB" if total else ""
        logger.info(f"200 downloaded ({got / 1e6:.1f} MB{total_txt}). validator: {val}")
    return target


def _header_indices(header: list[str], member: str, *columns: str) -> tuple[int, ...]:
    """Resolve required column names to their positions in a CSV header row.

    Used by the ``csv.reader``-based hot loops over ``stop_times.txt``: GTFS
    guarantees no column *order*, so positions must be resolved from the header
    once per file — never hardcoded.

    Args:
        header: The file's first row (column names).
        member: The GTFS member file name, for the error message.
        *columns: Required column names to resolve.

    Returns:
        The position of each requested column, in the order requested.

    Raises:
        ValueError: A required column is missing from the header.
    """
    missing = [c for c in columns if c not in header]
    if missing:
        raise ValueError(f"column(s) {', '.join(missing)} missing from {member}")
    return tuple(header.index(c) for c in columns)


def _open_member(zf: zipfile.ZipFile, name: str) -> io.TextIOWrapper:
    """Open one member of the GTFS zip as a text stream suitable for ``csv``.

    Args:
        zf: The opened static feed zip.
        name: Member file name (e.g. ``"stops.txt"``).

    Returns:
        A text stream decoding UTF-8 (tolerating a BOM, which GTFS files from
        some publishers carry) with universal newlines disabled for ``csv``.
    """
    return io.TextIOWrapper(zf.open(name, "r"), encoding="utf-8-sig", newline="")


def _read_matched_stops(
    zf: zipfile.ZipFile, station_queries: list[str], near: list[NearFilter] | None = None
) -> dict[str, MatchedStop]:
    """Stream ``stops.txt`` and match stops by name and (optionally) location.

    Args:
        zf: The opened static feed zip.
        station_queries: Substrings to match (case-insensitively) against
            ``stop_name``; a stop matches if ANY query is contained.
        near: If given (non-empty), keep only stops within ANY of the radii;
            stops without parsable coordinates are dropped then (their location
            can't be verified) and counted in a log line.

    Returns:
        ``stop_id`` → :class:`MatchedStop` for every match.
    """
    queries = [q.strip().lower() for q in station_queries if q.strip()]
    matched: dict[str, MatchedStop] = {}
    dropped_far = 0
    dropped_nocoord = 0
    with _open_member(zf, "stops.txt") as fh:
        for row in csv.DictReader(fh):
            name = row.get("stop_name", "") or ""
            lowered = name.lower()
            if not any(q in lowered for q in queries):
                continue
            lat: float | None
            lon: float | None
            try:
                lat = float(row.get("stop_lat", ""))
                lon = float(row.get("stop_lon", ""))
            except (TypeError, ValueError):
                lat = lon = None
            if near:
                if lat is None or lon is None:
                    dropped_nocoord += 1
                    continue
                if not any(nf.contains(lat, lon) for nf in near):
                    dropped_far += 1
                    continue
            sid = row.get("stop_id", "")
            matched[sid] = MatchedStop(stop_id=sid, name=name, lat=lat, lon=lon)
    suffix = ""
    if near:
        centres = " OR ".join(f"{nf.radius_km:g} km of {nf.lat:.4f},{nf.lon:.4f}" for nf in near)
        suffix = f" within {centres} (dropped: {dropped_far} outside radius, {dropped_nocoord} without coordinates)"
    logger.info(f"stops matching {station_queries}: {len(matched)}{suffix}")
    return matched


def _read_service_calendar(zf: zipfile.ZipFile, service_ids: set[str]) -> ServiceCalendar:
    """Read ``calendar.txt`` / ``calendar_dates.txt`` for the given services.

    Both files are optional individually (a feed may express services purely as
    per-date exceptions); if neither exists, the returned calendar answers
    ``False`` for every day and next-departure mode degrades gracefully.

    Args:
        zf: The opened static feed zip.
        service_ids: Only rows for these services are kept.

    Returns:
        The service-day rules for the requested services.
    """
    names = set(zf.namelist())
    periods: dict[str, ServicePeriod] = {}
    exceptions: dict[str, dict[datetime.date, bool]] = {}

    if "calendar.txt" in names:
        weekday_cols = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        with _open_member(zf, "calendar.txt") as fh:
            for row in csv.DictReader(fh):
                sid = row.get("service_id", "")
                if sid not in service_ids:
                    continue
                start = _parse_gtfs_date(row.get("start_date", "") or "")
                end = _parse_gtfs_date(row.get("end_date", "") or "")
                if start is None or end is None:
                    continue
                weekdays = tuple((row.get(day, "") or "").strip() == "1" for day in weekday_cols)
                assert len(weekdays) == 7
                periods[sid] = ServicePeriod(weekdays=weekdays, start=start, end=end)

    if "calendar_dates.txt" in names:
        with _open_member(zf, "calendar_dates.txt") as fh:
            for row in csv.DictReader(fh):
                sid = row.get("service_id", "")
                if sid not in service_ids:
                    continue
                day = _parse_gtfs_date(row.get("date", "") or "")
                if day is None:
                    continue
                exc = (row.get("exception_type", "") or "").strip()
                if exc in ("1", "2"):
                    exceptions.setdefault(sid, {})[day] = exc == "1"

    if not periods and not exceptions:
        logger.warning("neither calendar.txt nor calendar_dates.txt usable — next-departure mode will find nothing")
    return ServiceCalendar(periods=periods, exceptions=exceptions)


def build_index(
    static_path: str, target_lines: list[str], station_queries: list[str], near: list[NearFilter] | None = None
) -> StaticFeedIndex:
    """Read the static GTFS zip and index it for the target lines and stations.

    Streams the four required GTFS files exactly once each (``stop_times.txt``
    can be huge) and keeps only what the realtime matching and next-departure
    computation need.

    Args:
        static_path: Path to the static GTFS feed zip.
        target_lines: Line names to look for (matched case-insensitively
            against ``route_short_name``).
        station_queries: Substrings to match (case-insensitively) against
            ``stop_name``; a stop matches if ANY query is contained.
        near: Optional geographic filters narrowing the matched stops (a stop
            is kept if it lies within ANY of them — same-named stations exist
            in several towns; GTFS has no postal codes).

    Returns:
        The reduced schedule index.

    Raises:
        ValueError: A required GTFS file is missing from the zip.
        zipfile.BadZipFile: ``static_path`` is not a valid zip archive.
    """
    lines_norm = {line.strip().upper() for line in target_lines}

    with zipfile.ZipFile(static_path) as zf:
        names = set(zf.namelist())
        for req in REQUIRED_MEMBERS:
            if req not in names:
                raise ValueError(f"'{req}' missing from static feed {static_path}")

        stop_names: dict[str, str] = {
            sid: stop.name for sid, stop in _read_matched_stops(zf, station_queries, near).items()
        }

        route_shortname: dict[str, str] = {}
        route_ids_of_line: defaultdict[str, set[str]] = defaultdict(set)
        with _open_member(zf, "routes.txt") as fh:
            for row in csv.DictReader(fh):
                rid = row.get("route_id", "")
                sn = (row.get("route_short_name", "") or "").strip()
                if sn.upper() in lines_norm:
                    route_shortname[rid] = sn
                    route_ids_of_line[sn.upper()].add(rid)
        logger.info(f"routes (route_ids) for target lines: {len(route_shortname)}")

        trip_route_all: dict[str, str] = {}
        trip_service_all: dict[str, str] = {}
        with _open_member(zf, "trips.txt") as fh:
            for row in csv.DictReader(fh):
                rid = row.get("route_id", "")
                if rid in route_shortname:
                    tid = row.get("trip_id", "")
                    trip_route_all[tid] = rid
                    trip_service_all[tid] = row.get("service_id", "")
        logger.info(f"trips of target lines in total: {len(trip_route_all)}")

        target_trip_ids: set[str] = set()
        departures_raw: list[tuple[str, str, int, str]] = []  # (trip_id, stop_id, dep_seconds, headsign)
        if stop_names and trip_route_all:
            # Hot loop over tens of millions of rows: csv.reader with header-resolved
            # positions instead of DictReader (no per-row dict, ~2-3x faster).
            with _open_member(zf, "stop_times.txt") as fh:
                reader = csv.reader(fh)
                header = next(reader, [])
                i_trip, i_stop = _header_indices(header, "stop_times.txt", "trip_id", "stop_id")
                i_dep = header.index("departure_time") if "departure_time" in header else -1
                i_arr = header.index("arrival_time") if "arrival_time" in header else -1
                i_headsign = header.index("stop_headsign") if "stop_headsign" in header else -1
                min_len = max(i_trip, i_stop) + 1
                for cols in reader:
                    if len(cols) >= min_len and cols[i_trip] in trip_route_all and cols[i_stop] in stop_names:
                        target_trip_ids.add(cols[i_trip])
                        # Time/headsign parsing only for the rare matched rows.
                        raw_time = cols[i_dep] if 0 <= i_dep < len(cols) and cols[i_dep].strip() else ""
                        if not raw_time and 0 <= i_arr < len(cols):
                            raw_time = cols[i_arr]
                        dep_seconds = _parse_gtfs_time(raw_time)
                        if dep_seconds is not None:
                            headsign = cols[i_headsign].strip() if 0 <= i_headsign < len(cols) else ""
                            departures_raw.append((cols[i_trip], cols[i_stop], dep_seconds, headsign))
        logger.info(f"target trips serving {station_queries}: {len(target_trip_ids)}")

        trip_service = {tid: trip_service_all[tid] for tid in target_trip_ids}
        calendar = _read_service_calendar(zf, set(trip_service.values()))

    # Restrict the route set to routes that actually run through the station.
    # Common line names ("1", "12", …) exist at many agencies nationwide; without
    # this narrowing, a route_id-level realtime match against any same-named line
    # elsewhere in the feed would count as a false hit for the target station.
    trip_route = {tid: trip_route_all[tid] for tid in target_trip_ids}
    used_route_ids = set(trip_route.values())
    dropped = len(route_shortname) - len(used_route_ids)
    if dropped:
        logger.info(f"narrowing route set to routes serving {station_queries}: dropped {dropped} same-named route(s)")

    departures = tuple(
        ScheduledDeparture(trip_id=tid, route_id=trip_route[tid], stop_id=sid, departure_seconds=dep, headsign=headsign)
        for tid, sid, dep, headsign in departures_raw
        if tid in trip_route
    )
    logger.info(f"scheduled departures at matched stops: {len(departures)}")

    return StaticFeedIndex(
        station_queries=tuple(station_queries),
        target_lines=tuple(target_lines),
        stop_names=stop_names,
        route_shortname={rid: sn for rid, sn in route_shortname.items() if rid in used_route_ids},
        route_ids_of_line={line: rids & used_route_ids for line, rids in route_ids_of_line.items()},
        trip_route=trip_route,
        trip_service=trip_service,
        departures=departures,
        calendar=calendar,
    )


def find_stops(
    static_path: str, station_queries: list[str], near: list[NearFilter] | None = None
) -> dict[str, MatchedStop]:
    """Match stops by name (and optionally location) — reads only ``stops.txt``.

    The fast path for stop discovery (``--show-stops``): no routes/trips/
    stop_times scan, so it answers in about a second even on the Germany-wide
    feed.

    Args:
        static_path: Path to the static GTFS feed zip.
        station_queries: Substrings to match (case-insensitively) against
            ``stop_name``; a stop matches if ANY query is contained.
        near: Optional geographic filters (centre + radius each; OR-combined).

    Returns:
        ``stop_id`` → :class:`MatchedStop` for every match.

    Raises:
        ValueError: ``stops.txt`` is missing from the zip.
        zipfile.BadZipFile: ``static_path`` is not a valid zip archive.
    """
    with zipfile.ZipFile(static_path) as zf:
        if "stops.txt" not in zf.namelist():
            raise ValueError(f"'stops.txt' missing from static feed {static_path}")
        return _read_matched_stops(zf, station_queries, near)


def build_stop_details(static_path: str, stops: dict[str, MatchedStop]) -> dict[str, StopDetails]:
    """Enrich matched stops with their serving lines (incl. agency) and headsigns.

    Streams ``stop_times.txt`` once to collect, per matched stop, the visiting
    trips and their ``stop_headsign`` values (this feed carries direction info
    only there — ``trips.txt`` has neither ``direction_id`` nor
    ``trip_headsign``), then resolves trips → routes → line names and agencies.
    Only data for the given stops is kept in memory.

    Args:
        static_path: Path to the static GTFS feed zip.
        stops: The matched stops to enrich (as returned by :func:`find_stops`).

    Returns:
        ``stop_id`` → :class:`StopDetails`, one entry per input stop (stops
        without any scheduled visit — e.g. parent stations — get empty lines
        and headsigns).

    Raises:
        ValueError: A required GTFS file is missing from the zip.
        zipfile.BadZipFile: ``static_path`` is not a valid zip archive.
    """
    with zipfile.ZipFile(static_path) as zf:
        names = set(zf.namelist())
        for req in ("routes.txt", "trips.txt", "stop_times.txt"):
            if req not in names:
                raise ValueError(f"'{req}' missing from static feed {static_path}")

        # Pass 1: which trips visit the stops, and which headsigns are shown there.
        # Hot loop over tens of millions of rows: csv.reader with header-resolved
        # positions instead of DictReader (no per-row dict, ~2-3x faster).
        trips_at: defaultdict[str, set[str]] = defaultdict(set)  # stop_id -> trip_ids
        headsigns_at: defaultdict[str, set[str]] = defaultdict(set)  # stop_id -> headsigns
        wanted_trips: set[str] = set()
        with _open_member(zf, "stop_times.txt") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
            i_stop, i_trip = _header_indices(header, "stop_times.txt", "stop_id", "trip_id")
            # stop_headsign is optional in GTFS — tolerate feeds without it.
            i_headsign = header.index("stop_headsign") if "stop_headsign" in header else -1
            min_len = max(i_stop, i_trip) + 1
            for cols in reader:
                if len(cols) < min_len:
                    continue
                sid = cols[i_stop]
                if sid not in stops:
                    continue
                tid = cols[i_trip]
                trips_at[sid].add(tid)
                wanted_trips.add(tid)
                if 0 <= i_headsign < len(cols):
                    headsign = cols[i_headsign].strip()
                    if headsign:
                        headsigns_at[sid].add(headsign)

        # Pass 2: resolve only the visiting trips to their routes.
        trip_route: dict[str, str] = {}
        with _open_member(zf, "trips.txt") as fh:
            for row in csv.DictReader(fh):
                tid = row.get("trip_id", "")
                if tid in wanted_trips:
                    trip_route[tid] = row.get("route_id", "")

        # Pass 3: route -> (line name, agency id); agency id -> agency name.
        used_route_ids = set(trip_route.values())
        route_info: dict[str, tuple[str, str]] = {}
        with _open_member(zf, "routes.txt") as fh:
            for row in csv.DictReader(fh):
                rid = row.get("route_id", "")
                if rid in used_route_ids:
                    route_info[rid] = ((row.get("route_short_name", "") or "").strip(), row.get("agency_id", "") or "")

        agency_names: dict[str, str] = {}
        if "agency.txt" in names:
            with _open_member(zf, "agency.txt") as fh:
                for row in csv.DictReader(fh):
                    agency_names[row.get("agency_id", "") or ""] = (row.get("agency_name", "") or "").strip()

    details: dict[str, StopDetails] = {}
    for sid, stop in stops.items():
        lines = {
            StopLine(line=route_info[rid][0], agency=agency_names.get(route_info[rid][1], ""))
            for rid in {trip_route.get(tid, "") for tid in trips_at.get(sid, set())}
            if rid in route_info
        }
        details[sid] = StopDetails(
            stop=stop,
            lines=tuple(sorted(lines, key=lambda sl: (sl.line, sl.agency))),
            headsigns=tuple(sorted(headsigns_at.get(sid, set()))),
        )
    return details
