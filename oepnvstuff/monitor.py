"""Realtime coverage monitoring: per-line summaries, one-shot check, watch loop.

:class:`RealtimeMonitor` ties a :class:`~oepnvstuff.gtfs_realtime.RealtimeFetcher`
to a :class:`~oepnvstuff.gtfs_static.StaticFeedIndex` and exposes two modes:

* :meth:`RealtimeMonitor.check_once` — fetch + parse once, return a
  :class:`CycleResult`.
* :meth:`RealtimeMonitor.watch` — poll in a loop with staleness detection
  (feed age via ``FeedHeader.timestamp`` and unchanged-cycle counting).

Both modes report through pluggable ``on_*`` handlers instead of printing:
register callables via :meth:`RealtimeMonitor.add_cycle_handler` (every
completed cycle), :meth:`RealtimeMonitor.add_alert_handler` (each *new* service
alert, deduplicated), :meth:`RealtimeMonitor.add_stale_handler` (edge-triggered
on the transition into staleness) and
:meth:`RealtimeMonitor.add_error_handler` (fetch errors in the watch loop).
Console reporting (:mod:`oepnvstuff.check_realtime`) and MQTT publishing
(:mod:`oepnvstuff.mqtt_bridge`) are just such handlers — the monitor itself
stays I/O-free apart from the feed itself.
"""

import datetime
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from oepnvstuff.gtfs_realtime import (
    FetchStatus,
    RealtimeFetcher,
    RealtimeSnapshot,
    ServiceAlert,
    TripUpdateHit,
    parse_realtime,
)
from oepnvstuff.gtfs_static import StaticFeedIndex

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NextDeparture:
    """The next departure of one line towards one direction.

    Attributes:
        line: Line name (``route_short_name``).
        direction: The direction shown at the stop (``stop_headsign``),
            possibly empty when the feed carries none.
        stop_id: The stop the departure happens at.
        stop_name: The stop's display name.
        scheduled: Scheduled (planned) departure time.
        delay_seconds: Realtime delay in seconds, or ``None`` when no realtime
            information exists for this trip (``expected`` then equals
            ``scheduled``).
        expected: Expected departure time (scheduled + delay).
    """

    line: str
    direction: str
    stop_id: str
    stop_name: str
    scheduled: datetime.datetime
    delay_seconds: int | None
    expected: datetime.datetime

    def minutes_until(self, now: datetime.datetime) -> float:
        """Minutes from ``now`` until the expected departure (may be negative).

        Args:
            now: The reference time.

        Returns:
            Fractional minutes until :attr:`expected`.
        """
        return (self.expected - now).total_seconds() / 60


def _delay_at_stop(hit: TripUpdateHit | None, stop_id: str) -> int | None:
    """Extract the realtime delay for one stop from a trip update.

    Args:
        hit: The trip's realtime update, or ``None`` if the trip has none.
        stop_id: The stop to look up.

    Returns:
        The delay in seconds: the stop's own value if present, otherwise the
        update's last known delay (GTFS-RT semantics propagate a delay to
        subsequent stops), otherwise ``None``.
    """
    if hit is None:
        return None
    last: int | None = None
    for sd in hit.delays:
        if sd.delay is not None:
            if sd.stop_id == stop_id:
                return sd.delay
            last = sd.delay
    return last


