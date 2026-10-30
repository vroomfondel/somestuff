"""Unit tests for f2bapistuff/f2b_api.py."""

import ipaddress
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections.abc import Sequence
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "f2bapistuff"))

from f2b_api import ApiError, BanService, Fail2ban, Handler, Policy, read_networks, read_tokens  # noqa: E402

TOKEN = "t" * 40
OTHER_TOKEN = "o" * 40


class FakeFail2banClient:
    """Stands in for ``fail2ban-client``: keeps the banned set per jail and answers like the real one.

    Attributes:
        jails: Jail name to banned addresses.
        calls: Every argv received, without the command prefix.
    """

    def __init__(self) -> None:
        """Start with no bans."""
        self.jails: dict[str, set[str]] = {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> str:
        """Emulate one fail2ban-client call.

        Args:
            argv: ``fail2ban-client set <jail> banip|unbanip <ip>`` or ``get <jail> banned``.

        Returns:
            The output the real client prints.
        """
        args = list(argv[1:])
        self.calls.append(args)
        jail = self.jails.setdefault(args[1], set())
        match args:
            case ["set", _, "banip", ip]:
                new = ip not in jail
                jail.add(ip)
                return "1" if new else "0"
            case ["set", _, "unbanip", ip]:
                old = ip in jail
                jail.discard(ip)
                return "1" if old else "0"
            case ["get", _, "banned"]:
                return repr(sorted(jail))
        raise AssertionError(f"unexpected call {args}")


def make_service() -> tuple[BanService, FakeFail2banClient]:
    """Build a service with two clients and one protected network.

    Returns:
        The service and its fake fail2ban-client.
    """
    fake = FakeFail2banClient()
    policy = Policy({"dgx": TOKEN, "demo": OTHER_TOKEN}, [ipaddress.IPv4Network("198.51.100.0/24")])
    return BanService(policy, Fail2ban(["fail2ban-client"], fake)), fake


class PolicyTest(unittest.TestCase):
    """Tests for :class:`f2b_api.Policy`."""

    def test_client_for(self) -> None:
        """Known bearer tokens resolve to their client; anything else is 401."""
        policy = Policy({"dgx": TOKEN})
        self.assertEqual(policy.client_for(f"Bearer {TOKEN}"), "dgx")
        self.assertEqual(policy.client_for(f"bearer {TOKEN}"), "dgx")
        for header in (None, "", TOKEN, f"Basic {TOKEN}", "Bearer ", f"Bearer {OTHER_TOKEN}"):
            with self.subTest(header=header), self.assertRaises(ApiError) as ctx:
                policy.client_for(header)
            self.assertEqual(ctx.exception.status, 401)

    def test_check_ip(self) -> None:
        """Only global IPv4 outside the protected networks passes, in canonical form."""
        policy = Policy({}, [ipaddress.IPv4Network("198.51.100.0/24")])
        self.assertEqual(policy.check_ip("8.8.8.8"), "8.8.8.8")
        for value in (
            "10.200.2.10",
            "192.168.1.1",
            "127.0.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "2001:db8::1",
            "1.2.3",
            "1.2.3.4/32",
            "",
            None,
            7,
            "198.51.100.20",
        ):
            with self.subTest(value=value), self.assertRaises(ApiError) as ctx:
                policy.check_ip(value)
            self.assertEqual(ctx.exception.status, 422)


class BanServiceTest(unittest.TestCase):
    """Tests for :class:`f2b_api.BanService`."""

    def test_ban_unban_idempotent(self) -> None:
        """Ban/unban report whether they changed anything and touch only the client's jail."""
        service, fake = make_service()
        self.assertTrue(service.ban("dgx", {"ip": "8.8.8.8", "jail": "dovecot"})["changed"])
        self.assertFalse(service.ban("dgx", {"ip": "8.8.8.8"})["changed"])
        self.assertEqual(fake.jails["remote-dgx"], {"8.8.8.8"})
        self.assertNotIn("remote-demo", fake.jails)
        self.assertTrue(service.unban("dgx", {"ip": "8.8.8.8"})["changed"])
        self.assertFalse(service.unban("dgx", {"ip": "8.8.8.8"})["changed"])

    def test_reconcile(self) -> None:
        """Reconcile bans the missing, unbans the extra and reports rejected entries without failing."""
        service, fake = make_service()
        service.ban("dgx", {"ip": "1.1.1.1"})
        service.ban("dgx", {"ip": "9.9.9.9"})
        service.ban("demo", {"ip": "4.4.4.4"})
        out = service.reconcile("dgx", {"ips": ["9.9.9.9", "8.8.8.8", "10.0.0.1", "198.51.100.5", "8.8.8.8"]})
        self.assertEqual(out["banned"], ["8.8.8.8"])
        self.assertEqual(out["unbanned"], ["1.1.1.1"])
        self.assertEqual(sorted(out["rejected"]), ["10.0.0.1", "198.51.100.5"])
        self.assertEqual(fake.jails["remote-dgx"], {"8.8.8.8", "9.9.9.9"})
        self.assertEqual(fake.jails["remote-demo"], {"4.4.4.4"})
        self.assertEqual(service.state("dgx"), {"ips": ["8.8.8.8", "9.9.9.9"]})

    def test_reconcile_rejects_non_list(self) -> None:
        """ips must be a list."""
        service, _ = make_service()
        with self.assertRaises(ApiError):
            service.reconcile("dgx", {"ips": "8.8.8.8"})

    def test_banned_parses_client_output(self) -> None:
        """The client's Python-literal list is parsed; other output is an error."""
        self.assertEqual(Fail2ban([], lambda _: "['203.0.113.9', '8.8.8.8']").banned("j"), {"203.0.113.9", "8.8.8.8"})
        self.assertEqual(Fail2ban([], lambda _: "[]").banned("j"), set())
        with self.assertRaises(ValueError):
            Fail2ban([], lambda _: "{'a': 1}").banned("j")


class ConfigFilesTest(unittest.TestCase):
    """Tests for the config file readers."""

    def test_read_tokens_and_networks(self) -> None:
        """Comments and blank lines are skipped; short tokens and duplicates fail."""
        with tempfile.TemporaryDirectory() as d:
            tokens = Path(d) / "tokens"
            tokens.write_text(f"# clients\ndgx {TOKEN}\n\ndemo {OTHER_TOKEN}  # second\n")
            self.assertEqual(read_tokens(tokens), {"dgx": TOKEN, "demo": OTHER_TOKEN})
            for bad in (f"dgx {TOKEN}\ndgx {OTHER_TOKEN}\n", "dgx short\n", f"dgx {TOKEN} extra\n"):
                tokens.write_text(bad)
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    read_tokens(tokens)
            nets = Path(d) / "protect"
            nets.write_text("90.187.235.233/32 # office\n10.0.0.1\n")
            self.assertEqual(
                read_networks(nets), [ipaddress.IPv4Network("90.187.235.233/32"), ipaddress.IPv4Network("10.0.0.1/32")]
            )


class HttpTest(unittest.TestCase):
    """End-to-end tests through the HTTP server."""

    def setUp(self) -> None:
        """Start the server on a free loopback port."""
        self.service, self.fake = make_service()
        Handler.service = self.service
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/f2b/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        """Stop the server."""
        self.server.shutdown()
        self.server.server_close()

    def call(
        self, method: str, path: str, body: dict[str, Any] | None = None, token: str = TOKEN
    ) -> tuple[int, dict[str, Any]]:
        """Send one request.

        Args:
            method: HTTP method.
            path: Path below /f2b/v1.
            body: JSON body, if any.
            token: Bearer token.

        Returns:
            Status code and parsed JSON response.
        """
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers={"Authorization": f"Bearer {token}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self) -> None:
        """Ban, state, reconcile and unban work end to end."""
        self.assertEqual(
            self.call("POST", "/ban", {"ip": "8.8.8.8", "jail": "dovecot"}), (200, {"ip": "8.8.8.8", "changed": True})
        )
        self.assertEqual(self.call("GET", "/state"), (200, {"ips": ["8.8.8.8"]}))
        status, out = self.call("POST", "/state", {"ips": ["1.1.1.1"]})
        self.assertEqual((status, out["banned"], out["unbanned"]), (200, ["1.1.1.1"], ["8.8.8.8"]))
        self.assertEqual(self.call("POST", "/unban", {"ip": "1.1.1.1"})[0], 200)
        self.assertEqual(self.fake.jails["remote-dgx"], set())

    def test_errors(self) -> None:
        """Bad token, bad route, bad address and broken fail2ban map to distinct statuses."""
        self.assertEqual(self.call("GET", "/state", token="x" * 40)[0], 401)
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("POST", "/ban", {"ip": "10.1.1.1"})[0], 422)
        self.assertEqual(self.call("POST", "/ban", {"ip": "8.8.8.8"} | {"x": 1})[0], 200)
        self.service.fail2ban = Fail2ban([], lambda _: "garbage(")
        self.assertEqual(self.call("GET", "/state")[0], 502)


if __name__ == "__main__":
    unittest.main()
