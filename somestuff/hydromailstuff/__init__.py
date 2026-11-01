"""hydromail — status email assembled from MQTT and Netatmo data."""

import os

from loguru import logger as glogger
from tabulate import tabulate

__version__ = "0.1.0"

__all__ = ["__version__", "print_banner"]


def print_banner() -> None:
    startup_rows = [
        ["version", __version__],
        ["buildtime", os.getenv("BUILDTIME", "unknown")],
        ["github", "https://github.com/vroomfondel/somestuff/tree/main/somestuff/hydromailstuff"],
        ["Docker Hub", "https://hub.docker.com/r/xomoxcc/somestuff"],
    ]
    table_str = tabulate(startup_rows, tablefmt="mixed_grid")
    lines = table_str.split("\n")
    table_width = len(lines[0])
    banner_title = "hydromail starting up"
    title_border = "┍" + "━" * (table_width - 2) + "┑"
    title_row = "│ " + banner_title.center(table_width - 4) + " │"
    separator = lines[0].replace("┍", "┝").replace("┑", "┥").replace("┯", "┿")

    glogger.opt(raw=True).info(f"\n{title_border}\n{title_row}\n{separator}\n{'\n'.join(lines[1:])}\n")