def compute_next_departures(
    index: StaticFeedIndex,
    snapshot: RealtimeSnapshot,
    now: datetime.datetime | None = None,
    *,
    horizon_hours: float = 48.0,
    per_direction: int = 3,
    grace_seconds: int = 60,
) -> tuple[NextDeparture, ...]:
    """Compute the next N departures per (line, stop, direction) at the matched stops.

    Walks the schedule's departures for every service day that can produce a
    departure inside the look-ahead window — from the previous day (GTFS times
    may exceed 24h, so yesterday's service can still depart today) up to
    ``horizon_hours`` ahead — keeps only services that actually run on that day
    (calendar + exceptions), applies realtime delays from the snapshot, and
    reduces to the ``per_direction`` earliest upcoming departures per
    (line, stop name, direction) — so a per-stop departure board always carries
    up to N entries, even with several stations configured. Same-named
    platforms (Steig A/B) count as one stop.

    Args:
        index: The static schedule index (departures, calendar, line names).
        snapshot: The current realtime snapshot (delays per trip/stop).
        now: Reference time; defaults to the current local time.
        horizon_hours: Look-ahead window; departures expected later than this
            are ignored. 48h is plenty — a fresh static feed appears within
            that window anyway, and each restart looks 48h ahead again.
        per_direction: How many upcoming departures to keep per
            (line, stop name, direction) group.
        grace_seconds: Departures expected up to this many seconds in the past
            are still reported (the bus may be right at the stop).

    Returns:
        The next departures, sorted by expected time.
    """
    ref = now if now is not None else datetime.datetime.now()
    horizon_end = ref + datetime.timedelta(hours=horizon_hours)
    hits_by_trip: dict[str, TripUpdateHit] = {h.trip_id: h for h in snapshot.hits if h.trip_id}
    grouped: dict[tuple[str, str, str], list[NextDeparture]] = {}
    max_day_offset = int(horizon_hours // 24) + 1
    for dep in index.departures:
        service_id = index.trip_service.get(dep.trip_id, "")
        for day_offset in range(-1, max_day_offset + 1):
            day = ref.date() + datetime.timedelta(days=day_offset)
            if not index.calendar.runs_on(service_id, day):
                continue
            scheduled = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(
                seconds=dep.departure_seconds
            )
            delay = _delay_at_stop(hits_by_trip.get(dep.trip_id), dep.stop_id)
            expected = scheduled + datetime.timedelta(seconds=delay or 0)
            if expected < ref - datetime.timedelta(seconds=grace_seconds) or expected > horizon_end:
                continue
            line = index.route_shortname.get(dep.route_id, dep.route_id or "?")
            candidate = NextDeparture(
                line=line,
                direction=dep.headsign,
                stop_id=dep.stop_id,
                stop_name=index.stop_names.get(dep.stop_id, dep.stop_id),
                scheduled=scheduled,
                delay_seconds=delay,
                expected=expected,
            )
            grouped.setdefault((line.upper(), candidate.stop_name, dep.headsign), []).append(candidate)

    kept: list[NextDeparture] = []
    for group in grouped.values():
        group.sort(key=lambda d: d.expected)
        kept.extend(group[: max(per_direction, 1)])
    return tuple(sorted(kept, key=lambda d: d.expected))


@dataclass(frozen=True)
class LineStatus:
    """Realtime status of one line for one cycle.

    Attributes:
        line: Line name (``route_short_name``) as given by the caller.
        updates: Number of matched trip updates for this line in the snapshot.
        delays: All concrete delay values (seconds) across those updates'
            stops; departure preferred over arrival per stop.
        static_trips: Number of scheduled target trips of this line (from the
            static feed) — distinguishes "no realtime" from "line/stop not in
            the schedule at all".
    """

    line: str
    updates: int
    delays: tuple[int, ...]
    static_trips: int

    @property
    def has_realtime(self) -> bool:
        """Whether at least one trip update matched this line."""
        return self.updates > 0

    @property
    def avg_delay(self) -> float | None:
        """Mean of :attr:`delays` in seconds, or ``None`` without values."""
        return sum(self.delays) / len(self.delays) if self.delays else None


@dataclass(frozen=True)
class CycleResult:
    """Everything one poll cycle (or a one-shot check) produced.

    Attributes:
        wall_time: Local wall-clock time the cycle completed.
        fetch_status: How the realtime fetch resolved, or ``None`` if the fetch
            failed (watch loop keeps running on fetch errors).
        snapshot: The parsed snapshot backing this cycle. On a
            ``304 Not Modified`` this is the *previous* snapshot (bytes were
            identical); ``None`` only if no data has been seen yet at all.
        per_line: Line name → :class:`LineStatus`, one entry per target line.
        feed_age: Seconds between now and ``FeedHeader.timestamp``, if the
            feed carries one.
        unchanged_cycles: Consecutive cycles without a feed-timestamp change.
        stale: Whether the feed is considered stale this cycle (see
            :class:`RealtimeMonitor` thresholds).
        payload_bytes: Size of the freshly downloaded payload, or ``None`` on
            ``304`` / fetch error.
        next_departures: Upcoming departures per (line, stop, direction),
            populated only when the monitor runs with
            ``compute_departures=True``.
    """

    wall_time: datetime.datetime
    fetch_status: FetchStatus | None
    snapshot: RealtimeSnapshot | None
    per_line: dict[str, LineStatus] = field(default_factory=dict)
    feed_age: int | None = None
    unchanged_cycles: int = 0
    stale: bool = False
    payload_bytes: int | None = None
    next_departures: tuple[NextDeparture, ...] = ()

    @property
    def any_realtime(self) -> bool:
        """Whether at least one target line has realtime data this cycle."""
        return any(s.has_realtime for s in self.per_line.values())


#: Handler called after every completed cycle (fresh data or ``304``).
CycleHandler = Callable[[CycleResult], None]
#: Handler called once per *new* (deduplicated) service alert.
AlertHandler = Callable[[ServiceAlert], None]
#: Handler called on the transition from fresh to stale.
StaleHandler = Callable[[CycleResult], None]
#: Handler called when a fetch attempt in the watch loop fails.
ErrorHandler = Callable[[Exception], None]


def summarize_per_line(snapshot: RealtimeSnapshot, index: StaticFeedIndex) -> dict[str, LineStatus]:
    """Aggregate a snapshot's hits into one :class:`LineStatus` per target line.

    A hit's line is resolved via its ``route_id`` when the index knows it, and
    via the static ``trip_id`` → route mapping otherwise — gtfs.de's realtime
    feed ships TripUpdates with an *empty* ``route_id``, so trip-matched hits
    would otherwise never be attributed to a target line.

    Args:
        snapshot: The parsed realtime snapshot.
        index: The static schedule index (provides line names and trip counts).

    Returns:
        Line name → status, one entry per line in ``index.target_lines``.
    """
    updates_per_line: dict[str, int] = {}
    delays_per_line: dict[str, list[int]] = {}
    for hit in snapshot.hits:
        rid = hit.route_id if hit.route_id in index.route_shortname else index.trip_route.get(hit.trip_id, "")
        line = index.route_shortname.get(rid, rid or hit.route_id or "?").upper()
        updates_per_line[line] = updates_per_line.get(line, 0) + 1
        bucket = delays_per_line.setdefault(line, [])
        for sd in hit.delays:
            if sd.delay is not None:
                bucket.append(sd.delay)

    out: dict[str, LineStatus] = {}
    for line in index.target_lines:
        key = line.upper()
        out[line] = LineStatus(
            line=line,
            updates=updates_per_line.get(key, 0),
            delays=tuple(delays_per_line.get(key, [])),
            static_trips=index.static_trip_count(line),
        )
    return out


class RealtimeMonitor:
    """Poll a GTFS-RT feed and dispatch per-cycle results to handlers.

    Staleness is judged two ways, either of which marks a cycle stale:
    the feed's own ``FeedHeader.timestamp`` being older than ``max_age``
    seconds, or ``stale_cycles`` consecutive cycles without a timestamp change.

    Handlers are dispatched in registration order and must not raise; any
    exception they do raise is logged and swallowed so one misbehaving sink
    (e.g. a broker hiccup) cannot kill the poll loop.
    """

    def __init__(
        self,
        fetcher: RealtimeFetcher,
        index: StaticFeedIndex,
        *,
        interval: float = 20.0,
        max_age: int = 120,
        stale_cycles: int = 6,
        stop_on_stale: bool = False,
        compute_departures: bool = False,
        departures_horizon_hours: float = 48.0,
        departures_per_direction: int = 3,
    ) -> None:
        """Initialize the monitor.

        Args:
            fetcher: The realtime feed fetcher (URL with conditional requests,
                or local file).
            index: The static schedule index to match against.
            interval: Target poll-cycle duration in seconds (fetch time is
                subtracted from the sleep).
            max_age: Feed counts as stale when ``FeedHeader.timestamp`` is
                older than this many seconds; ``0`` disables the age check.
            stale_cycles: Feed counts as stale after this many consecutive
                cycles without a timestamp change.
            stop_on_stale: Make :meth:`watch` return (exit code ``2``) once the
                feed is stale instead of merely flagging it.
            compute_departures: Populate :attr:`CycleResult.next_departures`
                every cycle via :func:`compute_next_departures`. Recomputed
                even on ``304`` cycles — the "in N minutes" view changes with
                wall-clock time although the snapshot doesn't.
            departures_horizon_hours: Look-ahead window for next departures
                (see :func:`compute_next_departures`).
            departures_per_direction: How many upcoming departures to report
                per (line, stop name, direction) group.
        """
        self._fetcher = fetcher
        self._index = index
        self.interval = interval
        self.max_age = max_age
        self.stale_cycles = stale_cycles
        self.stop_on_stale = stop_on_stale
        self.compute_departures = compute_departures
        self.departures_horizon_hours = departures_horizon_hours
        self.departures_per_direction = departures_per_direction

        self._cycle_handlers: list[CycleHandler] = []
        self._alert_handlers: list[AlertHandler] = []
        self._stale_handlers: list[StaleHandler] = []
        self._error_handlers: list[ErrorHandler] = []
        self._seen_alerts: set[ServiceAlert] = set()

    # ── handler registration ────────────────────────────────────────────────

    def add_cycle_handler(self, handler: CycleHandler) -> None:
        """Register a handler fired after every completed cycle.

        Args:
            handler: Callable receiving the cycle's :class:`CycleResult`.
        """
        self._cycle_handlers.append(handler)

    def add_alert_handler(self, handler: AlertHandler) -> None:
        """Register a handler fired once per newly seen service alert.

        Alerts are deduplicated over the monitor's lifetime (by entity + text),
        so a persistent alert does not re-fire every cycle.

        Args:
            handler: Callable receiving the new :class:`ServiceAlert`.
        """
        self._alert_handlers.append(handler)

    def add_stale_handler(self, handler: StaleHandler) -> None:
        """Register a handler fired on the transition into staleness.

        Edge-triggered: fires when a cycle is stale and the previous one was
        not (every cycle's :attr:`CycleResult.stale` still carries the level).

        Args:
            handler: Callable receiving the stale cycle's :class:`CycleResult`.
        """
        self._stale_handlers.append(handler)

    def add_error_handler(self, handler: ErrorHandler) -> None:
        """Register a handler fired when a fetch in the watch loop fails.

        Args:
            handler: Callable receiving the exception; the loop continues.
        """
        self._error_handlers.append(handler)

    def _dispatch(self, handlers: list[Callable[..., None]], *args: object) -> None:
        """Call each handler with ``args``, logging (never raising) exceptions.

        Args:
            handlers: The registered handlers to invoke, in order.
            *args: Arguments forwarded to each handler (the payload objects
                defined by the respective handler type alias).
        """
        for handler in handlers:
            try:
                handler(*args)
            except Exception:
                logger.exception(f"handler {handler!r} failed")

    def _dispatch_alerts(self, snapshot: RealtimeSnapshot) -> None:
        """Fire alert handlers for every alert not seen before.

        Args:
            snapshot: The freshly parsed snapshot whose alerts to examine.
        """
        for alert in snapshot.alerts:
            if alert not in self._seen_alerts:
                self._seen_alerts.add(alert)
                self._dispatch(list(self._alert_handlers), alert)

    def _next_departures(self, snapshot: RealtimeSnapshot | None) -> tuple[NextDeparture, ...]:
        """Compute the cycle's next departures, if enabled and data exists.

        Args:
            snapshot: The snapshot backing the cycle (``None`` before the first
                successful fetch).

        Returns:
            The next departures, or ``()`` when disabled or without a snapshot.
        """
        if not self.compute_departures or snapshot is None:
            return ()
        return compute_next_departures(
            self._index,
            snapshot,
            horizon_hours=self.departures_horizon_hours,
            per_direction=self.departures_per_direction,
        )

    # ── one-shot ────────────────────────────────────────────────────────────

    def check_once(self, timeout: float = 60) -> CycleResult:
        """Fetch and evaluate the realtime feed exactly once.

        Dispatches cycle and alert handlers, then returns the result.

        Args:
            timeout: HTTP timeout in seconds for the fetch.

        Returns:
            The evaluated cycle.

        Raises:
            Exception: Whatever the fetch or protobuf parsing raises — the
                one-shot mode does *not* swallow transport errors (unlike the
                watch loop, which reports them via error handlers).
        """
        result = self._fetcher.fetch(timeout=timeout)
        assert result.data is not None  # no prior validators -> never 304 on first fetch
        snapshot = parse_realtime(result.data, self._index.target_trip_ids, self._index.target_route_ids)
        feed_age = int(time.time()) - snapshot.feed_timestamp if snapshot.feed_timestamp else None
        cycle = CycleResult(
            wall_time=datetime.datetime.now(),
            fetch_status=result.status,
            snapshot=snapshot,
            next_departures=self._next_departures(snapshot),
            per_line=summarize_per_line(snapshot, self._index),
            feed_age=feed_age,
            payload_bytes=len(result.data),
        )
        self._dispatch_alerts(snapshot)
        self._dispatch(list(self._cycle_handlers), cycle)
        return cycle

    # ── watch loop ──────────────────────────────────────────────────────────

    def watch(self) -> int:
        """Poll the feed until interrupted (or stale with ``stop_on_stale``).

        Returns:
            ``0`` on a normal end (``KeyboardInterrupt``), ``2`` when the loop
            stopped because the feed went stale and ``stop_on_stale`` is set.
        """
        logger.info(f"watch: poll interval {self.interval:.0f}s, stop with CTRL+C")
        if self.max_age:
            logger.info(
                f"watch: stale when feed older than {self.max_age}s or {self.stale_cycles} unchanged cycles"
                f"{' (then stop)' if self.stop_on_stale else ''}"
            )

        last_ts: int | None = None
        unchanged = 0
        last_snapshot: RealtimeSnapshot | None = None
        was_stale = False
        rc = 0
        try:
            while True:
                cycle_start = time.time()
                try:
                    result = self._fetcher.fetch()
                except Exception as exc:  # network errors must not kill the loop
                    logger.warning(f"fetch failed: {type(exc).__name__}: {exc}")
                    self._dispatch(list(self._error_handlers), exc)
                    self._sleep_rest(cycle_start)
                    continue

                payload_bytes: int | None = None
                if result.status is FetchStatus.NOT_MODIFIED:
                    # Bytes identical -> feed timestamp unchanged as well.
                    unchanged += 1
                    snapshot = last_snapshot
                else:
                    assert result.data is not None
                    snapshot = parse_realtime(result.data, self._index.target_trip_ids, self._index.target_route_ids)
                    last_snapshot = snapshot
                    payload_bytes = len(result.data)
                    unchanged = unchanged + 1 if snapshot.feed_timestamp == last_ts else 0
                    last_ts = snapshot.feed_timestamp
                    self._dispatch_alerts(snapshot)

                stale = False
                feed_age: int | None = None
                if snapshot and snapshot.feed_timestamp:
                    feed_age = int(time.time()) - snapshot.feed_timestamp
                    stale = bool(self.max_age and feed_age > self.max_age) or unchanged >= self.stale_cycles

                cycle = CycleResult(
                    wall_time=datetime.datetime.now(),
                    fetch_status=result.status,
                    snapshot=snapshot,
                    per_line=summarize_per_line(snapshot, self._index) if snapshot else {},
                    feed_age=feed_age,
                    unchanged_cycles=unchanged,
                    stale=stale,
                    payload_bytes=payload_bytes,
                    next_departures=self._next_departures(snapshot),
                )
                self._dispatch(list(self._cycle_handlers), cycle)
                if stale and not was_stale:
                    self._dispatch(list(self._stale_handlers), cycle)
                was_stale = stale

                if stale and self.stop_on_stale:
                    logger.warning(f"stopping: feed considered stale (unchanged cycles: {unchanged})")
                    rc = 2
                    break

                self._sleep_rest(cycle_start)
        except KeyboardInterrupt:
            logger.info("watch ended (CTRL+C)")
            rc = 0
        return rc

    def _sleep_rest(self, cycle_start: float) -> None:
        """Sleep so the whole cycle lasts ~``interval`` (fetch time deducted).

        Args:
            cycle_start: ``time.time()`` at the beginning of the cycle.
        """
        rest = self.interval - (time.time() - cycle_start)
        if rest > 0:
            time.sleep(rest)
