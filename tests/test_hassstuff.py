"""Tests for somestuff.hassstuff (client + CLI, no live Home Assistant)."""

import json
from pathlib import Path

import httpx
import pytest
import typer
from typer.testing import CliRunner

from somestuff.hassstuff import cli
from somestuff.hassstuff import client
from somestuff.hassstuff.client import FALLBACK_URL
from somestuff.hassstuff.client import TOKEN_FILE_NAME
from somestuff.hassstuff.client import URL_FILE_NAME
from somestuff.hassstuff.client import HassConfig
from somestuff.hassstuff.client import HassError
from somestuff.hassstuff.client import HassRest
from somestuff.hassstuff.client import HassWebSocket

CONFIG = HassConfig(url="http://ha.test:8123", token="secret")


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "a" / "b"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    for var in ("HASS_URL", "HASS_TOKEN", "HASS_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    return work


def test_import_reads_nothing() -> None:
    assert not hasattr(client, "DEFAULT_URL")
    assert not hasattr(client, "DEFAULT_TOKEN_FILE")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://ha.test:8123", "ws://ha.test:8123/api/websocket"),
        ("https://ha.test/", "wss://ha.test/api/websocket"),
        ("https://proxy.test/ha", "wss://proxy.test/ha/api/websocket"),
    ],
)
def test_ws_url(url: str, expected: str) -> None:
    assert HassConfig(url=url, token="t").ws_url == expected


def test_find_local_file_in_parent(isolated: Path) -> None:
    target = isolated.parent / TOKEN_FILE_NAME
    target.write_text("x")
    assert client.find_local_file(TOKEN_FILE_NAME) == target


def test_find_local_file_in_secrets_local(isolated: Path) -> None:
    (isolated.parent.parent / "secrets.local").mkdir()
    target = isolated.parent.parent / "secrets.local" / TOKEN_FILE_NAME
    target.write_text("x")
    assert client.find_local_file(TOKEN_FILE_NAME) == target


def test_find_local_file_missing_returns_cwd_candidate(isolated: Path) -> None:
    assert client.find_local_file("does-not-exist.local.txt") == isolated / "does-not-exist.local.txt"


