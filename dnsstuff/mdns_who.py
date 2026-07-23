"""Resolve a ``.local`` name via mDNS and show WHO actually answered.

``avahi-resolve-host-name`` only gives you the final IP, not the responder. This
tool sends the mDNS query itself and collects EVERY response, logging for each:

  - the source IP of the response packet  (the host that really answered)
  - the A record(s) it advertised
  - the record TTL
  - the mDNS cache-flush bit               (set = authoritative unique record)

Stale/cache heuristic
---------------------
A genuine mDNS host answers authoritatively for its OWN name, so the packet's
source IP == the advertised A record. Two smells indicate a proxy / cache /
reflected (e.g. router) reply instead of the real host:

  1. source-IP != advertised-IP        -> someone answers on behalf of another IP
  2. more than one distinct responder   -> conflicting / cached second answerer

Optional --unicast additionally resolves the bare name and <name>.fritz.box via
the system resolver, to surface a router DHCP-lease that disagrees with mDNS
(the classic "Fritzbox hands out a stale boot-IP" case).

By default the query is sent out of EVERY local interface (so a multi-homed host
is covered on every subnet regardless of the default route); pass --iface to pin
it to a single interface.

No external DNS dependencies (raw sockets + struct). Logging/banner setup is
shared with the rest of the package via :mod:`dnsstuff`.

Usage::

    python3 -m dnsstuff.mdns_who spark5.local
    python3 -m dnsstuff.mdns_who spark5 --timeout 3 --unicast
    python3 -m dnsstuff.mdns_who spark5.local --iface 192.168.191.205   # pin the send interface

Author: vroomfondel
Source: https://github.com/vroomfondel/somestuff/blob/main/dnsstuff/mdns_who.py
"""

import argparse
import socket
import struct
import sys
import time

from loguru import logger as glogger

from dnsstuff import configure_logging, print_banner

MCAST_GRP = "224.0.0.251"
MCAST_PORT = 5353
QTYPE_A = 1
QCLASS_IN = 1
CACHE_FLUSH_BIT = 0x8000
QU_BIT = 0x8000  # "unicast response requested" (top bit of the question qclass)

logger = glogger.bind(classname="mdns_who")

# One parsed mDNS A answer: (advertised_ip, ttl, cache_flush).
Answer = tuple[str, int, bool]
# A collected answer plus the local interface the response packet arrived on.
Record = tuple[str, int, bool, str]


# --------------------------------------------------------------------------- #
# DNS wire helpers
# --------------------------------------------------------------------------- #
def encode_name(name: str) -> bytes:
    """Encode a dotted name into DNS label form (b'\\x06spark5\\x05local\\x00')."""
    out = bytearray()
    for label in name.rstrip(".").split("."):
        b = label.encode("utf-8")
        if len(b) > 63:
            raise ValueError("label too long: %r" % label)
        out.append(len(b))
        out.extend(b)
    out.append(0)
    return bytes(out)


def parse_name(data: bytes, offset: int) -> tuple[str, int]:
    """Parse a (possibly compressed) DNS name. Returns (name, next_offset)."""
    labels: list[str] = []
    next_offset: int | None = None
    jumped = False
    # Guard against pointer loops.
    for _ in range(128):
        if offset >= len(data):
            break
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:  # compression pointer
            if offset + 1 >= len(data):
                break
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                next_offset = offset + 2
            offset = pointer
            jumped = True
            continue
        offset += 1
        labels.append(data[offset : offset + length].decode("utf-8", "replace"))
        offset += length
    if next_offset is None:
        next_offset = offset
    return ".".join(labels), next_offset


def build_query(name: str, want_unicast: bool) -> tuple[bytes, bytes, bytes]:
    """Build an mDNS A-record query for `name`."""
    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)  # id=0, flags=0, 1 question
    qclass = QCLASS_IN | (QU_BIT if want_unicast else 0)
    question = encode_name(name) + struct.pack(">HH", QTYPE_A, qclass)
    return header, question, header + question


def parse_a_answers(data: bytes, want_name: str) -> list[Answer]:
    """
    Parse a response message and return the list of A records whose owner name
    matches `want_name` (case-insensitive). Each entry: (ip, ttl, cache_flush).
    """
    try:
        _id, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
    except struct.error:
        return []
    if not (flags & 0x8000):  # QR bit -- only look at responses, not queries
        return []
    offset = 12
    # Skip the question section.
    for _ in range(qd):
        _n, offset = parse_name(data, offset)
        offset += 4  # qtype + qclass
    results: list[Answer] = []
    total_rr = an + ns + ar
    for _ in range(total_rr):
        if offset >= len(data):
            break
        rname, offset = parse_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, rclass, ttl, rdlength = struct.unpack(">HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + rdlength]
        offset += rdlength
        if rtype == QTYPE_A and rdlength == 4:
            if rname.lower().rstrip(".") == want_name.lower().rstrip("."):
                ip = socket.inet_ntoa(rdata)
                cache_flush = bool(rclass & CACHE_FLUSH_BIT)
                results.append((ip, ttl, cache_flush))
    return results


# --------------------------------------------------------------------------- #
# mDNS query/collect
# --------------------------------------------------------------------------- #
def _enable_pktinfo(sock: socket.socket) -> None:
    """Request IP_PKTINFO so recvmsg() reveals the interface a packet arrived on."""
    if hasattr(socket, "IP_PKTINFO"):
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_PKTINFO, 1)
        except OSError as exc:
            logger.debug("IP_PKTINFO not available: %s" % exc)


