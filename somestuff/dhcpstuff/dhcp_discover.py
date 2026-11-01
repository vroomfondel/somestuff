#!/usr/bin/env python3
"""Send DHCP Discover and display all responses (DHCP + Proxy DHCP)."""

import argparse
import random
import socket
import struct
import time

DHCP_SERVER_PORT = 67
DHCP_CLIENT_PORT = 68
MAGIC_COOKIE = b"\x63\x82\x53\x63"

DHCP_MSG_TYPES = {
    1: "DISCOVER",
    2: "OFFER",
    3: "REQUEST",
    4: "DECLINE",
    5: "ACK",
    6: "NAK",
    7: "RELEASE",
    8: "INFORM",
}

DHCP_OPTIONS = {
    1: "Subnet Mask",
    3: "Router",
    6: "DNS Server",
    12: "Hostname",
    15: "Domain Name",
    28: "Broadcast Address",
    43: "Vendor Specific",
    50: "Requested IP",
    51: "Lease Time",
    53: "DHCP Message Type",
    54: "DHCP Server ID",
    58: "Renewal Time",
    59: "Rebinding Time",
    60: "Vendor Class ID",
    61: "Client ID",
    66: "TFTP Server Name",
    67: "Bootfile Name",
    93: "Client System Architecture",
    97: "UUID/GUID",
    175: "iPXE Encapsulated",
    255: "End",
}


def mac_bytes(mac_str: str) -> bytes:
    return bytes(int(b, 16) for b in mac_str.split(":"))


def fmt_ip(raw: bytes) -> str:
    return ".".join(str(b) for b in raw)


def fmt_mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


PXE_ARCH_TYPES = {
    "bios": (0, "PXEClient:Arch:00000:UNDI:002001"),
    "efi64": (7, "PXEClient:Arch:00007:UNDI:003016"),
    "efi64-http": (9, "PXEClient:Arch:00009:UNDI:003016"),
}


def build_discover(mac: bytes, xid: int, arch: int | None = None, vendor_class: str | None = None) -> bytes:
    """Build a DHCPDISCOVER packet."""
    pkt = struct.pack("!BBBB", 1, 1, 6, 0)  # op, htype, hlen, hops
    pkt += struct.pack("!I", xid)  # xid
    pkt += struct.pack("!HH", 0, 0x8000)  # secs, flags (broadcast)
    pkt += b"\x00" * 4 * 4  # ciaddr, yiaddr, siaddr, giaddr
    pkt += mac + b"\x00" * 10  # chaddr (16 bytes)
    pkt += b"\x00" * 64  # sname
    pkt += b"\x00" * 128  # file
    pkt += MAGIC_COOKIE
    # Options
    pkt += b"\x35\x01\x01"  # 53: DHCPDISCOVER
    pkt += b"\x37\x08\x01\x03\x06\x0f\x1c\x42\x43\xaf"  # 55: param request list
    if arch is not None:
        pkt += struct.pack("!BBH", 93, 2, arch)  # 93: Client System Architecture
    if vendor_class is not None:
        vc = vendor_class.encode("ascii")
        pkt += struct.pack("!BB", 60, len(vc)) + vc  # 60: Vendor Class Identifier
    pkt += b"\xff"  # end
    return pkt


def parse_options(data: bytes) -> dict:
    """Parse DHCP options from raw bytes."""
    opts = {}
    i = 0
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        length = data[i + 1]
        value = data[i + 2 : i + 2 + length]
        opts[code] = value
        i += 2 + length
    return opts


def format_option(code: int, value: bytes) -> str:
    """Format a DHCP option value for display."""
    if code == 53:
        return DHCP_MSG_TYPES.get(value[0], f"Unknown({value[0]})")
    if code in (1, 3, 6, 28, 50, 54):
        ips = [fmt_ip(value[j : j + 4]) for j in range(0, len(value), 4)]
        return ", ".join(ips)
    if code in (51, 58, 59):
        return f"{struct.unpack('!I', value)[0]}s"
    if code in (12, 15, 60, 66, 67):
        return value.decode("ascii", errors="replace")
    return value.hex()