def test_default_url_order(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.default_url() == FALLBACK_URL
    (isolated / URL_FILE_NAME).write_text("https://from-file.test\n")
    assert client.default_url() == "https://from-file.test"
    monkeypatch.setenv("HASS_URL", "https://from-env.test")
    assert client.default_url() == "https://from-env.test"


def test_load_token(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(" from-file \n")
    assert client.load_token(" direct ", token_file) == "direct"
    assert client.load_token(None, token_file) == "from-file"
    with pytest.raises(HassError, match="no token"):
        client.load_token(None, tmp_path / "missing")


def test_parse_kv() -> None:
    result = cli._parse_kv(["max=300", "name=Soll", 'options=["a","b"]', "flag=true"], '{"name": "x", "min": 0}')
    assert result == {"name": "Soll", "min": 0, "max": 300, "options": ["a", "b"], "flag": True}
    assert cli._parse_kv(None, None) == {}


@pytest.mark.parametrize(("items", "data"), [(["novalue"], None), (["=x"], None), (None, "[1]")])
def test_parse_kv_rejects(items: list[str] | None, data: str | None) -> None:
    with pytest.raises(typer.BadParameter):
        cli._parse_kv(items, data)


@pytest.mark.parametrize(
    ("pattern", "fields", "expected"),
    [
        (None, ("light.kitchen",), True),
        ("KITCHEN", ("light.kitchen",), True),
        ("light.*", ("light.kitchen",), True),
        ("sensor.*", ("light.kitchen", "Kitchen"), False),
        ("kit", (None, "Kitchen"), True),
        ("bath", ("light.kitchen", None), False),
    ],
)
def test_matches(pattern: str | None, fields: tuple[str | None, ...], expected: bool) -> None:
    assert cli._matches(pattern, *fields) is expected


def _rest(handler: httpx.MockTransport) -> HassRest:
    return HassRest(CONFIG, transport=handler)


def test_rest_decodes_json_text_and_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        if request.url.path == "/api/states":
            return httpx.Response(200, json=[{"entity_id": "light.a"}])
        if request.url.path == "/api/template":
            assert json.loads(request.content) == {"template": "{{ 1 }}"}
            return httpx.Response(200, text="1")
        return httpx.Response(200)

    with _rest(httpx.MockTransport(handler)) as rest:
        assert rest.get("/states") == [{"entity_id": "light.a"}]
        assert rest.post("/template", {"template": "{{ 1 }}"}) == "1"
        assert rest.delete("/config/automation/config/x") is None


def test_rest_omits_body_when_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == b""
        return httpx.Response(200)

    with _rest(httpx.MockTransport(handler)) as rest:
        rest.post("/services/homeassistant/restart")


def test_rest_error_carries_status() -> None:
    with _rest(httpx.MockTransport(lambda request: httpx.Response(404, text="not found"))) as rest:
        with pytest.raises(HassError) as exc_info:
            rest.get("/states/light.nope")
    assert exc_info.value.status_code == 404


class FakeConnection:
    def __init__(self, frames: list[dict[str, object]]) -> None:
        self.frames = [json.dumps(f) for f in frames]
        self.sent: list[dict[str, object]] = []
        self.closed = False

    def recv(self, timeout: float | None = None) -> str:
        return self.frames.pop(0)

    def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def close(self) -> None:
        self.closed = True


def _fake_connect(monkeypatch: pytest.MonkeyPatch, conn: FakeConnection) -> None:
    monkeypatch.setattr(client, "connect", lambda *args, **kwargs: conn)


def test_websocket_auth_failure_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConnection([{"type": "auth_required", "ha_version": "2026.11.0"}, {"type": "auth_invalid"}])
    _fake_connect(monkeypatch, conn)
    with pytest.raises(HassError, match="auth failed"):
        with HassWebSocket(CONFIG):
            pass
    assert conn.closed


def test_websocket_call(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConnection(
        [
            {"type": "auth_required", "ha_version": "2026.11.0"},
            {"type": "auth_ok"},
            {"id": 1, "type": "event", "event": {}},
            {"id": 1, "type": "result", "success": True, "result": [{"area_id": "kitchen"}]},
            {"id": 2, "type": "result", "success": False, "error": {"code": "not_found"}},
        ]
    )
    _fake_connect(monkeypatch, conn)
    with HassWebSocket(CONFIG) as ws:
        assert ws.ha_version == "2026.11.0"
        assert ws.call("config/area_registry/list") == [{"area_id": "kitchen"}]
        with pytest.raises(HassError, match="not_found"):
            ws.call("config/entity_registry/get", entity_id="light.x")
    assert conn.sent[0] == {"type": "auth", "access_token": "secret"}
    assert conn.sent[1] == {"id": 1, "type": "config/area_registry/list"}
    assert conn.closed


def test_subcommand_help_without_token(isolated: Path) -> None:
    result = CliRunner().invoke(cli.app, ["states", "--help"])
    assert result.exit_code == 0, result.output
    assert "List current states" in result.output


def test_command_without_token_fails(isolated: Path) -> None:
    result = CliRunner().invoke(cli.app, ["states"])
    assert isinstance(result.exception, HassError)


def _put_app(monkeypatch: pytest.MonkeyPatch, get_status: int, posted: list[object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(get_status, json={"message": "x"})
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"result": "ok"})

    monkeypatch.setattr(cli, "HassRest", lambda config: HassRest(config, transport=httpx.MockTransport(handler)))


def test_put_creates_on_404(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[object] = []
    _put_app(monkeypatch, 404, posted)
    (isolated / "a.yaml").write_text("alias: Test\n")
    result = CliRunner().invoke(cli.app, ["--token", "t", "automation", "put", "abc", "a.yaml"])
    assert result.exit_code == 0, result.output
    assert posted == [{"alias": "Test", "id": "abc"}]


def test_put_aborts_on_other_errors(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[object] = []
    _put_app(monkeypatch, 401, posted)
    (isolated / "a.yaml").write_text("alias: Test\n")
    result = CliRunner().invoke(cli.app, ["--token", "t", "automation", "put", "abc", "a.yaml"])
    assert isinstance(result.exception, HassError)
    assert result.exception.status_code == 401
    assert posted == []


@pytest.mark.parametrize(
    "error",
    [
        HassError("boom"),
        httpx.ConnectError("refused"),
        ConnectionRefusedError("refused"),
        TimeoutError("slow"),
    ],
)
def test_run_maps_errors_to_exit_1(
    error: Exception, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> None:
        raise error

    monkeypatch.setattr(cli, "app", fail)
    with pytest.raises(SystemExit) as exc_info:
        cli.run()
    assert exc_info.value.code == 1
    assert capsys.readouterr().err.startswith("error: ")