def open_socket() -> tuple[socket.socket, bool]:
    """
    Try to bind :5353 (captures ALL multicast responses, incl. proxies). Fall
    back to an ephemeral port (QU / unicast replies only) if :5353 is
    unavailable. Group membership and the send interface(s) are set up per
    interface by :func:`send_query`. Returns (sock, listen_on_5353).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    _enable_pktinfo(sock)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    try:
        sock.bind(("", MCAST_PORT))
        return sock, True
    except OSError:
        # :5353 busy in an incompatible way -> ephemeral + request unicast reply.
        sock.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        _enable_pktinfo(sock)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.bind(("", 0))
        return sock, False


def _send_targets(iface_ip: str | None) -> list[tuple[str, bytes, bytes]]:
    """Build the per-interface socket-option payloads to join on and send out of.

    Each entry is ``(label, join_opt, multicast_if_opt)``:

      - with `iface_ip` pinned: a single ``ip_mreq`` / ``in_addr`` addressing that IP;
      - otherwise: one ``ip_mreqn`` per local interface (by index, from
        :func:`socket.if_nameindex`, loopback excluded), so a multi-homed host is
        covered on every subnet regardless of the default route.
    """
    if iface_ip:
        return [
            (
                iface_ip,
                socket.inet_aton(MCAST_GRP) + socket.inet_aton(iface_ip),  # ip_mreq
                socket.inet_aton(iface_ip),  # in_addr
            )
        ]
    targets: list[tuple[str, bytes, bytes]] = []
    for idx, name in socket.if_nameindex():
        if name == "lo":  # loopback only yields the local avahi echo -> skip
            continue
        packed_idx = struct.pack("@i", idx)
        targets.append(
            (
                name,
                socket.inet_aton(MCAST_GRP) + socket.inet_aton("0.0.0.0") + packed_idx,  # ip_mreqn
                socket.inet_aton("0.0.0.0") * 2 + packed_idx,  # ip_mreqn (ifindex only)
            )
        )
    return targets


def send_query(sock: socket.socket, packet: bytes, iface_ip: str | None, join: bool) -> list[str]:
    """Send `packet` to the mDNS group out of every target interface.

    Joins the multicast group per interface first when `join` (i.e. we bound
    :5353 and listen for multicast replies). Interfaces that cannot join/send
    (down, IPv4-less) are skipped. Returns the labels actually used.
    """
    sent_on: list[str] = []
    for label, join_opt, if_opt in _send_targets(iface_ip):
        try:
            if join:
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, join_opt)
                except OSError as exc:
                    # Already joined / no IPv4 / down -> still try to send out of it.
                    logger.debug("group join on %s failed: %s" % (label, exc))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, if_opt)
            sock.sendto(packet, (MCAST_GRP, MCAST_PORT))
            sent_on.append(label)
        except OSError as exc:
            logger.debug("send on %s failed: %s" % (label, exc))
    return sent_on


def _recv(sock: socket.socket, if_names: dict[int, str]) -> tuple[bytes, str, str]:
    """Receive one datagram, returning (data, src_ip, recv_iface).

    Uses recvmsg() to read the IP_PKTINFO ancillary datum (the index of the
    interface the packet arrived on), falling back to ``"?"`` when unavailable.
    """
    if hasattr(socket, "IP_PKTINFO"):
        data, ancdata, _flags, addr = sock.recvmsg(9000, socket.CMSG_SPACE(12))
        recv_iface = "?"
        for level, msg_type, cmsg_data in ancdata:
            if level == socket.IPPROTO_IP and msg_type == socket.IP_PKTINFO:
                (ifindex,) = struct.unpack("@i", cmsg_data[:4])  # in_pktinfo.ipi_ifindex
                recv_iface = if_names.get(ifindex, str(ifindex))
        return data, addr[0], recv_iface
    data, addr = sock.recvfrom(9000)
    return data, addr[0], "?"


def query_mdns(name: str, timeout: float, iface_ip: str | None) -> tuple[dict[str, list[Record]], bool, str]:
    """Send the mDNS query and collect every A-record answer, keyed by responder IP.

    With `iface_ip` the query goes out of that interface only; without it, out of
    every local interface. Each answer is tagged with the interface its response
    packet arrived on. Returns ``(responders, listen_on_5353, scope)`` where
    `scope` describes the interface(s) used, for the report header.
    """
    sock, listen_5353 = open_socket()
    # If we could not grab :5353 we depend on unicast replies -> set the QU bit.
    _h, _q, packet = build_query(name, want_unicast=not listen_5353)
    sent_on = send_query(sock, packet, iface_ip, join=listen_5353)
    if not sent_on:
        logger.warning("could not send the query on any interface")

    if_names = {idx: iname for idx, iname in socket.if_nameindex()}
    responders: dict[str, list[Record]] = {}  # src_ip -> list of (answer_ip, ttl, cache_flush, recv_iface)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        try:
            data, src_ip, recv_iface = _recv(sock, if_names)
        except socket.timeout:
            break
        except OSError:
            break
        answers = parse_a_answers(data, name)
        if answers:
            recs = responders.setdefault(src_ip, [])
            for ip, ttl, cf in answers:
                rec: Record = (ip, ttl, cf, recv_iface)
                if rec not in recs:
                    recs.append(rec)
    sock.close()
    scope = iface_ip if iface_ip else "all interfaces (%s)" % ", ".join(sent_on)
    return responders, listen_5353, scope


# --------------------------------------------------------------------------- #
# unicast cross-check (surfaces router DHCP-lease disagreements)
# --------------------------------------------------------------------------- #
def unicast_lookup(host: str) -> list[str]:
    """Resolve `host` via the system resolver, returning the sorted IPv4 addresses."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        ips = sorted({str(i[4][0]) for i in infos})
        return ips
    except OSError as exc:
        return ["<%s>" % (exc.strerror or exc)]


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def report(name: str, responders: dict[str, list[Record]], listen_5353: bool, timeout: float, scope: str) -> None:
    """Log the responder table and the stale/cache verdict for `name`."""
    transport = "multicast-listen (:5353)" if listen_5353 else "unicast-reply (ephemeral)"
    logger.info("mDNS query for %s   [%s via %s, timeout %.1fs]" % (name, transport, scope, timeout))
    logger.info("=" * 68)

    if not responders:
        logger.warning("  no mDNS responder answered.")
        return

    all_answer_ips: set[str] = set()
    for src_ip in sorted(responders):
        for ip, ttl, cf, recv_iface in responders[src_ip]:
            all_answer_ips.add(ip)
            match = ip == src_ip
            tag = "self (authoritative)" if match else "PROXY/CACHE (src != answer!)"
            flush = "cache-flush" if cf else "shared"
            logger.info(
                "  responder %-15s (in via %-10s) ->  A %-15s  ttl=%-6d  %-11s  %s"
                % (src_ip, recv_iface, ip, ttl, flush, tag)
            )

    logger.info("-" * 68)
    # Verdict.
    problems: list[str] = []
    if len(responders) > 1:
        problems.append("MULTIPLE responders (%d) -> conflicting/cached answerer" % len(responders))
    mismatched = [s for s in responders for (ip, _t, _c, _if) in responders[s] if ip != s]
    if mismatched:
        problems.append("source-IP != advertised-IP -> proxy/cache/reflected reply")
    if len(all_answer_ips) > 1:
        problems.append("DISAGREEING answers: %s" % ", ".join(sorted(all_answer_ips)))
    if problems:
        logger.warning("  VERDICT: suspicious")
        for p in problems:
            logger.warning("     - " + p)
    else:
        only_ip = next(iter(all_answer_ips))
        logger.info(
            "  VERDICT: clean -- single authoritative host %s answered with %s" % (next(iter(responders)), only_ip)
        )