def parse_response(data: bytes) -> dict | None:
    """Parse a DHCP response packet."""
    if len(data) < 240:
        return None
    if data[236:240] != MAGIC_COOKIE:
        return None

    info = {
        "op": data[0],
        "yiaddr": fmt_ip(data[16:20]),
        "siaddr": fmt_ip(data[20:24]),
        "giaddr": fmt_ip(data[24:28]),
        "chaddr": fmt_mac(data[28:34]),
        "sname": data[44:108].rstrip(b"\x00").decode("ascii", errors="replace"),
        "file": data[108:236].rstrip(b"\x00").decode("ascii", errors="replace"),
        "options": parse_options(data[240:]),
    }
    return info


def print_response(info: dict, idx: int) -> None:
    """Print a parsed DHCP response."""
    msg_type_raw = info["options"].get(53, b"\x00")[0]
    msg_type = DHCP_MSG_TYPES.get(msg_type_raw, f"Unknown({msg_type_raw})")
    server_id = fmt_ip(info["options"][54]) if 54 in info["options"] else "?"
    is_proxy = info["yiaddr"] == "0.0.0.0"

    label = f"Proxy DHCP {msg_type}" if is_proxy else f"DHCP {msg_type}"
    print(f"\n{'='*60}")
    print(f"Response #{idx}: {label} from {server_id}")
    print(f"{'='*60}")

    if not is_proxy:
        print(f"  Offered IP:    {info['yiaddr']}")
    if info["siaddr"] != "0.0.0.0":
        print(f"  Next Server:   {info['siaddr']}")
    if info["sname"]:
        print(f"  Server Name:   {info['sname']}")
    if info["file"]:
        print(f"  Boot File:     {info['file']}")

    print(f"  Options:")
    for code, value in sorted(info["options"].items()):
        name = DHCP_OPTIONS.get(code, f"Option {code}")
        print(f"    {name} ({code}): {format_option(code, value)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="DHCP Discover - show DHCP and Proxy DHCP responses")
    parser.add_argument("-i", "--interface", help="Bind to specific interface (e.g. eth0)")
    parser.add_argument("-t", "--timeout", type=float, default=5.0, help="Seconds to wait for responses (default: 5)")
    parser.add_argument("-m", "--mac", default=None, help="MAC address to use (default: random)")
    parser.add_argument(
        "-a",
        "--arch",
        choices=list(PXE_ARCH_TYPES.keys()),
        help="PXE client architecture for option 93 (required for Proxy DHCP/PXE boot servers)",
    )
    args = parser.parse_args()

    if args.mac:
        mac = mac_bytes(args.mac)
    else:
        mac = bytes([0x02] + [random.randint(0, 255) for _ in range(5)])
        print(f"Using random MAC: {fmt_mac(mac)}")

    xid = random.randint(1, 0xFFFFFFFF)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if args.interface:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, args.interface.encode() + b"\0")
    sock.bind(("", DHCP_CLIENT_PORT))
    sock.settimeout(1.0)

    if args.arch:
        arch_code, vendor_class = PXE_ARCH_TYPES[args.arch]
    else:
        arch_code, vendor_class = None, None
    pkt = build_discover(mac, xid, arch=arch_code, vendor_class=vendor_class)
    sock.sendto(pkt, ("255.255.255.255", DHCP_SERVER_PORT))
    arch_info = f", arch={args.arch}({arch_code}), vendor={vendor_class}" if arch_code is not None else ""
    print(f"Sent DHCPDISCOVER (xid=0x{xid:08x}{arch_info}), waiting {args.timeout}s for responses...")

    responses = []
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        info = parse_response(data)
        if info and struct.unpack("!I", struct.pack("!I", xid))[0] == struct.unpack("!I", data[4:8])[0]:
            responses.append((addr, info))
            print_response(info, len(responses))

    sock.close()

    if not responses:
        print("\nNo responses received.")
    else:
        print(f"\n{len(responses)} response(s) received.")


if __name__ == "__main__":
    main()
