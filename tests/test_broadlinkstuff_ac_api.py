"""Tests for broadlinkstuff.ac_api: room discovery, routes, per-room locking, error mapping."""

import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from somestuff.broadlinkstuff.ac_api import ACTION_ROUTE, create_app, discover_rooms


def _write_script(script_dir: Path, name: str, body: str, *, executable: bool = True) -> Path:
    path = script_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture()
def script_dir(tmp_path: Path) -> Path:
    _write_script(tmp_path, "activate_ac_lounge.sh", "echo activated")
    _write_script(tmp_path, "deactivate_ac_lounge.sh", "echo deactivated")
    _write_script(
        tmp_path,
        "gettemp_ac_lounge.sh",
        'printf \'{"Lounge": {"temperature": 21.5, "humidity": 44}}\'',
    )
    _write_script(tmp_path, "gettemp_ac_serverraum.sh", "exit 0")
    _write_script(tmp_path, "activate_ac_fails.sh", "echo boom >&2; exit 3")
    _write_script(tmp_path, "activate_ac_slow.sh", "sleep 5")
    _write_script(tmp_path, "activate_ac_noexec.sh", "echo never", executable=False)
    _write_script(tmp_path, "reboot_ac_lounge.sh", "echo not-an-action")
    _write_script(tmp_path, "activate_ac_Upper.sh", "echo uppercase-slug")
    (tmp_path / "activate_ac_dir.sh").mkdir()
    return tmp_path


@pytest.fixture()
def client(script_dir: Path) -> TestClient:
    with TestClient(create_app(script_dir, timeout=0.5)) as c:
        yield c


def test_discover_rooms_filters_by_name_and_mode(script_dir: Path) -> None:
    rooms = discover_rooms(script_dir)
    assert sorted(rooms) == ["fails", "lounge", "serverraum", "slow"]
    assert sorted(rooms["lounge"]) == ["activate", "deactivate", "gettemp"]
    assert rooms["serverraum"] == {"gettemp": script_dir / "gettemp_ac_serverraum.sh"}


def test_discover_rooms_missing_dir(tmp_path: Path) -> None:
    assert discover_rooms(tmp_path / "nope") == {}


def test_action_route_matches_script_prefixes() -> None:
    assert set(ACTION_ROUTE.values()) == {"activate", "deactivate", "gettemp"}


def test_healthz(client: TestClient, script_dir: Path) -> None:
    body = client.get("/healthz").json()
    assert body == {"ok": True, "script_dir": str(script_dir), "rooms": 4}


def test_rooms_lists_supported_actions(client: TestClient) -> None:
    rooms = {r["name"]: r["actions"] for r in client.get("/rooms").json()}
    assert rooms["lounge"] == ["on", "off", "temp"]
    assert rooms["serverraum"] == ["temp"]
    assert rooms["fails"] == ["on"]


def test_switch_on_and_off(client: TestClient) -> None:
    on = client.post("/rooms/lounge/on").json()
    assert on["ok"] is True and on["action"] == "on" and on["stdout"] == "activated"
    off = client.post("/rooms/Lounge/off").json()
    assert off["room"] == "lounge" and off["stdout"] == "deactivated"


def test_temp_parses_json_and_drops_device_key(client: TestClient) -> None:
    body = client.get("/rooms/lounge/temp").json()
    assert body["room"] == "lounge"
    assert body["temperature"] == 21.5
    assert body["humidity"] == 44
    assert body["raw"] == {"Lounge": {"temperature": 21.5, "humidity": 44}}


def test_temp_without_json_is_502(client: TestClient) -> None:
    resp = client.get("/rooms/serverraum/temp")
    assert resp.status_code == 502
    assert "no JSON" in resp.json()["detail"]


def test_unknown_room_is_404(client: TestClient) -> None:
    resp = client.post("/rooms/kueche/on")
    assert resp.status_code == 404
    assert "kueche" in resp.json()["detail"]


def test_missing_action_is_404(client: TestClient) -> None:
    resp = client.post("/rooms/serverraum/on")
    assert resp.status_code == 404


def test_nonzero_exit_is_502_with_stderr(client: TestClient) -> None:
    resp = client.post("/rooms/fails/on")
    assert resp.status_code == 502
    assert "exited 3" in resp.json()["detail"]
    assert "boom" in resp.json()["detail"]


def test_timeout_is_504(client: TestClient) -> None:
    resp = client.post("/rooms/slow/on")
    assert resp.status_code == 504
    assert "timeout" in resp.json()["detail"]


def test_get_on_switch_route_is_405(client: TestClient) -> None:
    assert client.get("/rooms/lounge/on").status_code == 405


def test_scripts_run_with_inherited_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_script(tmp_path, "activate_ac_env.sh", 'printf "%s" "$AC_API_TEST_MARKER"')
    monkeypatch.setenv("AC_API_TEST_MARKER", "marker-42")
    with TestClient(create_app(tmp_path, timeout=5)) as c:
        assert c.post("/rooms/env/on").json()["stdout"] == "marker-42"
    assert os.environ["AC_API_TEST_MARKER"] == "marker-42"
