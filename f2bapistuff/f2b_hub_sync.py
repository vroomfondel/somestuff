"""Reconcile the hub's ban jail with this fail2ban and rotate fail2ban's log; runs beside fail2ban-server.

The hub has one jail per cluster, the cluster several (dovecot, recidive, ...). Bans go to the hub at once (action
hubapi); unbans must not, or the end of a 3-day dovecot ban would lift an IP that recidive still holds for 7 days.
So actionunban only touches TRIGGER, and this loop posts the union of all locally banned addresses
(POST /f2b/v1/state) within POLL seconds of it, and every F2B_SYNC_INTERVAL seconds anyway, which also heals bans
the hub missed (hub restart, tunnel gap). Once a day fail2ban.log is renamed to fail2ban.log.1 and reopened
(recidive reads it).
``f2b_hub_sync.py once`` pushes the list a single time and exits (manual re-seed of the hub).
"""

import ast
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SECRET_DIR = Path("/etc/f2b-hub")
LOG_FILE = Path(os.environ.get("F2B_LOGTARGET", "/var/log/mail/fail2ban.log"))
INTERVAL = int(os.environ.get("F2B_SYNC_INTERVAL", "300"))
TRIGGER = Path(os.environ.get("F2B_SYNC_TRIGGER", "/run/f2b-hub-resync"))
POLL = 5
ROTATE_EVERY = 86400
STARTUP_DELAY = 30

log = logging.getLogger("f2b-hub-sync")


def banned_ips() -> list[str]:
    """Collect the addresses banned in any local jail.

    Returns:
        The sorted union over all jails.

    Raises:
        subprocess.SubprocessError: When fail2ban-client fails or hangs.
        ValueError: When its output is not the expected list of ``{jail: [ip, ...]}`` mappings.
    """
    out = subprocess.run(["fail2ban-client", "banned"], check=True, capture_output=True, text=True, timeout=30).stdout
    try:
        parsed: object = ast.literal_eval(out.strip() or "[]")
    except SyntaxError as e:
        raise ValueError(f"unparsable fail2ban-client output: {out!r}") from e
    if not isinstance(parsed, list):
        raise ValueError(f"unexpected fail2ban-client output: {parsed!r}")
    ips: set[str] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            raise ValueError(f"unexpected fail2ban-client entry: {entry!r}")
        for jail_ips in entry.values():
            if not isinstance(jail_ips, list):
                raise ValueError(f"unexpected fail2ban-client entry: {entry!r}")
            ips.update(str(ip) for ip in jail_ips)
    return sorted(ips)


def push_state(ips: list[str]) -> dict[str, object]:
    """Post the complete ban list to the hub.

    Args:
        ips: Every address that must be banned on the hub.

    Returns:
        The API's answer (``banned``, ``unbanned``, ``rejected``).

    Raises:
        urllib.error.URLError: When the hub is unreachable or answers with an error.
    """
    url = (SECRET_DIR / "url").read_text().strip().rstrip("/") + "/f2b/v1/state"
    token = (SECRET_DIR / "token").read_text().strip()
    req = urllib.request.Request(
        url,
        data=json.dumps({"ips": ips}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        answer: dict[str, object] = json.loads(resp.read())
    return answer


def rotate_log() -> None:
    """Keep one previous day of fail2ban.log and let fail2ban reopen its log target."""
    if LOG_FILE.exists():
        LOG_FILE.rename(LOG_FILE.with_name(LOG_FILE.name + ".1"))
    subprocess.run(["fail2ban-client", "flushlogs"], check=True, capture_output=True, timeout=30)


def sync(last_rejected: dict[str, object]) -> dict[str, object]:
    """Reconcile the hub once and log what changed.

    Args:
        last_rejected: The ``rejected`` map of the previous round; unchanged rejections are not logged again.

    Returns:
        This round's ``rejected`` map.

    Raises:
        OSError: When the hub is unreachable or answers with an error.
        subprocess.SubprocessError: When fail2ban-client fails.
        ValueError: When fail2ban-client's output is unexpected.
    """
    answer = push_state(banned_ips())
    rejected = answer.get("rejected")
    rejected_map: dict[str, object] = rejected if isinstance(rejected, dict) else {}
    if answer.get("banned") or answer.get("unbanned") or rejected_map != last_rejected:
        log.info("hub reconciled: %s", answer)
    return rejected_map


def main() -> None:
    """Loop forever, or push once with argument ``once``; every loop error is logged and retried next round."""
    logging.basicConfig(level=logging.INFO, format="f2b-hub-sync %(levelname)s %(message)s")
    if sys.argv[1:] == ["once"]:
        log.info("hub reconciled: %s", push_state(banned_ips()))
        return
    time.sleep(STARTUP_DELAY)
    last_rotate = time.monotonic()
    next_sync = 0.0
    rejected: dict[str, object] = {}
    while True:
        if TRIGGER.exists() or time.monotonic() >= next_sync:
            # Removed before reading the bans, so an unban during this round triggers the next one.
            TRIGGER.unlink(missing_ok=True)
            try:
                rejected = sync(rejected)
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                log.warning("sync failed: %s", e)
            next_sync = time.monotonic() + INTERVAL
        if time.monotonic() - last_rotate >= ROTATE_EVERY:
            try:
                rotate_log()
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("log rotation failed: %s", e)
            last_rotate = time.monotonic()
        time.sleep(POLL)


if __name__ == "__main__":
    main()
