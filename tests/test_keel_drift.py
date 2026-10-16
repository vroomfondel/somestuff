"""Tests for k3shelperstuff.keel_drift."""

import pytest
import requests

from k3shelperstuff import keel_drift
from k3shelperstuff.keel_drift import (
    REGISTRY_MAX_ATTEMPTS,
    REGISTRY_RETRY_BASE_SECONDS,
    REGISTRY_RETRY_MAX_WAIT_SECONDS,
    retry_wait_seconds,
)

GHCR_429_BODY = '{"errors":[{"code":"TOOMANYREQUESTS","message":"retry-after: 458.955µs, allowed: 44000/minute"}]}'


def _response(status: int, body: str = "", headers: dict[str, str] | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = body.encode()
    response.encoding = "utf-8"
    response.headers.update(headers or {})
    return response


@pytest.mark.parametrize(
    ("body", "headers", "attempt", "expected"),
    [
        (GHCR_429_BODY, {}, 0, REGISTRY_RETRY_BASE_SECONDS),
        (GHCR_429_BODY, {}, 2, REGISTRY_RETRY_BASE_SECONDS * 4),
        ('{"message":"retry-after: 3s"}', {}, 0, 3.0),
        ('{"message":"retry-after: 1500ms"}', {}, 0, 1.5),
        ("", {"Retry-After": "7"}, 0, 7.0),
        ("", {"Retry-After": "3600"}, 0, REGISTRY_RETRY_MAX_WAIT_SECONDS),
        ("", {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, 0, REGISTRY_RETRY_BASE_SECONDS),
        ("", {"Retry-After": "garbage"}, 1, REGISTRY_RETRY_BASE_SECONDS * 2),
        ("no hint at all", {}, 10, REGISTRY_RETRY_MAX_WAIT_SECONDS),
    ],
)
def test_retry_wait_seconds(body: str, headers: dict[str, str], attempt: int, expected: float) -> None:
    assert retry_wait_seconds(_response(429, body, headers), attempt) == pytest.approx(expected)


def _fake_registry(monkeypatch: pytest.MonkeyPatch, statuses: list[int]) -> tuple[list[str], list[float]]:
    calls: list[str] = []
    sleeps: list[float] = []
    pending = iter(statuses)

    def fake_request(method: str, url: str, **_: object) -> requests.Response:
        calls.append(url)
        status = next(pending)
        return _response(status, GHCR_429_BODY if status == 429 else "{}")

    monkeypatch.setattr("requests.request", fake_request)
    monkeypatch.setattr("time.sleep", sleeps.append)
    return calls, sleeps


def test_request_retries_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sleeps = _fake_registry(monkeypatch, [429, 429, 200])
    response = keel_drift._request("HEAD", "https://ghcr.io/v2/x/manifests/latest")
    assert response.status_code == 200
    assert len(calls) == 3
    assert sleeps == [REGISTRY_RETRY_BASE_SECONDS, REGISTRY_RETRY_BASE_SECONDS * 2]


def test_request_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sleeps = _fake_registry(monkeypatch, [429] * (REGISTRY_MAX_ATTEMPTS + 2))
    response = keel_drift._request("GET", "https://ghcr.io/v2/x/manifests/latest")
    assert response.status_code == 429
    assert len(calls) == REGISTRY_MAX_ATTEMPTS
    assert len(sleeps) == REGISTRY_MAX_ATTEMPTS - 1


def test_request_does_not_retry_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sleeps = _fake_registry(monkeypatch, [503])
    assert keel_drift._request("GET", "https://ghcr.io/v2/x/manifests/latest").status_code == 503
    assert len(calls) == 1
    assert sleeps == []


def test_authenticated_get_retries_each_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    pending = iter(
        [
            _response(429, GHCR_429_BODY),
            _response(
                401, headers={"WWW-Authenticate": 'Bearer realm="https://ghcr.io/token",scope="repository:x:pull"'}
            ),
            _response(429, GHCR_429_BODY),
            _response(200, '{"token":"t"}'),
            _response(429, GHCR_429_BODY),
            _response(200, "{}"),
        ]
    )
    seen_auth: list[str | None] = []

    def fake_request(method: str, url: str, **kwargs: object) -> requests.Response:
        headers = kwargs.get("headers")
        seen_auth.append(headers.get("Authorization") if isinstance(headers, dict) else None)
        return next(pending)

    monkeypatch.setattr("requests.request", fake_request)
    monkeypatch.setattr("time.sleep", lambda _: None)

    response = keel_drift._authenticated_get("https://ghcr.io/v2/x/manifests/latest", None)
    assert response.status_code == 200
    assert seen_auth[-1] == "Bearer t"
