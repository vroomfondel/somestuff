# dnsstuff

DNS and network diagnostics for mail relays and LANs: an SPF crawler that feeds a
Linux `ipset`, a Microsoft 365 21Vianet IP-range updater, and an mDNS resolver that
shows which host actually answered.

## Modules

- **`spf_ipset_updater.py`** — recursively resolves the SPF records of the given
  domains (`include:`, `a`, `mx`, `ip4`, redirects), collects every IPv4 address and
  network and atomically swaps the result into a kernel `ipset` via netlink
  (`pyroute2`). Meant for SMTP allowlists on relay hosts.
- **`update_21vianet_ips.py`** — polls the Microsoft 365 endpoint API for the
  21Vianet (China) Exchange Online ranges, caches the version and updates two
  ipsets: all Exchange IPv4 networks and the SMTP-only (port 25) subset.
- **`mdns_who.py`** — sends an mDNS query for a `.local` name itself and reports
  every responder: source IP, advertised A records, TTL and cache-flush bit.
  Flags proxied, cached or reflected replies (source IP ≠ advertised IP, more
  than one responder). `--unicast` adds a system-resolver lookup to surface a
  stale router DHCP lease.

Logging (loguru, with an intercept for stdlib `logging` from `dnspython`,
`pyroute2`, `urllib`) and the startup banner are shared via `somestuff.dnsstuff`.

## Installation

```bash
pip install somestuff-dnsstuff
```

`somestuff` is a PEP 420 namespace; the package is imported as
`somestuff.dnsstuff` and shares the namespace with the other `somestuff-*`
distributions.

## Usage

Console scripts and module invocation are equivalent:

```bash
spf-ipset-updater example.com mail.example.net --ipset-name smtpallowlist
python3 -m somestuff.dnsstuff.spf_ipset_updater example.com --dry-run -v

update-21vianet-ips --ipset-name-all 21vianet_exchange --ipset-name-smtp 21vianet_smtp
python3 -m somestuff.dnsstuff.update_21vianet_ips --force --dry-run

mdns-who printer.local
python3 -m somestuff.dnsstuff.mdns_who printer --timeout 3 --unicast --iface 192.168.1.5
```

Common flags: `--dry-run` populates the ipset but skips the atomic swap,
`--no-comment` disables the ipset comment extension, `-v` enables DEBUG logging.
`update-21vianet-ips --help` lists the cache directory and API timeout options.

Cron example for the 21Vianet updater (daily at 03:00):

```
0 3 * * * update-21vianet-ips 2>&1 | logger -t 21vianet
```

### Library

```python
from somestuff.dnsstuff import configure_logging
from somestuff.dnsstuff.spf_ipset_updater import get_spf_records

configure_logging(verbose=True)
```

## Privileges

- `spf-ipset-updater` and `update-21vianet-ips` modify kernel ipsets over netlink
  and need `CAP_NET_ADMIN` (in practice: root).
- `mdns-who` binds a UDP socket on port 5353 and joins the mDNS multicast group;
  on most systems this works unprivileged unless another mDNS daemon holds the
  port exclusively.

## Notes

- Requires: `dnspython`, `pyroute2`, `loguru`, `tabulate`. Linux only (netlink ipsets).
- Licensed under the LGPL, see `LICENSE.md` in the repository root.
