# dhcpstuff

DHCP Discover sender and response analyzer for network diagnostics: broadcasts a
DHCPDISCOVER and prints every answer, including regular DHCP offers and Proxy DHCP
responses from PXE boot servers. Standard library only.

## Modules

- **`dhcp_discover.py`** — builds a DHCPDISCOVER packet, broadcasts it to
  `255.255.255.255:67` from UDP port 68 and collects replies for the given timeout.
  Only replies matching the transaction ID are shown. Each response is labelled
  `DHCP <type>` or, if the offered address (`yiaddr`) is `0.0.0.0`, `Proxy DHCP <type>`,
  followed by offered IP, next server, server name, boot file and all parsed options
  with human-readable names. With `--arch` the Discover carries PXE client
  architecture (option 93) and a matching `PXEClient` vendor class (option 60),
  which Proxy DHCP / PXE boot servers require before they answer.
- **`__main__.py`** — makes `python3 -m somestuff.dhcpstuff` run the same CLI.

## Installation

```bash
pip install somestuff-dhcpstuff
```

`somestuff` is a PEP 420 namespace; the package is imported as
`somestuff.dhcpstuff` and shares the namespace with the other `somestuff-*`
distributions.

## Usage

Console script and module invocation are equivalent:

```bash
sudo dhcp-discover
sudo python3 -m somestuff.dhcpstuff.dhcp_discover
sudo python3 -m somestuff.dhcpstuff
```

| Flag                | Description                                                            |
|---------------------|------------------------------------------------------------------------|
| `-i`, `--interface` | Bind to a specific interface (e.g. `eth0`)                             |
| `-t`, `--timeout`   | Seconds to wait for responses (default: 5)                             |
| `-m`, `--mac`       | MAC address to use (default: random locally-administered MAC)          |
| `-a`, `--arch`      | PXE client architecture for option 93: `bios`, `efi64` or `efi64-http` |

Examples:

```bash
# Basic discover on all interfaces
sudo dhcp-discover

# Specific interface, PXE EFI64 architecture
sudo dhcp-discover -i eth0 -a efi64

# Custom MAC and longer timeout
sudo dhcp-discover -m 02:aa:bb:cc:dd:ee -t 10
```

### Library

```python
from somestuff.dhcpstuff import build_discover, mac_bytes, parse_response

packet = build_discover(mac_bytes("02:aa:bb:cc:dd:ee"), xid=0x12345678)
```

The package also exports `parse_options`, `print_response`, `format_option`,
`fmt_ip`, `fmt_mac`, `main` and the constants `DHCP_SERVER_PORT`,
`DHCP_CLIENT_PORT`, `MAGIC_COOKIE`, `DHCP_MSG_TYPES`, `DHCP_OPTIONS` and
`PXE_ARCH_TYPES`.

## Privileges

`dhcp-discover` binds UDP port 68 and needs root (or `CAP_NET_BIND_SERVICE`). It
will not work while another DHCP client holds the port exclusively.

## Notes

- The repository additionally contains `diagnose-dhcp.sh`, a root-only (German)
  shell script for finding unwanted DHCP on physical interfaces of Ubuntu Server
  (cloud-init, Netplan, systemd-networkd, NetworkManager, DHCP client processes,
  lease files, `ip=dhcp` kernel cmdline, udev/dispatcher hooks). It is not part
  of the installed package:
  https://github.com/vroomfondel/somestuff/tree/main/somestuff/dhcpstuff/
- Requires: no third-party dependencies. Linux only (`SO_BINDTODEVICE`).
- Licensed under the LGPL, see `LICENSE.md` in the repository root.
