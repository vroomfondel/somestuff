"""Thin typed clients for the Home Assistant REST and WebSocket APIs.

REST covers states, services, history, logbook, templates and the automation/script config store.
The WebSocket API is needed for everything REST does not expose: entity/device/area registry,
helpers (``input_*``, ``counter``, ``timer``, ``schedule``), Lovelace dashboards and traces.
"""

import itertools
import json
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final
from typing import Self
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import httpx
from websockets.sync.client import ClientConnection
from websockets.sync.client import connect

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

FALLBACK_URL: Final = "http://homeassistant.local:8123"
TOKEN_FILE_NAME: Final = "homeassistant_token.local.txt"
URL_FILE_NAME: Final = "homeassistant_url.local.txt"


def find_local_file(name: str) -> Path:
    """Locate a local file in the current directory or one of its parents, each also under ``secrets.local/``.

    Args:
        name: File name, e.g. ``homeassistant_token.local.txt``.

    Returns:
        The first existing candidate, otherwise the path in the current directory (for the error message).
    """
    cwd = Path.cwd()
    candidates = [path for base in (cwd, *cwd.parents) for path in (base / name, base / "secrets.local" / name)]
    return next((path for path in candidates if path.is_file()), candidates[0])


def default_url() -> str:
    """Resolve the Home Assistant base URL from ``HASS_URL``, then ``homeassistant_url.local.txt``.

    Returns:
        The configured URL, otherwise :data:`FALLBACK_URL`.
    """
    if url := os.environ.get("HASS_URL", "").strip():
        return url
    url_file = find_local_file(URL_FILE_NAME)
    if url_file.is_file() and (url := url_file.read_text(encoding="utf-8").strip()):
        return url
    return FALLBACK_URL


def default_token_file() -> Path:
    """Locate ``homeassistant_token.local.txt`` like :func:`find_local_file`.

    Returns:
        The token file path (may not exist).
    """
    return find_local_file(TOKEN_FILE_NAME)


class HassError(RuntimeError):
    """Raised when Home Assistant rejects a request or answers unexpectedly.

    Attributes:
        status_code: HTTP status of a rejected REST request, otherwise ``None``.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Store the message and the optional HTTP status.

        Args:
            message: Error text.
            status_code: HTTP status of a rejected REST request.
        """
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class HassConfig:
    """Connection settings for one Home Assistant instance.

    Attributes:
        url: Base URL without trailing ``/api``, e.g. ``https://ha.example.org``.
        token: Long-lived access token.
        verify_tls: Whether to verify the server certificate.
        timeout: Per-request timeout in seconds.
    """

    url: str
    token: str
    verify_tls: bool = True
    timeout: float = 30.0

    @property
    def ws_url(self) -> str:
        """WebSocket endpoint derived from :attr:`url`.

        Returns:
            The ``ws://``/``wss://`` URL of ``/api/websocket``.
        """
        parts = urlsplit(self.url.rstrip("/"))
        scheme = "wss" if parts.scheme == "https" else "ws"
        return urlunsplit((scheme, parts.netloc, parts.path + "/api/websocket", "", ""))


def load_token(token: str | None, token_file: Path | None) -> str:
    """Resolve the access token from an explicit value or a file.

    Args:
        token: Token given directly; wins over ``token_file``.
        token_file: File holding the token on its first line.

    Returns:
        The stripped token.

    Raises:
        HassError: If neither source yields a token.
    """
    if token:
        return token.strip()
    if token_file and token_file.is_file():
        value = token_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise HassError(f"no token: pass --token, set HASS_TOKEN or provide {token_file}")


