# dhcpstuff

DHCP diagnostic and discovery tools.

## dhcp_discover.py

Sends a DHCP Discover broadcast and displays all responses, including regular DHCP offers and Proxy DHCP responses (e.g. from PXE boot servers).

**Requires root privileges** (binds to UDP port 68).

### Usage

```bash
sudo python3 dhcp_discover.py [-i INTERFACE] [-t TIMEOUT] [-m MAC] [-a ARCH]
```

| Flag | Description |
|------|-------------|
| `-i`, `--interface` | Bind to a specific interface (e.g. `eth0`) |
| `-t`, `--timeout` | Seconds to wait for responses (default: 5) |
| `-m`, `--mac` | MAC address to use (default: random locally-administered MAC) |
| `-a`, `--arch` | PXE client architecture: `bios`, `efi64`, or `efi64-http` |

### Examples

```bash
# Basic discover on all interfaces
sudo python3 dhcp_discover.py

# Discover on a specific interface with PXE EFI64 architecture
sudo python3 dhcp_discover.py -i eth0 -a efi64

# Custom MAC and longer timeout
sudo python3 dhcp_discover.py -m 02:aa:bb:cc:dd:ee -t 10
```

The output shows each DHCP Offer with the offered IP, server ID, boot file, and all parsed DHCP options.

## diagnose-dhcp.sh

Comprehensive diagnostic script for finding unwanted DHCP on physical interfaces of an Ubuntu Server (24.04+). Written in German.

**Requires root privileges.**

### Usage

```bash
sudo bash diagnose-dhcp.sh
```

### What it checks

1. **System overview** &mdash; lists physical (non-virtual) interfaces and their DHCP state
2. **cloud-init** &mdash; whether cloud-init is installed/active and generating network configs
3. **Netplan** &mdash; scans all netplan YAML sources for `dhcp4: true`
4. **systemd-networkd** &mdash; checks `.network` files for DHCP settings
5. **NetworkManager** &mdash; checks active NM connections for `ipv4.method auto`
6. **DHCP client processes** &mdash; detects running `dhclient`, `dhcpcd`, `udhcpc`
7. **DHCP leases** &mdash; inspects lease files in `/run/systemd/netif/leases`, `/var/lib/dhcp`, etc.
8. **Kernel cmdline** &mdash; checks for `ip=dhcp` in boot parameters and initramfs hooks
9. **udev rules & hooks** &mdash; scans networkd-dispatcher and if-up hooks for DHCP client calls
10. **networkctl summary** &mdash; shows `networkctl status` for each physical interface

At the end it prints a summary of all findings with concrete remediation steps.
