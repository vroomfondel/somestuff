"""SPF record crawler and ipset updater for SMTP allowlisting.

Recursively resolves SPF records for given domains, collects all referenced
IPv4 addresses and networks, and atomically updates a Linux kernel ipset
via the pyroute2 netlink interface. Designed for maintaining firewall
allowlists on mail relay hosts.

Author: vroomfondel
Source: https://github.com/vroomfondel/somestuff/blob/main/dnsstuff/spf_ipset_updater.py
"""

import argparse
import os
import random
import socket
import string
import sys
from collections.abc import Callable
from typing import Any

import dns.name
import dns.rdtypes.ANY.SPF
import dns.rdtypes.ANY.TXT
import dns.resolver
from loguru import logger as glogger
from pyroute2.ipset import IPSet, PortEntry, PortRange
from tabulate import tabulate

# glogger.disable(__name__)

__version__ = "0.1.0"


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
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{module}</cyan>::<cyan>{extra[classname]}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )
    glogger.add(sys.stderr, level=os.getenv("LOGURU_LEVEL"), format=logger_fmt, filter=loguru_filter)  # type: ignore[arg-type]
    glogger.configure(extra={"classname": "None", "skiplog": False})


def print_banner() -> None:
    """Log the operator startup banner with version and project links."""
    startup_rows = [
        ["version", __version__],
        ["github", "https://github.com/vroomfondel/somestuff/tree/main/dnsstuff"],
        ["Docker Hub", "https://hub.docker.com/r/xomoxcc/somestuff"],
    ]
    table_str = tabulate(startup_rows, tablefmt="mixed_grid")
    lines = table_str.split("\n")
    table_width = len(lines[0])
    title = "spf_ipset_updater starting up"
    title_border = "\u250d" + "\u2501" * (table_width - 2) + "\u2511"
    title_row = "\u2502 " + title.center(table_width - 4) + " \u2502"
    separator = lines[0].replace("\u250d", "\u251d").replace("\u2511", "\u2525").replace("\u252f", "\u253f")

    glogger.opt(raw=True).info(f"\n{title_border}\n{title_row}\n{separator}\n{'\n'.join(lines[1:])}\n")


configure_logging()

logger = glogger.bind(classname="spf_ipset_updater")
print_banner()


def ipsettest() -> None:
    """Demonstrates basic ipset operations using pyroute2.

    Creates and manipulates ipsets of different types (hash:ip, bitmap:port,
    hash:net,port) to showcase the pyroute2 IPSet API. Each example creates
    an ipset, adds entries, tests membership, and cleans up.
    """
    ipset: IPSet = IPSet()
    ipset.swap("oldset", "newset")

    ipset.create("foo", stype="hash:ip")
    ipset.add("foo", "198.51.100.1", etype="ip")
    ipset.add("foo", "198.51.100.2", etype="ip")
    logger.info(f"test 198.51.100.1: {ipset.test('foo', '198.51.100.1')}")  # True
    logger.info(f"test 198.51.100.10: {ipset.test('foo', '198.51.100.10')}")  # False
    msg_list = ipset.list("foo")
    for msg in msg_list:
        for attr_data in msg.get_attr("IPSET_ATTR_ADT").get_attrs("IPSET_ATTR_DATA"):
            for attr_ip_from in attr_data.get_attrs("IPSET_ATTR_IP_FROM"):
                for ipv4 in attr_ip_from.get_attrs("IPSET_ATTR_IPADDR_IPV4"):
                    logger.info(f"- {ipv4}")
    ipset.destroy("foo")
    ipset.close()

    ipset = IPSet()
    ipset.create("bar", stype="bitmap:port", bitmap_ports_range=(1000, 2000))
    ipset.add("bar", 1001, etype="port")
    ipset.add("bar", PortRange(1500, 2000), etype="port")
    logger.info(f"test port 1600: {ipset.test('bar', 1600, etype='port')}")  # True
    logger.info(f"test port 2600: {ipset.test('bar', 2600, etype='port')}")  # False
    ipset.destroy("bar")
    ipset.close()

    ipset = IPSet()
    protocol_tcp = socket.getprotobyname("tcp")
    ipset.create("foobar", stype="hash:net,port")
    port_entry_http = PortEntry(80, protocol=protocol_tcp)
    ipset.add("foobar", ("198.51.100.0/24", port_entry_http), etype="net,port")
    logger.info(
        f"test ip,port http: {ipset.test('foobar', ('198.51.100.1', port_entry_http), etype='ip,port')}"
    )  # True
    port_entry_https = PortEntry(443, protocol=protocol_tcp)
    logger.info(
        f"test ip,port https: {ipset.test('foobar', ('198.51.100.1', port_entry_https), etype='ip,port')}"
    )  # False
    ipset.destroy("foobar")
    ipset.close()