class HassRest:
    """Synchronous client for ``/api/*`` REST endpoints."""

    def __init__(self, config: HassConfig, transport: httpx.BaseTransport | None = None) -> None:
        """Create the HTTP client.

        Args:
            config: Connection settings.
            transport: Custom httpx transport, e.g. ``httpx.MockTransport`` in tests.
        """
        self._client = httpx.Client(
            base_url=config.url.rstrip("/") + "/api",
            headers={"Authorization": f"Bearer {config.token}"},
            verify=config.verify_tls,
            timeout=config.timeout,
            transport=transport,
        )

    def __enter__(self) -> Self:
        """Enter the context.

        Returns:
            This client.
        """
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        """Close the HTTP client.

        Args:
            exc_type: Exception type, if any.
            exc: Exception instance, if any.
            tb: Traceback, if any.
        """
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: JsonValue = None,
    ) -> JsonValue:
        """Send one request and decode the JSON (or text) answer.

        Args:
            method: HTTP method.
            path: Path below ``/api``, starting with ``/``.
            params: Query parameters.
            body: JSON body; omitted when ``None``.

        Returns:
            Decoded JSON, or the raw text for non-JSON answers (``/api/template``).

        Raises:
            HassError: On any status >= 400, carrying ``status_code``.
        """
        resp = self._client.request(method, path, params=params, json=body)
        if resp.status_code >= 400:
            raise HassError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:500]}", resp.status_code)
        if not resp.content:
            return None
        if resp.headers.get("content-type", "").startswith("application/json"):
            result: JsonValue = resp.json()
            return result
        return resp.text

    def get(self, path: str, params: dict[str, str] | None = None) -> JsonValue:
        """GET shortcut.

        Args:
            path: Path below ``/api``.
            params: Query parameters.

        Returns:
            Decoded answer.
        """
        return self.request("GET", path, params=params)

    def post(self, path: str, body: JsonValue = None, params: dict[str, str] | None = None) -> JsonValue:
        """POST shortcut.

        Args:
            path: Path below ``/api``.
            body: JSON body.
            params: Query parameters.

        Returns:
            Decoded answer.
        """
        return self.request("POST", path, params=params, body=body)

    def delete(self, path: str) -> JsonValue:
        """DELETE shortcut.

        Args:
            path: Path below ``/api``.

        Returns:
            Decoded answer.
        """
        return self.request("DELETE", path)


class HassWebSocket:
    """Synchronous client for the ``/api/websocket`` command protocol."""

    def __init__(self, config: HassConfig) -> None:
        """Store settings; the connection opens in :meth:`__enter__`.

        Args:
            config: Connection settings.
        """
        self._config = config
        self._conn: ClientConnection | None = None
        self._ids = itertools.count(1)
        self.ha_version: str | None = None

    def __enter__(self) -> Self:
        """Connect and authenticate.

        Returns:
            This client.

        Raises:
            HassError: If authentication fails.
        """
        ssl_ctx: ssl.SSLContext | None = None
        if self._config.ws_url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            if not self._config.verify_tls:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
        self._conn = connect(self._config.ws_url, ssl=ssl_ctx, max_size=None, open_timeout=self._config.timeout)
        try:
            hello = self._recv()
            self.ha_version = str(hello.get("ha_version"))
            self._send({"type": "auth", "access_token": self._config.token})
            answer = self._recv()
            if answer.get("type") != "auth_ok":
                raise HassError(f"websocket auth failed: {answer.get('message', answer)}")
        except BaseException:
            self._close()
            raise
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        """Close the connection.

        Args:
            exc_type: Exception type, if any.
            exc: Exception instance, if any.
            tb: Traceback, if any.
        """
        self._close()

    def _close(self) -> None:
        """Close the connection if it is open."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _send(self, message: JsonObject) -> None:
        """Send one JSON frame.

        Args:
            message: Frame payload.

        Raises:
            HassError: If called outside the context manager.
        """
        if self._conn is None:
            raise HassError("websocket not connected")
        self._conn.send(json.dumps(message))

    def _recv(self) -> JsonObject:
        """Receive one JSON frame.

        Returns:
            Decoded frame.

        Raises:
            HassError: If called outside the context manager or the frame is not an object.
        """
        if self._conn is None:
            raise HassError("websocket not connected")
        frame = json.loads(self._conn.recv(timeout=self._config.timeout))
        if not isinstance(frame, dict):
            raise HassError(f"unexpected websocket frame: {frame!r}")
        return frame

    def call(self, msg_type: str, **payload: JsonValue) -> JsonValue:
        """Run one command and wait for its ``result`` frame.

        Args:
            msg_type: Command type, e.g. ``config/entity_registry/list``.
            **payload: Additional command fields.

        Returns:
            The ``result`` member of the answer.

        Raises:
            HassError: If Home Assistant reports ``success: false``.
        """
        msg_id = next(self._ids)
        self._send({"id": msg_id, "type": msg_type, **payload})
        while True:
            frame = self._recv()
            if frame.get("id") != msg_id or frame.get("type") != "result":
                continue
            if not frame.get("success"):
                error = frame.get("error")
                raise HassError(f"{msg_type}: {json.dumps(error, ensure_ascii=False)}")
            return frame.get("result")
