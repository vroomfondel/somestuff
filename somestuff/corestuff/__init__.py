"""corestuff — shared settings loader (pydantic-settings over config.yaml + config.local.yaml) and helpers.

Lookup order for ``config.yaml``: ``SOMESTUFF_CONFIG_PATH``, ``SOMESTUFF_CONFIG_DIR_PATH``, the current
directory, ``/app``, then the packaged sample. ``config.local.yaml`` is searched the same way
(``SOMESTUFF_CONFIG_LOCAL_PATH``) and merged over it. Nothing is read at import time: call
:func:`somestuff.corestuff.config.get_settings`, or use the lazy ``settings`` proxy.
"""

import inspect
import logging
import os
import sys
from collections.abc import Callable
from types import FrameType
from typing import TYPE_CHECKING

from loguru import logger as glogger

if TYPE_CHECKING:
    from loguru import Record

__version__ = "0.1.0"

__all__ = ["__version__", "configure_logging"]


def _loguru_skiplog_filter(record: "Record") -> bool:
    return not record.get("extra", {}).get("skiplog", False)


class _InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        level: str | int
        try:
            level = glogger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame: FrameType | None = inspect.currentframe()
        depth = 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        glogger.bind(classname=record.name).opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(
    verbose: bool = False, loguru_filter: Callable[["Record"], bool] = _loguru_skiplog_filter
) -> None:
    level_name: str = "DEBUG" if verbose else os.getenv("LOGURU_LEVEL", "INFO")
    os.environ["LOGURU_LEVEL"] = level_name
    glogger.remove()
    logger_fmt: str = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{module}</cyan>::<cyan>{extra[classname]}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )
    glogger.add(sys.stderr, level=level_name, format=logger_fmt, filter=loguru_filter)  # type: ignore[arg-type]
    glogger.configure(extra={"classname": "None", "skiplog": False})
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.getLevelNamesMapping()[level_name], force=True)