def get_spf_records(domain: str) -> list[str]:
    """Retrieves SPF records for a specific domain.

    SPF records are typically stored in TXT records. This function queries
    the TXT records for the given domain and filters for SPF entries
    (starting with ``v=spf1``).

    Args:
        domain: The domain name for which SPF records should be retrieved.

    Returns:
        List of found SPF record strings.
    """
    spf_records: list[str] = []

    try:
        # Query TXT records, as SPF records are stored there
        answers = dns.resolver.resolve(domain, "TXT")
        txt_rdata: dns.rdtypes.ANY.TXT.TXT

        spf_found = False
        logger.info(f"SPF records for {domain}:")

        for txt_rdata in answers:
            # TXT records can consist of multiple strings
            txt_content = "".join([s.decode("utf-8") if isinstance(s, bytes) else s for s in txt_rdata.strings])

            # Check if it's an SPF record (starts with "v=spf1")
            if txt_content.startswith("v=spf1"):
                spf_found = True
                spf_records.append(txt_content)

                # Create an SPF object for correct typing
                spf_rdata: dns.rdtypes.ANY.SPF.SPF = dns.rdtypes.ANY.SPF.SPF(
                    txt_rdata.rdclass, txt_rdata.rdtype, txt_rdata.strings
                )

                logger.debug(f"spf_rdata type: {type(spf_rdata)}")
                logger.info(f"SPF Record: {txt_content}")
                logger.info(f"SPF Record (to_text): {spf_rdata.to_text()}")

        if not spf_found:
            logger.warning(f"No SPF records found in TXT records for {domain}")

    except dns.resolver.NoAnswer:
        logger.warning(f"No TXT records found for {domain}")
    except dns.resolver.NXDOMAIN:
        logger.error(f"Domain {domain} does not exist")
    except Exception as e:
        logger.error(f"Error retrieving SPF records: {e}")

    return spf_records


