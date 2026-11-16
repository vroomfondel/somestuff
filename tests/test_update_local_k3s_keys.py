import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from somestuff.k3shelperstuff.update_local_k3s_keys import (
    compare_credentials,
    describe_certificate,
    describe_private_key,
    update_local_kubeconfig,
)


def _key_b64(key: ec.EllipticCurvePrivateKey) -> str:
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return base64.b64encode(pem).decode()


def _cert_pem(key: ec.EllipticCurvePrivateKey, not_after: datetime) -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "system:admin")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_after - timedelta(days=365))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_certificate_summary_shows_expiry_and_subject() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)

    expired = describe_certificate(_b64(_cert_pem(key, now - timedelta(days=1))))
    valid = describe_certificate(_b64(_cert_pem(key, now + timedelta(days=100))))

    assert expired is not None and "EXPIRED" in expired and "CN=system:admin" in expired
    assert valid is not None and "EXPIRED" not in valid and "d left" in valid


def test_certificate_summary_counts_chain() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = _cert_pem(key, datetime.now(timezone.utc) + timedelta(days=100))

    summary = describe_certificate(_b64(pem + pem))

    assert summary is not None and "(+1 in chain)" in summary


def test_private_key_summary_never_contains_key_material() -> None:
    value = _key_b64(ec.generate_private_key(ec.SECP256R1()))

    summary = describe_private_key(value)

    assert summary is not None and summary.startswith("public-key-sha256=")
    assert value[:40] not in summary


def test_undecodable_values_stay_distinguishable() -> None:
    first, second = describe_certificate("bm90LWEtY2VydA=="), describe_private_key("bm90LWEta2V5")

    assert first is not None and first.startswith("undecodable")
    assert second is not None and second.startswith("undecodable")
    assert first != second


def test_differing_credentials_are_visibly_different() -> None:
    not_after = datetime.now(timezone.utc) + timedelta(days=100)
    old_key, new_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    remote = {"client-certificate-data": _b64(_cert_pem(new_key, not_after)), "client-key-data": _key_b64(new_key)}
    local = {"client-certificate-data": _b64(_cert_pem(old_key, not_after)), "client-key-data": _key_b64(old_key)}

    differences = compare_credentials(remote, local, None, None)

    assert set(differences) == {"client-certificate-data", "client-key-data"}
    for values in differences.values():
        assert values["remote"] != values["local"]


def test_same_decoded_content_with_other_encoding_is_flagged() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = _cert_pem(key, datetime.now(timezone.utc) + timedelta(days=100))
    remote = {"client-certificate-data": _b64(pem)}
    local = {"client-certificate-data": _b64(pem + b"\n")}

    differences = compare_credentials(remote, local, None, None)

    assert "raw value differs" in str(differences["client-certificate-data"]["remote"])


def test_missing_value_is_reported_as_none() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    remote = {"client-key-data": _key_b64(key)}

    differences = compare_credentials(remote, {}, None, None)

    assert differences["client-key-data"]["local"] is None
    assert differences["client-key-data"]["remote"] is not None


@pytest.mark.parametrize(("remote_ca", "expected_ca"), [(None, "local-ca"), ("", "local-ca"), ("new-ca", "new-ca")])
def test_update_keeps_local_ca_unless_remote_provides_one(
    tmp_path: Path, remote_ca: str | None, expected_ca: str
) -> None:
    path = tmp_path / "config"
    kubeconfig: dict[str, object] = {
        "users": [{"name": "u", "user": {"client-certificate-data": "old", "client-key-data": "old"}}],
        "clusters": [{"name": "c", "cluster": {"certificate-authority-data": "local-ca", "server": "https://x:6443"}}],
    }
    remote = {"client-certificate-data": "new", "client-key-data": "new"}

    update_local_kubeconfig(path, kubeconfig, "u", "c", remote, remote_ca)

    written = yaml.safe_load(path.read_text())
    assert written["clusters"][0]["cluster"]["certificate-authority-data"] == expected_ca
    assert written["clusters"][0]["cluster"]["server"] == "https://x:6443"
    assert written["users"][0]["user"]["client-certificate-data"] == "new"
