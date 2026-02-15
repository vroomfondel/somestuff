"""Tests for sipstuff: config validation, WAV validation, mocked call logic."""

import os
import struct
import tempfile
import wave
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sipstuff.sip_caller import SipCaller, SipCallError, WavInfo
from sipstuff.sipconfig import CallConfig, NatConfig, SipCallerConfig, SipConfig, load_config

# ─── Config model tests ──────────────────────────────────────────────────────


class TestSipConfig:
    def test_minimal_config(self) -> None:
        cfg = SipConfig(server="pbx.local", user="1000", password="secret")
        assert cfg.server == "pbx.local"
        assert cfg.port == 5060
        assert cfg.transport == "udp"
        assert cfg.local_port == 0

    def test_full_config(self) -> None:
        cfg = SipConfig(server="10.0.0.1", port=5061, user="ext", password="pw", transport="tcp", local_port=15060)
        assert cfg.port == 5061
        assert cfg.transport == "tcp"
        assert cfg.local_port == 15060

    def test_invalid_port_too_high(self) -> None:
        with pytest.raises(Exception):
            SipConfig(server="pbx", user="u", password="p", port=99999)

    def test_invalid_port_negative(self) -> None:
        with pytest.raises(Exception):
            SipConfig(server="pbx", user="u", password="p", port=-1)

    def test_invalid_transport(self) -> None:
        with pytest.raises(Exception):
            SipConfig(server="pbx", user="u", password="p", transport="ws")  # type: ignore[arg-type]


class TestCallConfig:
    def test_defaults(self) -> None:
        cfg = CallConfig()
        assert cfg.timeout == 60

    def test_custom_timeout(self) -> None:
        cfg = CallConfig(timeout=30)
        assert cfg.timeout == 30

    def test_timeout_too_low(self) -> None:
        with pytest.raises(Exception):
            CallConfig(timeout=0)

    def test_timeout_too_high(self) -> None:
        with pytest.raises(Exception):
            CallConfig(timeout=999)


class TestSipCallerConfig:
    def test_nested_form(self) -> None:
        cfg = SipCallerConfig(sip=SipConfig(server="pbx", user="u", password="p"))
        assert cfg.sip.server == "pbx"
        assert cfg.call.timeout == 60

    def test_flat_dict_form(self) -> None:
        cfg = SipCallerConfig(**{"server": "pbx", "user": "u", "password": "p", "timeout": 30})
        assert cfg.sip.server == "pbx"
        assert cfg.call.timeout == 30


class TestNatConfig:
    def test_nat_defaults(self) -> None:
        nat = NatConfig()
        assert nat.stun_servers == []
        assert nat.stun_ignore_failure is True
        assert nat.ice_enabled is False
        assert nat.turn_enabled is False
        assert nat.turn_server == ""
        assert nat.keepalive_sec == 0
        assert nat.public_address == ""

    def test_public_address(self) -> None:
        nat = NatConfig(public_address="192.168.1.50")
        assert nat.public_address == "192.168.1.50"

    def test_stun_only(self) -> None:
        nat = NatConfig(stun_servers=["stun.l.google.com:19302"])
        assert len(nat.stun_servers) == 1
        assert nat.ice_enabled is False

    def test_ice_with_stun(self) -> None:
        nat = NatConfig(stun_servers=["stun.example.com:3478"], ice_enabled=True)
        assert nat.ice_enabled is True
        assert len(nat.stun_servers) == 1

    def test_turn_requires_server(self) -> None:
        with pytest.raises(Exception, match="turn_enabled requires turn_server"):
            NatConfig(turn_enabled=True)

    def test_turn_with_server(self) -> None:
        nat = NatConfig(turn_enabled=True, turn_server="turn.example.com:3478", turn_username="u", turn_password="p")
        assert nat.turn_enabled is True
        assert nat.turn_server == "turn.example.com:3478"
        assert nat.turn_transport == "udp"

    def test_full_nat_config(self) -> None:
        cfg = SipCallerConfig(
            sip=SipConfig(server="pbx", user="u", password="p"),
            nat=NatConfig(
                stun_servers=["stun.example.com:3478"],
                ice_enabled=True,
                turn_enabled=True,
                turn_server="turn.example.com:3478",
                turn_username="tu",
                turn_password="tp",
                turn_transport="tcp",
                keepalive_sec=30,
            ),
        )
        assert len(cfg.nat.stun_servers) == 1
        assert cfg.nat.ice_enabled is True
        assert cfg.nat.turn_enabled is True
        assert cfg.nat.keepalive_sec == 30

    def test_backward_compat_no_nat(self) -> None:
        cfg = SipCallerConfig(sip=SipConfig(server="pbx", user="u", password="p"))
        assert cfg.nat.stun_servers == []
        assert cfg.nat.ice_enabled is False
        assert cfg.nat.turn_enabled is False

    def test_flat_dict_with_nat(self) -> None:
        cfg = SipCallerConfig(
            **{"server": "pbx", "user": "u", "password": "p", "stun_servers": ["stun:3478"], "ice_enabled": True}
        )
        assert cfg.nat.stun_servers == ["stun:3478"]
        assert cfg.nat.ice_enabled is True

    def test_flat_dict_with_public_address(self) -> None:
        cfg = SipCallerConfig(**{"server": "pbx", "user": "u", "password": "p", "public_address": "192.168.1.50"})
        assert cfg.nat.public_address == "192.168.1.50"

    def test_keepalive_out_of_range(self) -> None:
        with pytest.raises(Exception):
            NatConfig(keepalive_sec=999)


