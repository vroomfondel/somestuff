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

from oepnvstuff.gtfs_realtime import FetchStatus, RealtimeFetcher, RealtimeSnapshot, ServiceAlert, parse_realtime
from oepnvstuff.gtfs_static import StaticFeedIndex

logger = logging.getLogger(__name__)


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
    """

    wall_time: datetime.datetime
    fetch_status: FetchStatus | None
    snapshot: RealtimeSnapshot | None
    per_line: dict[str, LineStatus] = field(default_factory=dict)
    feed_age: int | None = None
    unchanged_cycles: int = 0
    stale: bool = False
    payload_bytes: int | None = None

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

    Hits whose ``route_id`` is unknown to the index (matched via ``trip_id``
    with a diverging route id) are attributed to their raw route id and thus
    won't show under a target line — by design, target matching happens in
    :func:`~oepnvstuff.gtfs_realtime.parse_realtime`.

    Args:
        snapshot: The parsed realtime snapshot.
        index: The static schedule index (provides line names and trip counts).

    Returns:
        Line name → status, one entry per line in ``index.target_lines``.
    """
    updates_per_line: dict[str, int] = {}
    delays_per_line: dict[str, list[int]] = {}
    for hit in snapshot.hits:
        line = index.route_shortname.get(hit.route_id, hit.route_id or "?").upper()
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
        """
        self._fetcher = fetcher
        self._index = index
        self.interval = interval
        self.max_age = max_age
        self.stale_cycles = stale_cycles
        self.stop_on_stale = stop_on_stale

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