def resolve_spf_to_ipv4(domain: str, visited_domains: set[str] | None = None) -> list[str]:
    """Resolves SPF records recursively and collects all IPv4 addresses.

    Processes ``ip4:``, ``include:``, and ``mx:`` mechanisms. Tracks already
    visited domains to prevent infinite loops caused by circular includes.

    Args:
        domain: The domain name for which SPF records should be resolved.
        visited_domains: Set of already visited domains (prevents infinite
            loops). Created automatically on first call.

    Returns:
        List of all found IPv4 addresses and networks (CIDR notation).
    """
    if visited_domains is None:
        visited_domains = set()

    # Avoid infinite loops with circular includes
    if domain in visited_domains:
        logger.debug(f"Domain {domain} already visited, skipping...")
        return []

    visited_domains.add(domain)
    ipv4_addresses: list[str] = []

    # Get SPF records for the domain
    spf_records = get_spf_records(domain)

    if not spf_records:
        return ipv4_addresses

    # Process each SPF record
    for spf_record in spf_records:
        # Split the SPF record into individual mechanisms
        mechanisms = spf_record.split()

        for mechanism in mechanisms:
            # Extract direct IPv4 addresses
            if mechanism.startswith("ip4:"):
                ipv4 = mechanism[4:]  # Remove 'ip4:' prefix
                ipv4_addresses.append(ipv4)
                logger.info(f"  → Found IPv4: {ipv4}")

            # Process include directives recursively
            elif mechanism.startswith("include:"):
                include_domain = mechanism[8:]  # Remove 'include:' prefix
                logger.info(f"  → Processing include: {include_domain}")

                # Recursive call for the include domain
                included_ipv4s: list[str] = resolve_spf_to_ipv4(include_domain, visited_domains)
                ipv4_addresses.extend(included_ipv4s)

            # Process MX mechanisms
            elif mechanism.startswith("mx:") or mechanism == "mx":
                # Determine the domain for the MX query
                if mechanism == "mx":
                    mx_domain = domain  # Use the current domain
                else:
                    mx_domain = mechanism[3:]  # Remove 'mx:' prefix

                logger.info(f"  → Processing MX: {mx_domain}")

                try:
                    # Get MX records
                    mx_answers = dns.resolver.resolve(mx_domain, "MX")

                    for mx_rdata in mx_answers:
                        mx_host = str(mx_rdata.exchange).rstrip(".")
                        logger.info(f"    → MX host found: {mx_host}")

                        try:
                            # Resolve A records (IPv4) for the MX host
                            # dns.resolver.resolve() follows CNAMEs automatically,
                            # but only returns the final A records
                            a_answers = dns.resolver.resolve(mx_host, "A")

                            for a_rdata in a_answers:
                                ipv4 = str(a_rdata)
                                ipv4_addresses.append(ipv4)
                                logger.info(f"      → Found IPv4 (MX): {ipv4}")

                            # Check if CNAMEs were involved (for debugging purposes)
                            if hasattr(a_answers, "canonical_name") and a_answers.canonical_name != dns.name.from_text(
                                mx_host
                            ):
                                logger.debug(f"      → (via CNAME: {a_answers.canonical_name})")

                        except dns.resolver.NoAnswer:
                            logger.warning(f"      → No A records for {mx_host}")
                        except dns.resolver.NXDOMAIN:
                            logger.error(f"      → MX host {mx_host} does not exist")
                        except dns.resolver.NoNameservers:
                            logger.warning(f"      → No nameservers available for {mx_host}")
                        except Exception as e:
                            logger.error(f"      → Error resolving {mx_host}: {e}")

                except dns.resolver.NoAnswer:
                    logger.warning(f"    → No MX records for {mx_domain}")
                except dns.resolver.NXDOMAIN:
                    logger.error(f"    → Domain {mx_domain} does not exist")
                except Exception as e:
                    logger.error(f"    → Error retrieving MX records: {e}")

    return ipv4_addresses