class TestLoadConfig:
    def test_from_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "sip.yaml"
        yaml_file.write_text("sip:\n  server: pbx.test\n  user: '1000'\n  password: secret\ncall:\n  timeout: 45\n")
        cfg = load_config(config_path=yaml_file)
        assert cfg.sip.server == "pbx.test"
        assert cfg.sip.user == "1000"
        assert cfg.call.timeout == 45

    def test_from_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIP_SERVER", "env.pbx")
        monkeypatch.setenv("SIP_USER", "2000")
        monkeypatch.setenv("SIP_PASSWORD", "envpw")
        cfg = load_config()
        assert cfg.sip.server == "env.pbx"
        assert cfg.sip.user == "2000"

    def test_overrides_take_priority(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "sip.yaml"
        yaml_file.write_text("sip:\n  server: yaml.pbx\n  user: '1000'\n  password: yamlpw\n")
        cfg = load_config(config_path=yaml_file, overrides={"server": "override.pbx"})
        assert cfg.sip.server == "override.pbx"

    def test_env_overrides_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        yaml_file = tmp_path / "sip.yaml"
        yaml_file.write_text("sip:\n  server: yaml.pbx\n  user: '1000'\n  password: yamlpw\n")
        monkeypatch.setenv("SIP_SERVER", "env.pbx")
        cfg = load_config(config_path=yaml_file)
        assert cfg.sip.server == "env.pbx"

    def test_nonexistent_yaml(self) -> None:
        cfg = load_config(config_path="/nonexistent/sip.yaml", overrides={"server": "s", "user": "u", "password": "p"})
        assert cfg.sip.server == "s"

    def test_nat_from_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "sip.yaml"
        yaml_file.write_text(
            "sip:\n  server: pbx\n  user: u\n  password: p\n"
            "nat:\n  stun_servers:\n    - stun.example.com:3478\n  ice_enabled: true\n  keepalive_sec: 30\n"
        )
        cfg = load_config(config_path=yaml_file)
        assert cfg.nat.stun_servers == ["stun.example.com:3478"]
        assert cfg.nat.ice_enabled is True
        assert cfg.nat.keepalive_sec == 30

    def test_nat_env_stun_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIP_SERVER", "pbx")
        monkeypatch.setenv("SIP_USER", "u")
        monkeypatch.setenv("SIP_PASSWORD", "p")
        monkeypatch.setenv("SIP_STUN_SERVERS", "a:3478, b:3478")
        cfg = load_config()
        assert cfg.nat.stun_servers == ["a:3478", "b:3478"]

    def test_nat_overrides(self) -> None:
        cfg = load_config(
            overrides={"server": "pbx", "user": "u", "password": "p", "ice_enabled": True, "keepalive_sec": 20}
        )
        assert cfg.nat.ice_enabled is True
        assert cfg.nat.keepalive_sec == 20

    def test_public_address_from_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "sip.yaml"
        yaml_file.write_text(
            "sip:\n  server: pbx\n  user: u\n  password: p\n" "nat:\n  public_address: '192.168.1.50'\n"
        )
        cfg = load_config(config_path=yaml_file)
        assert cfg.nat.public_address == "192.168.1.50"

    def test_public_address_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIP_SERVER", "pbx")
        monkeypatch.setenv("SIP_USER", "u")
        monkeypatch.setenv("SIP_PASSWORD", "p")
        monkeypatch.setenv("SIP_PUBLIC_ADDRESS", "10.0.0.1")
        cfg = load_config()
        assert cfg.nat.public_address == "10.0.0.1"

    def test_public_address_from_overrides(self) -> None:
        cfg = load_config(overrides={"server": "pbx", "user": "u", "password": "p", "public_address": "172.16.0.1"})
        assert cfg.nat.public_address == "172.16.0.1"


# ─── WAV validation tests ────────────────────────────────────────────────────


def _make_wav(path: Path, channels: int = 1, sampwidth: int = 2, framerate: int = 8000, n_frames: int = 8000) -> Path:
    """Generate a minimal WAV file for testing."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(struct.pack(f"<{n_frames}h", *([0] * n_frames)))
    return path


class TestWavInfo:
    def test_valid_wav(self, tmp_path: Path) -> None:
        wav_path = _make_wav(tmp_path / "test.wav")
        info = WavInfo(wav_path)
        assert info.channels == 1
        assert info.sample_width == 2
        assert info.framerate == 8000
        assert info.duration == pytest.approx(1.0, abs=0.01)

    def test_wav_not_found(self) -> None:
        with pytest.raises(SipCallError, match="not found"):
            WavInfo("/nonexistent/test.wav")

    def test_invalid_file(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.wav"
        bad_file.write_text("not a wav file")
        with pytest.raises(SipCallError, match="Cannot read WAV"):
            WavInfo(bad_file)

    def test_stereo_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        wav_path = _make_wav(tmp_path / "stereo.wav", channels=2, n_frames=16000)
        info = WavInfo(wav_path)
        with caplog.at_level("WARNING"):
            info.validate()
        assert info.channels == 2

    def test_non_standard_rate(self, tmp_path: Path) -> None:
        wav_path = _make_wav(tmp_path / "odd_rate.wav", framerate=22050)
        info = WavInfo(wav_path)
        info.validate()
        assert info.framerate == 22050


# ─── SipCaller tests (mocked pjsua2) ─────────────────────────────────────────


class TestSipCallerNoPjsua2:
    """Test SipCaller behavior when pjsua2 is not available."""

    def test_require_pjsua2_raises(self) -> None:
        with patch("sipstuff.sip_caller.PJSUA2_AVAILABLE", False):
            config = SipCallerConfig(sip=SipConfig(server="pbx", user="u", password="p"))
            with pytest.raises(SipCallError, match="pjsua2.*not available"):
                SipCaller(config)


class TestSipCallerMocked:
    """Test SipCaller with fully mocked pjsua2 module."""

    @pytest.fixture(autouse=True)
    def _patch_local_addr(self) -> Any:
        with patch("sipstuff.sip_caller._local_address_for", return_value="127.0.0.1"):
            yield

    @pytest.fixture
    def mock_pj(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def config(self) -> SipCallerConfig:
        return SipCallerConfig(sip=SipConfig(server="pbx.test", user="1000", password="pw"))

    def test_start_stop(self, config: SipCallerConfig, mock_pj: MagicMock) -> None:
        with patch("sipstuff.sip_caller.pj", mock_pj), patch("sipstuff.sip_caller.PJSUA2_AVAILABLE", True):
            caller = SipCaller(config)
            caller.start()
            mock_pj.Endpoint.assert_called_once()
            mock_pj.Endpoint().libCreate.assert_called_once()
            mock_pj.Endpoint().libInit.assert_called_once()
            mock_pj.Endpoint().libStart.assert_called_once()

            caller.stop()
            mock_pj.Endpoint().libDestroy.assert_called_once()

    def test_context_manager(self, config: SipCallerConfig, mock_pj: MagicMock) -> None:
        with patch("sipstuff.sip_caller.pj", mock_pj), patch("sipstuff.sip_caller.PJSUA2_AVAILABLE", True):
            with SipCaller(config) as caller:
                assert caller._ep is not None
            # After exit, libDestroy should have been called
            mock_pj.Endpoint().libDestroy.assert_called()

    def test_make_call_requires_start(self, config: SipCallerConfig, mock_pj: MagicMock) -> None:
        with patch("sipstuff.sip_caller.pj", mock_pj), patch("sipstuff.sip_caller.PJSUA2_AVAILABLE", True):
            caller = SipCaller(config)
            with pytest.raises(SipCallError, match="not started"):
                caller.make_call("+491234", "/tmp/test.wav")

    def test_start_applies_stun(self, mock_pj: MagicMock) -> None:
        cfg = SipCallerConfig(
            sip=SipConfig(server="pbx", user="u", password="p"),
            nat=NatConfig(stun_servers=["stun.example.com:3478", "stun2.example.com:3478"]),
        )
        with patch("sipstuff.sip_caller.pj", mock_pj), patch("sipstuff.sip_caller.PJSUA2_AVAILABLE", True):
            caller = SipCaller(cfg)
            caller.start()
            ep_cfg = mock_pj.EpConfig()
            assert ep_cfg.uaConfig.stunServer.append.call_count == 2
            caller.stop()

    def test_start_applies_public_address(self, mock_pj: MagicMock) -> None:
        cfg = SipCallerConfig(
            sip=SipConfig(server="pbx", user="u", password="p"),
            nat=NatConfig(public_address="192.168.1.50"),
        )
        with patch("sipstuff.sip_caller.pj", mock_pj), patch("sipstuff.sip_caller.PJSUA2_AVAILABLE", True):
            caller = SipCaller(cfg)
            caller.start()
            tp_cfg = mock_pj.TransportConfig()
            assert tp_cfg.publicAddress == "192.168.1.50"
            acfg = mock_pj.AccountConfig()
            assert acfg.mediaConfig.transportConfig.publicAddress == "192.168.1.50"
            caller.stop()

    def test_start_no_public_address_by_default(self, config: SipCallerConfig, mock_pj: MagicMock) -> None:
        with patch("sipstuff.sip_caller.pj", mock_pj), patch("sipstuff.sip_caller.PJSUA2_AVAILABLE", True):
            caller = SipCaller(config)
            caller.start()
            tp_cfg = mock_pj.TransportConfig()
            # publicAddress should not be set (MagicMock default, not explicitly assigned to a string)
            assert not isinstance(tp_cfg.publicAddress, str) or tp_cfg.publicAddress != "192.168.1.50"
            caller.stop()

    def test_start_applies_ice_turn(self, mock_pj: MagicMock) -> None:
        cfg = SipCallerConfig(
            sip=SipConfig(server="pbx", user="u", password="p"),
            nat=NatConfig(
                ice_enabled=True,
                turn_enabled=True,
                turn_server="turn.example.com:3478",
                turn_username="tu",
                turn_password="tp",
                keepalive_sec=30,
            ),
        )
        with patch("sipstuff.sip_caller.pj", mock_pj), patch("sipstuff.sip_caller.PJSUA2_AVAILABLE", True):
            caller = SipCaller(cfg)
            caller.start()
            acfg = mock_pj.AccountConfig()
            assert acfg.natConfig.iceEnabled is True
            assert acfg.natConfig.turnEnabled is True
            assert acfg.natConfig.turnServer == "turn.example.com:3478"
            assert acfg.natConfig.turnUserName == "tu"
            assert acfg.natConfig.turnPassword == "tp"
            assert acfg.natConfig.udpKaIntervalSec == 30
            caller.stop()