def report_unicast(name: str) -> None:
    """Log the unicast cross-check that surfaces router DHCP-lease disagreements."""
    bare = name[: -len(".local")] if name.endswith(".local") else name
    logger.info("")
    logger.info("unicast cross-check (system resolver)")
    logger.info("=" * 68)
    for h in (bare, bare + ".fritz.box", bare + ".local"):
        logger.info("  %-24s -> %s" % (h, ", ".join(unicast_lookup(h))))
    logger.info("-" * 68)
    logger.info("  (a .fritz.box / bare answer that differs from the mDNS IP above is")
    logger.info("   a router DHCP-lease -- e.g. a stale boot-time lease -- not the host.)")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the mDNS query and log the who-answered report."""
    ap = argparse.ArgumentParser(
        description="Resolve a .local name via mDNS and show which host answered "
        "(and whether it smells like a stale/cache reply)."
    )
    ap.add_argument("name", help="hostname to resolve (bare name -> .local is appended)")
    ap.add_argument("--timeout", type=float, default=2.0, help="seconds to collect responses (default: 2.0)")
    ap.add_argument(
        "--iface",
        metavar="IP",
        default=None,
        help="pin the query to this local interface IP (default: send out of every interface)",
    )
    ap.add_argument(
        "--unicast",
        action="store_true",
        help="also do a unicast lookup of the bare name and <name>.fritz.box to surface a router DHCP-lease mismatch",
    )
    ap.add_argument("--verbose", "-v", action="store_true", help="enable DEBUG logging")
    args = ap.parse_args(argv)

    configure_logging(verbose=args.verbose)
    print_banner("mdns_who")

    name: str = args.name
    if "." not in name:
        name = name + ".local"
    if not name.endswith(".local") and not args.unicast:
        logger.warning("note: %r is not a .local name; mDNS may return nothing." % name)

    responders, listen_5353, scope = query_mdns(name, args.timeout, args.iface)
    report(name, responders, listen_5353, args.timeout, scope)

    if args.unicast:
        report_unicast(name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