def ddd() -> None:
    """Dumps all TXT records for pcbway.com for debugging purposes."""
    answers: dns.resolver.Answer = dns.resolver.resolve("pcbway.com", "TXT")
    rdata: dns.rdtypes.ANY.TXT.TXT
    for rdata in answers:
        logger.debug(f"{type(rdata)=} {rdata=}")
        logger.debug(f"Resolve response for pcbway.com TXT record : {rdata.to_text()=}")


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
        ipv4_entries: List of ``(ip_or_network, source_domain)`` tuples to be
            inserted into the ipset. The source domain is stored as a comment
            when ``enable_comment`` is ``True``.
        do_actual_swap: When ``True``, performs the actual swap. When ``False``,
            populates the temporary ipset but skips the swap (dry-run mode).
        create_srcname_defaulttype: Ipset type to use when the source ipset
            does not exist yet. Set to ``None`` to raise an error instead.
        enable_comment: When ``True``, creates the ipset with comment support
            and attaches the source domain as a comment to each entry.
    """
    ipset = IPSet()

    src_exists: bool = False

    try:
        # 1. Determine the type of the existing ipset
        stype: str | None = None
        try:
            msg_list = ipset.list(srcname)
            for msg in msg_list:
                logger.debug(f"{type(msg)=} {msg=}")
                type_attr = msg.get_attr("IPSET_ATTR_TYPENAME")
                if type_attr:
                    stype = type_attr.decode("utf-8") if isinstance(type_attr, bytes) else type_attr
                    break
        except Exception as list_err:
            # errno 2 = "No such file or directory" → ipset does not exist yet
            if getattr(list_err, "code", None) == 2:
                logger.debug(f"ipset '{srcname}' does not exist yet")
            else:
                raise

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
        for entry, source_domain in ipv4_entries:
            if not ipset.test(temp_name, entry, etype=etype):
                add_kwargs: dict[str, str] = {}
                if enable_comment:
                    add_kwargs["comment"] = source_domain
                ipset.add(temp_name, entry, etype=etype, **add_kwargs)
                logger.info(f"  → Added: {entry} ({source_domain})")
            else:
                logger.info(f"  → Skipped: {entry} ({source_domain})")

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


def parse_args() -> argparse.Namespace:
    """Parses command-line arguments for the SPF-to-ipset resolver.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="Resolve SPF records for domains and update a Linux ipset with the collected IPv4 addresses.",
    )
    parser.add_argument(
        "domains",
        nargs="*",
        default=["naddod.com", "aliexpress.com", "notice.aliexpress.com", "pcbway.com", "mail-notify.pcbway.com"],
        help="Domain names to resolve SPF records for (default: naddod.com aliexpress.com notice.aliexpress.com pcbway.com mail-notify.pcbway.com)",
    )
    parser.add_argument(
        "--ipset-name",
        default="smtpallowlist",
        help="Name of the ipset to update (default: smtpallowlist)",
    )
    parser.add_argument(
        "--ipset-type",
        default="hash:net",
        help="Ipset type to use when creating a new ipset (default: hash:net, with comment extension enabled)",
    )
    parser.add_argument(
        "--no-comment",
        action="store_true",
        help="Disable the ipset comment extension (comments are enabled by default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Populate the ipset but skip the atomic swap",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point for SPF-to-ipset resolution.

    Parses command-line arguments, resolves all SPF records for the given
    domains, collects IPv4 addresses, and updates the specified ipset if
    running as root.
    """
    args: argparse.Namespace = parse_args()
    domains: list[str] = args.domains

    logger.info(f"Processing {len(domains)} domain(s): {', '.join(domains)}")

    # Collect all IPv4 entries (ip/net, source_domain) for all domains
    all_ipv4_combined: list[tuple[str, str]] = []

    for domain in domains:
        logger.info(f"{'=' * 50}")
        logger.info(f"Processing domain: {domain}")
        logger.info(f"{'=' * 50}")

        get_spf_records(domain)

        logger.info(f"{'=' * 50}")
        logger.info(f"Resolving SPF records to IPv4 addresses for {domain}")
        logger.info(f"{'=' * 50}")

        domain_ipv4 = resolve_spf_to_ipv4(domain)

        logger.info(f"{'=' * 50}")
        logger.info(f"Found IPv4 addresses for {domain}: {len(domain_ipv4)}")
        logger.info(f"{'=' * 50}")
        for ip in domain_ipv4:
            logger.info(f"  - {ip}")

        all_ipv4_combined.extend((ip, domain) for ip in domain_ipv4)

    logger.info(f"{'=' * 50}")
    logger.info(f"Total IPv4 entries found (all domains): {len(all_ipv4_combined)}")
    logger.info(f"{'=' * 50}")
    for ip, src in all_ipv4_combined:
        logger.info(f"  - {ip} ({src})")

    # Check if the user has root privileges
    if os.getuid() == 0:
        ipset_update_with_swap(
            args.ipset_name,
            all_ipv4_combined,
            do_actual_swap=not args.dry_run,
            create_srcname_defaulttype=args.ipset_type,
            enable_comment=not args.no_comment,
        )
    else:
        logger.warning(f"{'=' * 50}")
        logger.warning("ipset update will be skipped!")
        logger.warning("Root privileges (UID 0) are required to update ipsets.")
        logger.warning(f"Current UID: {os.getuid()}")
        logger.warning(f"{'=' * 50}")


if __name__ == "__main__":
    main()
