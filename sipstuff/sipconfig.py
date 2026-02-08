"""Standalone Pydantic configuration for SIP caller.

Loads config from YAML file, environment variables (SIP_ prefix), or direct init.
Independent of the main somestuff config.py.
"""

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from ruamel.yaml import YAML


class SipConfig(BaseModel):
    """SIP account / server configuration."""

    server: str = Field(description="PBX hostname or IP address")
    port: int = Field(default=5060, ge=1, le=65535, description="SIP port")
    user: str = Field(description="SIP extension / username")
    password: str = Field(description="SIP password")
    transport: Literal["udp", "tcp", "tls"] = Field(default="udp", description="SIP transport protocol")
    srtp: Literal["disabled", "optional", "mandatory"] = Field(default="disabled", description="SRTP media encryption")
    tls_verify_server: bool = Field(default=False, description="Verify TLS server certificate")
    local_port: int = Field(default=0, ge=0, le=65535, description="Local bind port (0 = auto)")


class CallConfig(BaseModel):
    """Call behavior configuration."""

    timeout: int = Field(default=60, ge=1, le=600, description="Call timeout in seconds")
    pre_delay: float = Field(default=0.0, ge=0.0, le=30.0, description="Seconds to wait after answer before playback")
    post_delay: float = Field(default=0.0, ge=0.0, le=30.0, description="Seconds to wait after playback before hangup")
    inter_delay: float = Field(
        default=0.0, ge=0.0, le=30.0, description="Seconds to wait between WAV repeats (only when repeat > 1)"
    )
    repeat: int = Field(default=1, ge=1, le=100, description="Number of times to play the WAV file")


class TtsConfig(BaseModel):
    """Piper TTS configuration."""

    model: str = Field(default="de_DE-thorsten-high", description="Piper voice model name")
    sample_rate: int = Field(default=0, ge=0, le=48000, description="Resample to this rate (0 = keep native)")


class NatConfig(BaseModel):
    """NAT traversal configuration (all fields optional, disabled by default)."""

    stun_servers: list[str] = Field(default_factory=list, description="STUN servers (host:port)")
    stun_ignore_failure: bool = Field(default=True, description="Continue startup if STUN unreachable")
    ice_enabled: bool = Field(default=False, description="Enable ICE for media NAT traversal")
    turn_enabled: bool = Field(default=False, description="Enable TURN relay")
    turn_server: str = Field(default="", description="TURN server (host:port)")
    turn_username: str = Field(default="", description="TURN auth username")
    turn_password: str = Field(default="", description="TURN auth password")
    turn_transport: Literal["udp", "tcp", "tls"] = Field(default="udp", description="TURN transport")
    keepalive_sec: int = Field(default=0, ge=0, le=600, description="UDP keepalive interval (0 = disabled)")
    public_address: str = Field(
        default="",
        description="Public IP to advertise in SDP/Contact (e.g. K3s node IP). "
        "Overrides auto-detected local IP in signaling and media headers while keeping socket binding to the actual local interface.",
    )

    @model_validator(mode="after")
    def _check_turn(self) -> "NatConfig":
        if self.turn_enabled and not self.turn_server:
            raise ValueError("turn_enabled requires turn_server to be set")
        return self


class SipCallerConfig(BaseModel):
    """Top-level SIP caller configuration."""

    sip: SipConfig
    call: CallConfig = CallConfig()
    tts: TtsConfig = TtsConfig()
    nat: NatConfig = NatConfig()

    @model_validator(mode="before")
    @classmethod
    def _flatten_sip_fields(cls, data: Any) -> Any:
        """Allow flat dict with sip.* fields alongside nested form."""
        if not isinstance(data, dict):
            return data
        # Already has nested 'sip' key — use as-is
        if "sip" in data:
            return data
        # Try to build from flat keys (CLI / env var usage)
        sip_keys = {"server", "port", "user", "password", "transport", "srtp", "tls_verify_server", "local_port"}
        if sip_keys & set(data.keys()):
            sip_data = {k: data.pop(k) for k in list(data.keys()) if k in sip_keys}
            call_keys = {"timeout", "pre_delay", "post_delay", "inter_delay", "repeat"}
            call_data = {k: data.pop(k) for k in list(data.keys()) if k in call_keys}
            tts_data: dict[str, Any] = {}
            for k in list(data.keys()):
                if k == "tts_model":
                    tts_data["model"] = data.pop(k)
                elif k == "tts_sample_rate":
                    tts_data["sample_rate"] = data.pop(k)
            nat_keys = {
                "stun_servers",
                "stun_ignore_failure",
                "ice_enabled",
                "turn_enabled",
                "turn_server",
                "turn_username",
                "turn_password",
                "turn_transport",
                "keepalive_sec",
                "public_address",
            }
            nat_data = {k: data.pop(k) for k in list(data.keys()) if k in nat_keys}
            data["sip"] = sip_data
            if call_data:
                data["call"] = call_data
            if tts_data:
                data["tts"] = tts_data
            if nat_data:
                data["nat"] = nat_data
        return data


