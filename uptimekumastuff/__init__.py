"""uptimekumastuff — Uptime-Kuma export/import package.

Central logging setup lives here (loguru), mirroring ucmstuff's setup. The
``uptime_kuma_api`` / ``python-socketio`` libraries log via the stdlib
``logging`` module, so :func:`configure_logging` installs an intercept handler
that funnels those records into loguru instead of rewriting every call site.
"""

import inspect
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING

from dotenv import load_dotenv
from loguru import logger as glogger
from tabulate import tabulate

if TYPE_CHECKING:
    from loguru import Record

__version__ = "0.3.0"

__all__ = [
    "__version__",
    "CREDS_FILENAME",
    "configure_logging",
    "creds_file_candidates",
    "env_url",
    "load_creds_env",
    "print_banner",
]

CREDS_FILENAME = "uptimekuma.local.env"


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

    ``uptime_kuma_api`` and its socketio/engineio transports use
    ``logging.getLogger(...)``; installing this on the root logger lets
    :func:`configure_logging` govern all of them (format, level, colour) without
    touching the individual call sites.
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
    # (this package's and third-party libraries') flows through loguru.
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.getLevelNamesMapping()[level_name], force=True)


def creds_file_candidates() -> list[Path]:
    """List the ``uptimekuma.local.env`` locations, in precedence order.

    The current working directory comes first so a per-deployment file next to the
    state YAML wins over the one shipped in the package directory. Duplicates (the
    CWD *is* the package directory) are collapsed.

    Returns:
        Candidate paths, highest precedence first. They need not exist.
    """
    candidates: list[Path] = []
    for path in (Path.cwd() / CREDS_FILENAME, Path(__file__).parent / CREDS_FILENAME):
        resolved = path.resolve()
        if resolved not in {c.resolve() for c in candidates}:
            candidates.append(path)
    return candidates


def load_creds_env() -> list[Path]:
    """Load the credentials files into ``os.environ``.

    ``load_dotenv`` never overwrites an already-set variable, so real environment
    variables beat every file and the first candidate beats the later ones.

    Returns:
        The candidate paths that were searched, in precedence order — for error
        messages naming where the caller looked.
    """
    candidates = creds_file_candidates()
    for candidate in candidates:
        load_dotenv(candidate)
    return candidates


def env_url() -> str | None:
    """Read the instance URL from ``UPTIME_KUMA_URL``.

    Loads the creds files first, so the variable may come from the environment or
    from either ``uptimekuma.local.env``. It is the lowest-precedence source: a URL
    on the command line or in a state file always wins, so an ambient default can
    never silently redirect a run at a different instance than the file names.

    Returns:
        The configured base URL, or ``None`` if unset or empty.
    """
    load_creds_env()
    return os.environ.get("UPTIME_KUMA_URL") or None


def print_banner() -> None:
    """Log the startup banner with version, build time and project links."""
    startup_rows = [
        ["version", __version__],
        ["buildtime", os.getenv("BUILDTIME", "unknown")],
        ["github", "https://github.com/vroomfondel/somestuff/tree/main/uptimekumastuff"],
        ["Docker Hub", "https://hub.docker.com/r/xomoxcc/somestuff"],
    ]
    table_str = tabulate(startup_rows, tablefmt="mixed_grid")
    lines = table_str.split("\n")
    table_width = len(lines[0])
    title = "uptime-kuma export/import starting up"
    title_border = "┍" + "━" * (table_width - 2) + "┑"
    title_row = "│ " + title.center(table_width - 4) + " │"
    separator = lines[0].replace("┍", "┝").replace("┑", "┥").replace("┯", "┿")

    glogger.opt(raw=True).info(f"\n{title_border}\n{title_row}\n{separator}\n{'\n'.join(lines[1:])}\n")
