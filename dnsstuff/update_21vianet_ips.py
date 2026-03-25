#!/usr/bin/env python3
"""Microsoft 365 21Vianet (China) Exchange IP updater for ipset-based allowlisting.

Polls the Microsoft endpoint API for version changes, downloads Exchange Online
IP ranges, and atomically updates Linux kernel ipsets via the pyroute2 netlink
interface. Produces two ipsets: one for all Exchange IPv4 networks and one for
SMTP-only (port 25) networks.

Cron example (daily at 03:00)::

    0 3 * * * /usr/bin/python3 /opt/scripts/update_21vianet_ips.py 2>&1 | logger -t 21vianet

Author: vroomfondel
Source: https://github.com/vroomfondel/somestuff/blob/main/dnsstuff/update_21vianet_ips.py
"""

import argparse
import json
import os
import random
import string
import sys
import uuid
from collections.abc import Callable
from ipaddress import ip_network
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from loguru import logger as glogger
from pyroute2.ipset import IPSet
from tabulate import tabulate

# -- Constants ---------------------------------------------------------------

__version__: str = "0.1.0"

CLIENT_ID: str = str(uuid.uuid4())
VERSION_URL: str = f"https://endpoints.office.com/version/China?clientrequestid={CLIENT_ID}"
ENDPOINTS_URL: str = f"https://endpoints.office.com/endpoints/China?clientrequestid={CLIENT_ID}"

# Exchange Online service IDs (per Microsoft documentation):
#   1  = Exchange Optimize (HTTPS/HTTP)
#   2  = Exchange Allow (HTTPS) – Protection/EOP
#   20 = Exchange Allow (IMAP/POP/Submission)
#   27 = Exchange Allow (SMTP Port 25) – Mail flow
EXCHANGE_IDS_ALL: list[int] = [1, 2, 20, 27]
EXCHANGE_IDS_SMTP: list[int] = [2, 27]

SERVICE_NAMES: dict[int, str] = {
    1: "Exchange Optimize",
    2: "Exchange Allow/EOP",
    20: "Exchange IMAP/POP",
    27: "Exchange SMTP",
}

DEFAULT_OUTPUT_DIR: str = "/etc/firewall/21vianet"
DEFAULT_API_TIMEOUT: int = 30


# -- Logging -----------------------------------------------------------------


def _loguru_skiplog_filter(record: dict) -> bool:  # type: ignore[type-arg]
    """Filter function to hide records with ``extra['skiplog']`` set."""
    return not record.get("extra", {}).get("skiplog", False)


def configure_logging(
    loguru_filter: Callable[[dict[str, Any]], bool] = _loguru_skiplog_filter,
) -> None:
    """Configure a default ``loguru`` sink with a convenient format and filter."""
    os.environ["LOGURU_LEVEL"] = os.getenv("LOGURU_LEVEL", "DEBUG")
    glogger.remove()
    logger_fmt: str = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
        "<cyan>{module}</cyan>::<cyan>{extra[classname]}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )
    glogger.add(sys.stderr, level=os.getenv("LOGURU_LEVEL"), format=logger_fmt, filter=loguru_filter)  # type: ignore[arg-type]
    glogger.configure(extra={"classname": "None", "skiplog": False})


def print_banner() -> None:
    """Log the startup banner with version and project links."""
    startup_rows = [
        ["version", __version__],
        ["github", "https://github.com/vroomfondel/somestuff/tree/main/dnsstuff"],
        ["Docker Hub", "https://hub.docker.com/r/xomoxcc/somestuff"],
    ]
    table_str = tabulate(startup_rows, tablefmt="mixed_grid")
    lines = table_str.split("\n")
    table_width = len(lines[0])
    title = "update_21vianet_ips starting up"
    title_border = "\u250d" + "\u2501" * (table_width - 2) + "\u2511"
    title_row = "\u2502 " + title.center(table_width - 4) + " \u2502"
    separator = lines[0].replace("\u250d", "\u251d").replace("\u2511", "\u2525").replace("\u252f", "\u253f")

    glogger.opt(raw=True).info(f"\n{title_border}\n{title_row}\n{separator}\n{'\n'.join(lines[1:])}\n")


configure_logging()

logger = glogger.bind(classname="update_21vianet_ips")
print_banner()


# -- Microsoft API -----------------------------------------------------------


