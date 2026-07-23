"""dnsstuff — DNS utilities for SPF resolution, ipset management and mDNS diagnostics.

Bundles several standalone DNS/network tools:

* :mod:`dnsstuff.spf_ipset_updater` — SPF record crawler → ipset allowlist updater
* :mod:`dnsstuff.update_21vianet_ips` — Microsoft 365 21Vianet Exchange IP → ipset updater
* :mod:`dnsstuff.mdns_who` — resolve a ``.local`` name via mDNS and show who answered

Central logging setup lives here (loguru), mirroring the other packages in this
repo. Because some of these tools use third-party libraries that log via the
stdlib ``logging`` module (``dns.resolver``, ``pyroute2``, ``urllib`` …),
:func:`configure_logging` installs an intercept handler that funnels those
records into loguru instead of rewriting every call site.
"""

import inspect
import logging
import os
import sys
from collections.abc import Callable
from types import FrameType
from typing import TYPE_CHECKING

from loguru import logger as glogger
from tabulate import tabulate

if TYPE_CHECKING:
    from loguru import Record

__version__ = "0.2.0"

__all__ = ["__version__", "configure_logging", "print_banner"]


def _loguru_skiplog_filter(record: "Record") -> bool:
    """Decide whether a loguru record should be emitted.

    Args:
        record: The loguru record about to be handled by a sink.

    Returns:
        ``False`` for records whose ``extra['skiplog']`` flag is truthy (they are
        hidden), ``True`` otherwise.
    """
    return not record.get("extra", {}).get("skiplog", False)


class _InterceptHandler(logging.Handler):
    """Route stdlib ``logging`` records into loguru with correct call-site info.

    Third-party libraries used by this package (``dns.resolver``, ``pyroute2``,
    ``urllib`` …) log via ``logging.getLogger(...)``; installing this on the root
    logger lets :func:`configure_logging` govern all of them (format, level,
    colour) without touching the individual call sites.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Forward a single stdlib record to loguru.

        Args:
            record: The stdlib log record to re-emit through loguru, preserving
                its level, message, exception info and originating call site.
        """
        # Map the stdlib level name to a loguru level, falling back to the number.
        level: str | int
        try:
            level = glogger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Walk out of the logging module so {module}/{function}/{line} point at the
        # real caller rather than at this handler (canonical loguru intercept recipe).
        frame: FrameType | None = inspect.currentframe()
        depth = 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        glogger.bind(classname=record.name).opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(
    verbose: bool = False, loguru_filter: Callable[["Record"], bool] = _loguru_skiplog_filter
) -> None:
    """Configure a default ``loguru`` sink and funnel stdlib logging into it.

    Args:
        verbose: ``True`` logs at DEBUG, otherwise honours ``LOGURU_LEVEL`` (INFO
            by default).
        loguru_filter: Predicate deciding, per loguru record, whether it is emitted
            by the sink. Defaults to :func:`_loguru_skiplog_filter`.
    """
    level_name: str = "DEBUG" if verbose else os.getenv("LOGURU_LEVEL", "INFO")
    os.environ["LOGURU_LEVEL"] = level_name
    glogger.remove()
    logger_fmt: str = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{module}</cyan>::<cyan>{extra[classname]}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )
    glogger.add(sys.stderr, level=level_name, format=logger_fmt, filter=loguru_filter)  # type: ignore[arg-type]
    glogger.configure(extra={"classname": "None", "skiplog": False})
    # Replace any stdlib handlers with the intercept so all ``logging`` output
    # (third-party libraries') flows through loguru.
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.getLevelNamesMapping()[level_name], force=True)


def print_banner(title: str) -> None:
    """Log the startup banner with version, build time and project links.

    Args:
        title: Tool-specific banner title (this package bundles several CLIs, so
            each one passes its own, e.g. ``"mdns_who"``).
    """
    startup_rows = [
        ["version", __version__],
        ["buildtime", os.getenv("BUILDTIME", "unknown")],
        ["github", "https://github.com/vroomfondel/somestuff/tree/main/dnsstuff"],
        ["Docker Hub", "https://hub.docker.com/r/xomoxcc/somestuff"],
    ]
    table_str = tabulate(startup_rows, tablefmt="mixed_grid")
    lines = table_str.split("\n")
    table_width = len(lines[0])
    banner_title = f"{title} starting up"
    title_border = "┍" + "━" * (table_width - 2) + "┑"
    title_row = "│ " + banner_title.center(table_width - 4) + " │"
    separator = lines[0].replace("┍", "┝").replace("┑", "┥").replace("┯", "┿")

    glogger.opt(raw=True).info(f"\n{title_border}\n{title_row}\n{separator}\n{'\n'.join(lines[1:])}\n")
