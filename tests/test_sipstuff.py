"""Tests for sipstuff: config validation, WAV validation, mocked call logic."""

import os
import struct
import tempfile
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sipstuff.sip_caller import SipCallError, SipCaller, WavInfo
from sipstuff.sipconfig import CallConfig, SipCallerConfig, SipConfig, load_config

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