def api_get(url: str, timeout: int = DEFAULT_API_TIMEOUT) -> dict[str, Any] | list[dict[str, Any]]:
    """Fetches JSON from the Microsoft endpoint API.

    Depending on the endpoint, the API returns either a single JSON object
    (version endpoint) or a JSON array of objects (endpoints endpoint).

    Args:
        url: The API URL to query.
        timeout: HTTP request timeout in seconds.

    Returns:
        Parsed JSON response (dict or list of dicts).

    Raises:
        URLError: If the HTTP request fails.
        json.JSONDecodeError: If the response is not valid JSON.
    """
    req = Request(url, headers={"User-Agent": "21Vianet-IP-Updater/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))  # type: ignore[no-any-return]
    except (URLError, json.JSONDecodeError) as e:
        logger.error(f"API request failed: {url} → {e}")
        raise


def get_remote_version(timeout: int = DEFAULT_API_TIMEOUT) -> str | None:
    """Retrieves the current endpoint version from the Microsoft API.

    The version endpoint for a specific instance (China) returns a single
    JSON object with ``instance``, ``serviceArea``, and ``latest`` fields.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        The latest version string for the China instance, or ``None`` if
        the response does not match the expected format.
    """
    data: dict[str, Any] | list[dict[str, Any]] = api_get(VERSION_URL, timeout=timeout)
    if isinstance(data, dict):
        if data.get("instance") == "China":
            return data.get("latest")  # type: ignore[return-value]
    elif isinstance(data, list):
        for entry in data:
            if entry.get("instance") == "China":
                return entry.get("latest")  # type: ignore[return-value]
    return None


def get_local_version(output_dir: Path) -> str:
    """Reads the locally stored version from disk.

    Args:
        output_dir: Directory containing the ``last_version.txt`` file.

    Returns:
        The stored version string, or ``"0"`` if no version file exists.
    """
    version_file: Path = output_dir / "last_version.txt"
    if version_file.exists():
        return version_file.read_text().strip()
    return "0"


def save_local_version(output_dir: Path, version: str) -> None:
    """Persists the current version to disk.

    Args:
        output_dir: Directory in which to write ``last_version.txt``.
        version: The version string to store.
    """
    version_file: Path = output_dir / "last_version.txt"
    version_file.write_text(version + "\n")


# -- IP extraction -----------------------------------------------------------


def download_and_extract(
    ids_all: list[int],
    ids_smtp: list[int],
    timeout: int = DEFAULT_API_TIMEOUT,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Downloads endpoints and extracts Exchange Online IPv4 ranges.

    Fetches the full endpoint list from the Microsoft API, filters for
    Exchange Online services, and splits results into two lists: all Exchange
    IPv4 networks and SMTP-only IPv4 networks.

    Args:
        ids_all: Service IDs to include in the full (DNS + SMTP) list.
        ids_smtp: Subset of service IDs relevant for SMTP port 25.
        timeout: HTTP request timeout in seconds.

    Returns:
        A tuple of two lists of ``(cidr, comment)`` tuples:
        - First list: all Exchange IPv4 networks.
        - Second list: SMTP-only IPv4 networks.
        The comment contains the service description and ID.
    """
    logger.info("Downloading endpoints from Microsoft API...")
    raw: dict[str, Any] | list[dict[str, Any]] = api_get(ENDPOINTS_URL, timeout=timeout)
    data: list[dict[str, Any]] = raw if isinstance(raw, list) else [raw]

    all_entries: list[tuple[str, str]] = []
    smtp_entries: list[tuple[str, str]] = []
    seen_all: set[str] = set()
    seen_smtp: set[str] = set()
    ipv6_count: int = 0

    for entry in data:
        if entry.get("serviceArea") != "Exchange":
            continue

        entry_id: int = entry.get("id", -1)
        ips: list[str] = entry.get("ips", [])

        if entry_id not in ids_all:
            continue

        comment: str = f"{SERVICE_NAMES.get(entry_id, 'Exchange')} ({entry_id})"

        for cidr in ips:
            try:
                net = ip_network(cidr, strict=False)
            except ValueError:
                logger.warning(f"Skipping invalid CIDR: {cidr}")
                continue

            if net.version == 4:
                if cidr not in seen_all:
                    seen_all.add(cidr)
                    all_entries.append((cidr, comment))

                if entry_id in ids_smtp and cidr not in seen_smtp:
                    seen_smtp.add(cidr)
                    smtp_entries.append((cidr, comment))

            elif net.version == 6:
                ipv6_count += 1

    # Sort by network address
    all_entries.sort(key=lambda t: ip_network(t[0], strict=False).network_address)
    smtp_entries.sort(key=lambda t: ip_network(t[0], strict=False).network_address)

    logger.info(
        f"Extraction complete: {len(all_entries)} IPv4 all, "
        f"{len(smtp_entries)} IPv4 SMTP, {ipv6_count} IPv6 (skipped)"
    )
    return all_entries, smtp_entries


# -- ipset operations --------------------------------------------------------


def ipset_exists(ipset_instance: IPSet, name: str) -> bool:
    """Checks whether an ipset with the given name exists.

    Args:
        ipset_instance: An open pyroute2 IPSet connection.
        name: The ipset name to look up.

    Returns:
        ``True`` if the ipset exists, ``False`` otherwise.
    """
    try:
        # list() without parameters lists all ipsets
        all_ipsets = ipset_instance.list()
        for msg in all_ipsets:
            setname_attr = msg.get_attr("IPSET_ATTR_SETNAME")
            if setname_attr:
                setname = setname_attr.decode("utf-8") if isinstance(setname_attr, bytes) else setname_attr
                if setname == name:
                    return True
        return False
    except Exception:
        return False


def ipset_update_with_swap(
    srcname: str,
    ipv4_entries: list[tuple[str, str]],
    do_actual_swap: bool = True,
    create_srcname_defaulttype: str | None = "hash:net",
    enable_comment: bool = True,
) -> None:
    """Updates an ipset atomically using a swap operation.

    Creates a temporary ipset, populates it with the given entries, and swaps
    it with the existing ipset to achieve an atomic update. If the source ipset
    does not exist yet, it is created directly (no swap needed).

    Args:
        srcname: Name of the ipset to be updated.
        ipv4_entries: List of ``(ip_or_network, comment_text)`` tuples to be
            inserted into the ipset. The comment text is stored as a comment
            when ``enable_comment`` is ``True``.
        do_actual_swap: When ``True``, performs the actual swap. When ``False``,
            populates the temporary ipset but skips the swap (dry-run mode).
        create_srcname_defaulttype: Ipset type to use when the source ipset
            does not exist yet. Set to ``None`` to raise an error instead.
        enable_comment: When ``True``, creates the ipset with comment support
            and attaches the comment text to each entry.
    """
    ipset = IPSet()

    src_exists: bool = False

    try:
        # 1. Determine the type of the existing ipset
        msg_list = ipset.list(srcname)
        stype = None

        for msg in msg_list:
            logger.debug(f"{type(msg)=} {msg=}")

            # Get the set type from the attributes
            type_attr = msg.get_attr("IPSET_ATTR_TYPENAME")
            if type_attr:
                stype = type_attr.decode("utf-8") if isinstance(type_attr, bytes) else type_attr
                break

        if not stype:
            if create_srcname_defaulttype is None:
                raise ValueError(f"Could not determine type of ipset '{srcname}' and no default type was specified.")
            else:
                stype = create_srcname_defaulttype
                logger.info(
                    f"SRC ipset with name {srcname} does not exist -> using default type {create_srcname_defaulttype}"
                )
        else:
            src_exists = True
            logger.info(f"Determined ipset type: {stype}")

        # 2. Generate a random name for the temporary ipset
        random_suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        temp_name = f"tmp_{srcname}_{random_suffix}"
        if src_exists:
            logger.info(f"Temporary ipset: {temp_name}")
        else:
            temp_name = srcname

        # 3. Create the temporary ipset with the same type
        ipset.create(temp_name, stype=stype, comment=enable_comment)
        if src_exists:
            logger.info(f"Temporary ipset '{temp_name}' created (comment={enable_comment})")
        else:
            logger.info(f"ipset '{temp_name}' created (comment={enable_comment})")

        # 4. Determine the etype based on the stype
        # For most hash:ip and hash:net sets we can use "net",
        # as it also accepts individual IPs (as /32 network)
        if "hash:ip" in stype or "hash:net" in stype:
            etype = "net"
        elif "bitmap:ip" in stype:
            etype = "ip"
        else:
            # Fallback: try to use the part after the colon
            etype = stype.split(":")[1] if ":" in stype else "ip"

        logger.info(f"Using etype: {etype}")

        # 5. Add all entries to the temporary ipset
        for entry, comment_text in ipv4_entries:
            if not ipset.test(temp_name, entry, etype=etype):
                add_kwargs: dict[str, str] = {}
                if enable_comment:
                    add_kwargs["comment"] = comment_text
                ipset.add(temp_name, entry, etype=etype, **add_kwargs)
                logger.info(f"  → Added: {entry} ({comment_text})")
            else:
                logger.info(f"  → Skipped: {entry} ({comment_text})")

        if src_exists:
            logger.info(f"Total of {len(ipv4_entries)} entries added to temporary ipset")
        else:
            logger.info(f"Total of {len(ipv4_entries)} entries added to ipset")

        if do_actual_swap:
            if src_exists:
                # 6. Swap the temporary ipset with the source ipset
                ipset.swap(srcname, temp_name)
                logger.info(f"ipsets '{srcname}' and '{temp_name}' swapped")

                # 7. Destroy the temporary ipset (which now contains the old data)
                ipset.destroy(temp_name)
                logger.info(f"Temporary ipset '{temp_name}' destroyed")
        else:
            logger.warning("ACTUAL SWAP DISABLED!")

    except Exception as e:
        logger.error(f"Error updating ipset: {e}")
        # Cleanup: Try to delete the temporary ipset if it exists
        try:
            if "temp_name" in locals() and src_exists:
                ipset.destroy(temp_name)
        except:
            pass
        raise
    finally:
        ipset.close()


# -- CLI ---------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Parses command-line arguments for the 21Vianet IP updater.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Poll the Microsoft 365 21Vianet (China) endpoint API for Exchange Online IP ranges "
            "and update Linux ipsets for firewall allowlisting."
        ),
    )
    parser.add_argument(
        "--ipset-name-all",
        default="21vianet_exchange",
        help="Name of the ipset for all Exchange IPv4 networks (default: 21vianet_exchange)",
    )
    parser.add_argument(
        "--ipset-name-smtp",
        default="21vianet_smtp",
        help="Name of the ipset for SMTP-only IPv4 networks (default: 21vianet_smtp)",
    )
    parser.add_argument(
        "--ipset-type",
        default="hash:net",
        help="Ipset type to use when creating a new ipset (default: hash:net)",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for version cache file (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--api-timeout",
        type=int,
        default=DEFAULT_API_TIMEOUT,
        help=f"API request timeout in seconds (default: {DEFAULT_API_TIMEOUT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Populate ipsets but skip the atomic swap and version save",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force update even if the remote version has not changed",
    )
    return parser.parse_args()


# -- Main --------------------------------------------------------------------


def main() -> None:
    """Entry point for the 21Vianet Exchange IP updater.

    Parses command-line arguments, checks for version changes against the
    Microsoft endpoint API, downloads and extracts Exchange Online IPv4
    ranges, and updates two ipsets (all Exchange and SMTP-only) if running
    as root.
    """
    args: argparse.Namespace = parse_args()
    output_dir: Path = Path(args.output_dir)

    # -- Version check -------------------------------------------------------

    logger.info("=== 21Vianet Exchange IP update started ===")

    try:
        remote_version: str | None = get_remote_version(timeout=args.api_timeout)
    except Exception as e:
        logger.error(f"Failed to retrieve remote version: {e}")
        return

    if not remote_version:
        logger.error("Remote version is empty")
        return

    local_version: str = get_local_version(output_dir)
    logger.info(f"Version local: {local_version} | remote: {remote_version}")

    if remote_version == local_version and not args.force:
        logger.info("No change detected — exiting.")
        return

    if args.force:
        logger.info("Force update despite identical version")
    else:
        logger.info("New version detected! Updating...")

    # -- Download and extract ------------------------------------------------

    try:
        all_entries, smtp_entries = download_and_extract(
            ids_all=EXCHANGE_IDS_ALL,
            ids_smtp=EXCHANGE_IDS_SMTP,
            timeout=args.api_timeout,
        )
    except Exception as e:
        logger.error(f"Download/extraction failed: {e}")
        return

    if not all_entries:
        logger.error("No IPv4 addresses extracted — aborting (possible API issue)")
        return

    # -- Log summary ---------------------------------------------------------

    logger.info(f"{'=' * 50}")
    logger.info(f"All Exchange IPv4: {len(all_entries)} networks")
    logger.info(f"{'=' * 50}")
    for cidr, comment in all_entries:
        logger.info(f"  - {cidr} ({comment})")

    logger.info(f"{'=' * 50}")
    logger.info(f"SMTP-only IPv4: {len(smtp_entries)} networks")
    logger.info(f"{'=' * 50}")
    for cidr, comment in smtp_entries:
        logger.info(f"  - {cidr} ({comment})")

    # -- ipset update --------------------------------------------------------

    if os.getuid() == 0:
        ipset_update_with_swap(
            args.ipset_name_all,
            all_entries,
            do_actual_swap=not args.dry_run,
            create_srcname_defaulttype=args.ipset_type,
        )
        ipset_update_with_swap(
            args.ipset_name_smtp,
            smtp_entries,
            do_actual_swap=not args.dry_run,
            create_srcname_defaulttype=args.ipset_type,
        )
    else:
        logger.warning(f"{'=' * 50}")
        logger.warning("ipset update will be skipped!")
        logger.warning("Root privileges (UID 0) are required to update ipsets.")
        logger.warning(f"Current UID: {os.getuid()}")
        logger.warning(f"{'=' * 50}")

    # -- Save version --------------------------------------------------------

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        save_local_version(output_dir, remote_version)
        logger.info(f"Version saved: {remote_version}")
    else:
        logger.info("Dry-run: version file not updated")

    logger.info("=== Update complete ===")


if __name__ == "__main__":
    main()