def load_config(
    config_path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> SipCallerConfig:
    """Load SipCallerConfig from YAML file, environment variables, and overrides.

    Priority (highest first): overrides > env vars > YAML file.
    """
    data: dict[str, Any] = {}

    # 1. YAML file
    if config_path is not None:
        path = Path(config_path)
        if path.is_file():
            loaded = YAML().load(path)
            if isinstance(loaded, dict):
                data = loaded

    # 2. Environment variables (SIP_ prefix)
    env_map = {
        "SIP_SERVER": ("sip", "server"),
        "SIP_PORT": ("sip", "port"),
        "SIP_USER": ("sip", "user"),
        "SIP_PASSWORD": ("sip", "password"),
        "SIP_TRANSPORT": ("sip", "transport"),
        "SIP_SRTP": ("sip", "srtp"),
        "SIP_TLS_VERIFY_SERVER": ("sip", "tls_verify_server"),
        "SIP_LOCAL_PORT": ("sip", "local_port"),
        "SIP_TIMEOUT": ("call", "timeout"),
        "SIP_PRE_DELAY": ("call", "pre_delay"),
        "SIP_POST_DELAY": ("call", "post_delay"),
        "SIP_INTER_DELAY": ("call", "inter_delay"),
        "SIP_REPEAT": ("call", "repeat"),
        "SIP_TTS_MODEL": ("tts", "model"),
        "SIP_TTS_SAMPLE_RATE": ("tts", "sample_rate"),
        "SIP_STUN_SERVERS": ("nat", "stun_servers"),
        "SIP_STUN_IGNORE_FAILURE": ("nat", "stun_ignore_failure"),
        "SIP_ICE_ENABLED": ("nat", "ice_enabled"),
        "SIP_TURN_ENABLED": ("nat", "turn_enabled"),
        "SIP_TURN_SERVER": ("nat", "turn_server"),
        "SIP_TURN_USERNAME": ("nat", "turn_username"),
        "SIP_TURN_PASSWORD": ("nat", "turn_password"),
        "SIP_TURN_TRANSPORT": ("nat", "turn_transport"),
        "SIP_KEEPALIVE_SEC": ("nat", "keepalive_sec"),
        "SIP_PUBLIC_ADDRESS": ("nat", "public_address"),
    }
    for env_key, (section, field) in env_map.items():
        val = os.getenv(env_key)
        if val is not None:
            if env_key == "SIP_STUN_SERVERS":
                data.setdefault(section, {})[field] = [s.strip() for s in val.split(",") if s.strip()]
            else:
                data.setdefault(section, {})[field] = val

    # 3. Overrides from caller (e.g. CLI args)
    nat_override_keys = {
        "stun_servers",
        "stun_ignore_failure",
        "ice_enabled",
        "turn_enabled",
        "turn_server",
        "turn_username",
        "turn_password",
        "turn_transport",
        "keepalive_sec",
        "public_address",
    }
    if overrides:
        for key, val in overrides.items():
            if val is None:
                continue
            if key in ("server", "port", "user", "password", "transport", "srtp", "tls_verify_server", "local_port"):
                data.setdefault("sip", {})[key] = val
            elif key in ("timeout", "pre_delay", "post_delay", "inter_delay", "repeat"):
                data.setdefault("call", {})[key] = val
            elif key == "tts_model":
                data.setdefault("tts", {})["model"] = val
            elif key == "tts_sample_rate":
                data.setdefault("tts", {})["sample_rate"] = val
            elif key in nat_override_keys:
                data.setdefault("nat", {})[key] = val

    return SipCallerConfig(**data)
