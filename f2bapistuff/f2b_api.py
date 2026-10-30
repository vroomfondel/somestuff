#!/usr/bin/env python3
"""Ban API for the fail2ban of a customer cluster, served on the hub's WireGuard address.

The cluster's fail2ban decides; this API only mirrors its bans into the hub jail ``remote-<client>``,
which drops the addresses on every TCP port of the hub. Each client authenticates with its own bearer token
and can only touch its own jail. Private, reserved and protected addresses are never banned.

Runs in the image xomoxcc/f2b-api next to a fail2ban-server container that owns the jails and the host
firewall; the two share only fail2ban's socket directory, this container has no capabilities.

Endpoints (JSON):
    POST /f2b/v1/ban     {"ip": "203.0.113.7", "jail": "dovecot"}
    POST /f2b/v1/unban   {"ip": "203.0.113.7"}
    POST /f2b/v1/state   {"ips": ["203.0.113.7", ...]}   reconcile the jail to exactly this set
    GET  /f2b/v1/state
"""

import ast
import hmac
import ipaddress
import json
import logging
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Any, ClassVar

import typer

JAIL_PREFIX = "remote-"
MAX_BODY_BYTES = 1 << 20
MAX_STATE_IPS = 50_000

log = logging.getLogger("f2b-api")

Runner = Callable[[Sequence[str]], str]


class ApiError(Exception):
    """Request error that maps to an HTTP status.

    Attributes:
        status: HTTP status sent to the client.
        message: Human-readable reason, sent as ``{"error": message}``.
    """

    def __init__(self, status: HTTPStatus, message: str) -> None:
        """Create the error.

        Args:
            status: HTTP status sent to the client.
            message: Human-readable reason.
        """
        super().__init__(message)
        self.status = status
        self.message = message


def run_command(argv: Sequence[str]) -> str:
    """Run a command and return its stdout.

    Args:
        argv: Command and arguments, executed without a shell.

    Returns:
        The command's stdout, stripped.

    Raises:
        subprocess.CalledProcessError: When the command exits non-zero.
        subprocess.TimeoutExpired: When it runs longer than 30 seconds.
    """
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=30).stdout.strip()


@dataclass
class Fail2ban:
    """Thin wrapper around ``fail2ban-client`` for one kind of jail.

    Attributes:
        command: Prefix for every call, e.g. ``["/usr/bin/fail2ban-client"]``.
        runner: Executes a command and returns its stdout (replaced in tests).
    """

    command: list[str]
    runner: Runner = run_command

    def ban(self, jail: str, ip: str) -> bool:
        """Ban an address in a jail.

        Args:
            jail: Jail name.
            ip: Validated IPv4 address.

        Returns:
            True when the address was not banned before.
        """
        return self.runner([*self.command, "set", jail, "banip", ip]) != "0"

    def unban(self, jail: str, ip: str) -> bool:
        """Unban an address in a jail.

        Args:
            jail: Jail name.
            ip: Validated IPv4 address.

        Returns:
            True when the address was banned before.
        """
        return self.runner([*self.command, "set", jail, "unbanip", ip]) != "0"

    def banned(self, jail: str) -> set[str]:
        """List the addresses currently banned in a jail.

        Args:
            jail: Jail name.

        Returns:
            The banned addresses.

        Raises:
            ValueError: When fail2ban-client answers with something other than a list of strings.
        """
        output = self.runner([*self.command, "get", jail, "banned"]) or "[]"
        try:
            parsed: object = ast.literal_eval(output)
        except SyntaxError as e:
            raise ValueError(f"unparsable fail2ban-client output for {jail}: {output!r}") from e
        if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
            raise ValueError(f"unexpected fail2ban-client output for {jail}: {parsed!r}")
        return set(parsed)


@dataclass
class Policy:
    """Who may call the API and which addresses may be banned.

    Attributes:
        tokens: Client name to bearer token.
        protected: Networks that are never banned (own addresses, ops sources).
    """

    tokens: dict[str, str]
    protected: list[ipaddress.IPv4Network] = field(default_factory=list)

    def client_for(self, authorization: str | None) -> str:
        """Resolve the client of an ``Authorization: Bearer`` header.

        Args:
            authorization: The raw header value.

        Returns:
            The client name.

        Raises:
            ApiError: 401 when the header is missing or the token is unknown.
        """
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() == "bearer" and token:
            for name, expected in self.tokens.items():
                if hmac.compare_digest(token.encode(), expected.encode()):
                    return name
        raise ApiError(HTTPStatus.UNAUTHORIZED, "missing or unknown bearer token")

    def check_ip(self, value: object) -> str:
        """Validate an address before it reaches fail2ban.

        Args:
            value: The ``ip`` field of a request.

        Returns:
            The address in canonical form.

        Raises:
            ApiError: 422 for anything but a global IPv4 address outside the protected networks.
        """
        if not isinstance(value, str):
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "ip must be a string")
        try:
            ip = ipaddress.IPv4Address(value)
        except ValueError as e:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, f"not an IPv4 address: {value!r}") from e
        if not ip.is_global or ip.is_multicast:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, f"{ip} is not a global address")
        if any(ip in net for net in self.protected):
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, f"{ip} is protected")
        return str(ip)


