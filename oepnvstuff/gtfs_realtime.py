"""GTFS-Realtime fetching and parsing.

GTFS-RT is *not* push/WebSocket — it is an HTTP resource that has to be polled.
:class:`RealtimeFetcher` therefore keeps the server's ``ETag`` /
``Last-Modified`` validators between calls and sends conditional requests, so
an unchanged feed costs a ``304 Not Modified`` round-trip instead of a body
download.

:func:`parse_realtime` decodes the protobuf payload and reduces it to a typed
:class:`RealtimeSnapshot`: the trip updates matching the target trips/routes
(with their per-stop delay values) plus the service alerts touching them.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import requests


class FetchStatus(Enum):
    """Outcome of one :meth:`RealtimeFetcher.fetch` call."""

    OK = "ok"
    """Fresh bytes were downloaded from the URL."""

    NOT_MODIFIED = "not_modified"
    """The server answered ``304`` — the previously fetched bytes are still current."""

    LOCAL = "local"
    """The source is a local file; its bytes were read directly."""


@dataclass(frozen=True)
class FetchResult:
    """Result of one realtime fetch.

    Attributes:
        status: How the fetch resolved.
        data: The feed bytes; ``None`` exactly when :attr:`status` is
            :attr:`FetchStatus.NOT_MODIFIED`.
    """

    status: FetchStatus
    data: bytes | None


class RealtimeFetcher:
    """Fetch a GTFS-RT feed (URL or local file) with conditional HTTP requests.

    For URLs the ``ETag`` / ``Last-Modified`` response headers are remembered
    and sent back as ``If-None-Match`` / ``If-Modified-Since`` on the next call,
    letting the server answer ``304 Not Modified``. Local file paths are read
    directly on every call (useful for testing).
    """

    def __init__(self, source: str) -> None:
        """Initialize the fetcher for one source.

        Args:
            source: ``http(s)`` URL or local file path of the ``.pb`` feed.
        """
        self.source = source
        self.is_url = source.startswith("http://") or source.startswith("https://")
        self._etag: str | None = None
        self._last_modified: str | None = None
        self._session: "requests.Session | None" = None
        if self.is_url:
            import requests

            self._session = requests.Session()

    def fetch(self, timeout: float = 60) -> FetchResult:
        """Fetch the feed once.

        Args:
            timeout: HTTP timeout in seconds (URLs only).

        Returns:
            The fetch outcome; see :class:`FetchResult`.

        Raises:
            OSError: The local source file could not be read.
            requests.HTTPError: The server answered with an error status.
            requests.RequestException: The request failed on the transport level.
        """
        if not self.is_url:
            with open(self.source, "rb") as f:
                return FetchResult(FetchStatus.LOCAL, f.read())

        headers: dict[str, str] = {}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified

        assert self._session is not None  # set in __init__ for URLs
        r = self._session.get(self.source, headers=headers, timeout=timeout)
        if r.status_code == 304:
            return FetchResult(FetchStatus.NOT_MODIFIED, None)
        r.raise_for_status()
        # Remember the validators for the next call.
        self._etag = r.headers.get("ETag", self._etag)
        self._last_modified = r.headers.get("Last-Modified", self._last_modified)
        return FetchResult(FetchStatus.OK, r.content)


@dataclass(frozen=True)
class StopDelay:
    """Delay information of one stop within a trip update.

    Attributes:
        stop_id: The GTFS stop id the update refers to.
        arrival_delay: Arrival delay in seconds, if the feed provides one.
        departure_delay: Departure delay in seconds, if the feed provides one.
    """

    stop_id: str
    arrival_delay: int | None
    departure_delay: int | None

    @property
    def delay(self) -> int | None:
        """The effective delay: departure if present, else arrival, else ``None``."""
        return self.departure_delay if self.departure_delay is not None else self.arrival_delay


@dataclass(frozen=True)
class TripUpdateHit:
    """One realtime trip update that matched a target trip or route.

    Attributes:
        trip_id: The trip id from the feed (may be empty).
        route_id: The route id from the feed (may be empty).
        matched_by: Which key matched — ``"trip_id"`` (exact target trip) or
            ``"route_id"`` (same line, trip not in the static target set).
        delays: Per-stop delay values carried by this update.
    """

    trip_id: str
    route_id: str
    matched_by: str
    delays: tuple[StopDelay, ...] = ()


@dataclass(frozen=True)
class ServiceAlert:
    """One service alert touching a target line or trip.

    Attributes:
        entity: The route id or trip id the alert was matched on.
        text: The alert's header text (first translation), possibly empty.
    """

    entity: str
    text: str


@dataclass(frozen=True)
class RealtimeSnapshot:
    """Parsed contents of one realtime feed download, reduced to the targets.

    Attributes:
        total_trip_updates: Number of trip updates in the whole feed (matched
            or not) — a plausibility indicator for the feed itself.
        hits: Trip updates matching the target trips/routes.
        alerts: Service alerts touching the target lines/trips.
        feed_timestamp: The ``FeedHeader.timestamp`` (epoch seconds), if set;
            used for staleness detection.
    """

    total_trip_updates: int
    hits: tuple[TripUpdateHit, ...] = ()
    alerts: tuple[ServiceAlert, ...] = ()
    feed_timestamp: int | None = None


def parse_realtime(data: bytes, target_trip_ids: set[str], target_route_ids: set[str]) -> RealtimeSnapshot:
    """Decode a GTFS-RT protobuf payload and match it against the targets.

    Args:
        data: Raw bytes of the ``.pb`` feed (``FeedMessage``).
        target_trip_ids: Trip ids considered a direct hit.
        target_route_ids: Route ids considered a line-level hit.

    Returns:
        The reduced, typed snapshot.

    Raises:
        google.protobuf.message.DecodeError: ``data`` is not a valid
            ``FeedMessage``.
    """
    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(data)

    total_tu = 0
    hits: list[TripUpdateHit] = []
    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue
        total_tu += 1
        tu = ent.trip_update
        tid: str = tu.trip.trip_id
        rid: str = tu.trip.route_id
        if tid and tid in target_trip_ids:
            matched_by = "trip_id"
        elif rid and rid in target_route_ids:
            matched_by = "route_id"
        else:
            continue
        delays: list[StopDelay] = []
        for stu in tu.stop_time_update:
            arr = stu.arrival.delay if stu.HasField("arrival") and stu.arrival.HasField("delay") else None
            dep = stu.departure.delay if stu.HasField("departure") and stu.departure.HasField("delay") else None
            delays.append(StopDelay(stop_id=stu.stop_id, arrival_delay=arr, departure_delay=dep))
        hits.append(TripUpdateHit(trip_id=tid, route_id=rid, matched_by=matched_by, delays=tuple(delays)))

    alerts: list[ServiceAlert] = []
    for ent in feed.entity:
        if not ent.HasField("alert"):
            continue
        for ie in ent.alert.informed_entity:
            if (ie.route_id and ie.route_id in target_route_ids) or (
                ie.trip.trip_id and ie.trip.trip_id in target_trip_ids
            ):
                txt = ent.alert.header_text.translation[0].text if ent.alert.header_text.translation else ""
                alerts.append(ServiceAlert(entity=ie.route_id or ie.trip.trip_id, text=txt))
                break

    ts: int | None = feed.header.timestamp if feed.header.HasField("timestamp") else None
    return RealtimeSnapshot(total_trip_updates=total_tu, hits=tuple(hits), alerts=tuple(alerts), feed_timestamp=ts)