@dataclass
class BanService:
    """The API operations, independent of HTTP.

    Attributes:
        policy: Authentication and address rules.
        fail2ban: Access to the hub jails.
        lock: Serializes fail2ban calls; the reconcile diff must not interleave with single bans.
    """

    policy: Policy
    fail2ban: Fail2ban
    lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def jail(client: str) -> str:
        """Return the hub jail of a client.

        Args:
            client: Client name.

        Returns:
            The jail name.
        """
        return f"{JAIL_PREFIX}{client}"

    def ban(self, client: str, body: dict[str, Any]) -> dict[str, Any]:
        """Ban one address for a client.

        Args:
            client: Authenticated client name.
            body: Request body with ``ip`` and optionally the cluster jail name ``jail``.

        Returns:
            ``{"ip": ..., "changed": bool}``.
        """
        ip = self.policy.check_ip(body.get("ip"))
        with self.lock:
            changed = self.fail2ban.ban(self.jail(client), ip)
        log.info("ban %s client=%s source_jail=%s changed=%s", ip, client, body.get("jail", "-"), changed)
        return {"ip": ip, "changed": changed}

    def unban(self, client: str, body: dict[str, Any]) -> dict[str, Any]:
        """Unban one address for a client.

        Args:
            client: Authenticated client name.
            body: Request body with ``ip``.

        Returns:
            ``{"ip": ..., "changed": bool}``.
        """
        ip = self.policy.check_ip(body.get("ip"))
        with self.lock:
            changed = self.fail2ban.unban(self.jail(client), ip)
        log.info("unban %s client=%s changed=%s", ip, client, changed)
        return {"ip": ip, "changed": changed}

    def state(self, client: str) -> dict[str, Any]:
        """Return the addresses banned for a client.

        Args:
            client: Authenticated client name.

        Returns:
            ``{"ips": [...]}``, sorted.
        """
        with self.lock:
            ips = self.fail2ban.banned(self.jail(client))
        return {"ips": sorted(ips, key=ipaddress.IPv4Address)}

    def reconcile(self, client: str, body: dict[str, Any]) -> dict[str, Any]:
        """Make the client's jail hold exactly the given addresses.

        Invalid or protected entries are skipped and reported instead of failing the whole request, so one bad
        address in the cluster cannot keep every other ban from being applied.

        Args:
            client: Authenticated client name.
            body: Request body with ``ips``.

        Returns:
            ``{"banned": [...], "unbanned": [...], "rejected": {ip: reason}}``.

        Raises:
            ApiError: 422 when ``ips`` is not a list or too long.
        """
        raw = body.get("ips")
        if not isinstance(raw, list) or len(raw) > MAX_STATE_IPS:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, f"ips must be a list of at most {MAX_STATE_IPS} addresses")
        desired: set[str] = set()
        rejected: dict[str, str] = {}
        for value in raw:
            try:
                desired.add(self.policy.check_ip(value))
            except ApiError as e:
                rejected[str(value)] = e.message
        jail = self.jail(client)
        with self.lock:
            current = self.fail2ban.banned(jail)
            to_ban = sorted(desired - current, key=ipaddress.IPv4Address)
            to_unban = sorted(current - desired, key=ipaddress.IPv4Address)
            for ip in to_ban:
                self.fail2ban.ban(jail, ip)
            for ip in to_unban:
                self.fail2ban.unban(jail, ip)
        if to_ban or to_unban or rejected:
            log.info("reconcile client=%s banned=%s unbanned=%s rejected=%s", client, to_ban, to_unban, rejected)
        return {"banned": to_ban, "unbanned": to_unban, "rejected": rejected}


class Handler(BaseHTTPRequestHandler):
    """HTTP front of :class:`BanService`."""

    service: ClassVar[BanService]
    server_version = "f2b-api"

    def log_message(self, format: str, *args: Any) -> None:
        """Route the access log through :mod:`logging`.

        Args:
            format: printf-style format from BaseHTTPRequestHandler.
            *args: Its arguments.
        """
        log.debug("%s %s", self.address_string(), format % args)

    def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        """Write a JSON response.

        Args:
            status: HTTP status.
            payload: JSON-serializable body.
        """
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        """Read and parse the JSON request body.

        Returns:
            The body as a mapping.

        Raises:
            ApiError: 413 when too large, 400 when not a JSON object.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        try:
            body: object = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, "body is not JSON") from e
        if not isinstance(body, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
        return body

    def _dispatch(self, method: str) -> None:
        """Authenticate, route and answer one request.

        Args:
            method: ``GET`` or ``POST``.
        """
        try:
            client = self.service.policy.client_for(self.headers.get("Authorization"))
            route = (method, self.path.rstrip("/"))
            match route:
                case ("GET", "/f2b/v1/state"):
                    payload = self.service.state(client)
                case ("POST", "/f2b/v1/state"):
                    payload = self.service.reconcile(client, self._body())
                case ("POST", "/f2b/v1/ban"):
                    payload = self.service.ban(client, self._body())
                case ("POST", "/f2b/v1/unban"):
                    payload = self.service.unban(client, self._body())
                case _:
                    raise ApiError(HTTPStatus.NOT_FOUND, f"no route {method} {self.path}")
            self._send(HTTPStatus.OK, payload)
        except ApiError as e:
            if e.status == HTTPStatus.UNAUTHORIZED:
                log.warning("rejected token from %s", self.address_string())
            self._send(e.status, {"error": e.message})
        except (subprocess.SubprocessError, ValueError) as e:
            log.error("fail2ban call failed: %s", e)
            self._send(HTTPStatus.BAD_GATEWAY, {"error": "fail2ban call failed"})

    def do_GET(self) -> None:
        """Handle GET."""
        self._dispatch("GET")

    def do_POST(self) -> None:
        """Handle POST."""
        self._dispatch("POST")


def read_tokens(path: Path) -> dict[str, str]:
    """Read the client tokens file.

    Args:
        path: File with one ``<client> <token>`` pair per line; ``#`` starts a comment.

    Returns:
        Client name to token.

    Raises:
        ValueError: On a malformed line, a duplicate client or a token shorter than 32 characters.
    """
    tokens: dict[str, str] = {}
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or parts[0] in tokens or len(parts[1]) < 32:
            raise ValueError(f"{path}:{n}: expected '<client> <token>' with a unique client and a token of >= 32 chars")
        tokens[parts[0]] = parts[1]
    return tokens


def read_networks(path: Path) -> list[ipaddress.IPv4Network]:
    """Read the protected networks file.

    Args:
        path: File with one IPv4 address or CIDR per line; ``#`` starts a comment.

    Returns:
        The networks.
    """
    nets: list[ipaddress.IPv4Network] = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            nets.append(ipaddress.IPv4Network(line, strict=False))
    return nets


app = typer.Typer(add_completion=False)


@app.command()
def serve(
    listen: Annotated[str, typer.Option(help="Address to bind, the hub's WireGuard address.")],
    port: Annotated[int, typer.Option(help="TCP port.")] = 8099,
    tokens_file: Annotated[Path, typer.Option(help="'<client> <token>' per line.")] = Path("/etc/f2b-api/tokens"),
    protect_file: Annotated[Path, typer.Option(help="Never-ban networks, one per line.")] = Path(
        "/etc/f2b-api/protect"
    ),
    fail2ban_client: Annotated[str, typer.Option(help="fail2ban-client path.")] = "/usr/bin/fail2ban-client",
    debug: Annotated[bool, typer.Option(help="Log every request.")] = False,
) -> None:
    """Serve the ban API until stopped.

    Args:
        listen: Address to bind.
        port: TCP port.
        tokens_file: Client tokens.
        protect_file: Networks that are never banned.
        fail2ban_client: Path of fail2ban-client.
        debug: Whether to log every request.
    """
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, format="%(levelname)s %(message)s")
    Handler.service = BanService(
        Policy(read_tokens(tokens_file), read_networks(protect_file)), Fail2ban([fail2ban_client])
    )
    server = ThreadingHTTPServer((listen, port), Handler)
    log.info("listening on %s:%d for clients %s", listen, port, sorted(Handler.service.policy.tokens))
    server.serve_forever()


if __name__ == "__main__":
    app()
