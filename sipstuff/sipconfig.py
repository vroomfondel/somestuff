"""Standalone Pydantic configuration for the SIP caller.

Loads configuration from a YAML file, ``SIP_``-prefixed environment variables,
and/or direct Python overrides.  Independent of the main ``somestuff/config.py``
settings system so that ``sipstuff`` can be used as a self-contained package.

Configuration priority (highest first):
    1. ``overrides`` dict passed to ``load_config``
    2. ``SIP_*`` environment variables
    3. YAML config file
"""

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from ruamel.yaml import YAML


class SipConfig(BaseModel):
    """SIP account and server connection settings.

    Attributes:
        server: PBX hostname or IP address.
        port: SIP port (1–65535, default 5060).
        user: SIP extension / username.
        password: SIP authentication password.
        transport: SIP transport protocol (``"udp"``, ``"tcp"``, or ``"tls"``).
        srtp: SRTP media encryption mode
            (``"disabled"``, ``"optional"``, or ``"mandatory"``).
        tls_verify_server: Whether to verify the TLS server certificate.
        local_port: Local bind port for SIP (0 = auto-assigned).
    """

    server: str = Field(description="PBX hostname or IP address")
    port: int = Field(default=5060, ge=1, le=65535, description="SIP port")
    user: str = Field(description="SIP extension / username")
    password: str = Field(description="SIP password")
    transport: Literal["udp", "tcp", "tls"] = Field(default="udp", description="SIP transport protocol")
    srtp: Literal["disabled", "optional", "mandatory"] = Field(default="disabled", description="SRTP media encryption")
    tls_verify_server: bool = Field(default=False, description="Verify TLS server certificate")
    local_port: int = Field(default=0, ge=0, le=65535, description="Local bind port (0 = auto)")


class CallConfig(BaseModel):
    """Call timing and playback behaviour settings.

    Attributes:
        timeout: Maximum seconds to wait for the remote party to answer.
        pre_delay: Seconds to wait after answer before starting WAV playback.
        post_delay: Seconds to wait after playback completes before hanging up.
        inter_delay: Seconds of silence between WAV repeats (only when
            ``repeat > 1``).
        repeat: Number of times to play the WAV file.
        wait_for_silence: Seconds of continuous silence from the remote party
            to wait for before starting playback (0 = disabled).  Applied
            after ``pre_delay``.
    """

    timeout: int = Field(default=60, ge=1, le=600, description="Call timeout in seconds")
    pre_delay: float = Field(default=0.0, ge=0.0, le=30.0, description="Seconds to wait after answer before playback")
    post_delay: float = Field(default=0.0, ge=0.0, le=30.0, description="Seconds to wait after playback before hangup")
    inter_delay: float = Field(
        default=0.0, ge=0.0, le=30.0, description="Seconds to wait between WAV repeats (only when repeat > 1)"
    )
    repeat: int = Field(default=1, ge=1, le=100, description="Number of times to play the WAV file")
    wait_for_silence: float = Field(
        default=0.0,
        ge=0.0,
        le=10.0,
        description="Seconds of remote silence to wait for before playback (0 = disabled)",
    )


class TtsConfig(BaseModel):
    """Piper TTS voice model and output settings.

    Attributes:
        model: Piper voice model name (auto-downloaded on first use).
        sample_rate: Resample TTS output to this rate in Hz.
            0 keeps the native piper rate (~22 050 Hz).  Use 8000 for
            narrowband SIP or 16000 for wideband.
    """

    model: str = Field(default="de_DE-thorsten-high", description="Piper voice model name")
    sample_rate: int = Field(default=0, ge=0, le=48000, description="Resample to this rate (0 = keep native)")


class NatConfig(BaseModel):
    """NAT traversal configuration (STUN, ICE, TURN, keepalive).

    All fields are optional and NAT traversal is disabled by default.
    See the ``sipstuff/README.md`` NAT Traversal section for usage guidance.

    Attributes:
        stun_servers: STUN servers for public IP discovery (``host:port``).
        stun_ignore_failure: Continue startup if STUN is unreachable.
        ice_enabled: Enable ICE connectivity checks for media.
        turn_enabled: Enable TURN relay (requires ``turn_server``).
        turn_server: TURN relay address (``host:port``).
        turn_username: TURN authentication username.
        turn_password: TURN authentication password.
        turn_transport: TURN transport protocol
            (``"udp"``, ``"tcp"``, or ``"tls"``).
        keepalive_sec: UDP keepalive interval in seconds (0 = disabled).
        public_address: Public IP to advertise in SDP ``c=`` and SIP
            Contact headers.  Overrides auto-detected local IP while the
            socket stays bound to the actual local interface.
    """

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
        """Validate that ``turn_server`` is set when ``turn_enabled`` is ``True``.

        Raises:
            ValueError: If TURN is enabled without a server address.
        """
        if self.turn_enabled and not self.turn_server:
            raise ValueError("turn_enabled requires turn_server to be set")
        return self


class SipCallerConfig(BaseModel):
    """Top-level SIP caller configuration aggregating all sub-configs.

    Accepts either a nested dict (``{"sip": {...}, "call": {...}}``) or a
    flat dict with SIP field names at the top level.  The
    ``_flatten_sip_fields`` validator reshapes flat dicts into the nested
    form before Pydantic validation.

    Attributes:
        sip: SIP account and server connection settings.
        call: Call timing and playback behaviour (defaults apply).
        tts: Piper TTS voice model settings (defaults apply).
        nat: NAT traversal settings (disabled by default).
    """

    sip: SipConfig
    call: CallConfig = CallConfig()
    tts: TtsConfig = TtsConfig()
    nat: NatConfig = NatConfig()

    @model_validator(mode="before")
    @classmethod
    def _flatten_sip_fields(cls, data: Any) -> Any:
        """Reshape a flat dict into the nested ``{sip: …, call: …}`` form.

        Allows callers to pass SIP fields (``server``, ``port``, …) at the
        top level instead of nesting them under a ``"sip"`` key.  TTS fields
        ``tts_model`` and ``tts_sample_rate`` are mapped to ``tts.model``
        and ``tts.sample_rate``.  NAT fields are grouped under ``"nat"``.

        Args:
            data: Raw input data (dict or other).  Non-dict values are
                returned unchanged.

        Returns:
            The (possibly restructured) dict ready for Pydantic validation.
        """
        if not isinstance(data, dict):
            return data
        # Already has nested 'sip' key — use as-is
        if "sip" in data:
            return data
        # Try to build from flat keys (CLI / env var usage)
        sip_keys = {"server", "port", "user", "password", "transport", "srtp", "tls_verify_server", "local_port"}
        if sip_keys & set(data.keys()):
            sip_data = {k: data.pop(k) for k in list(data.keys()) if k in sip_keys}
            call_keys = {"timeout", "pre_delay", "post_delay", "inter_delay", "repeat", "wait_for_silence"}
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
    """Load a ``SipCallerConfig`` by merging YAML, environment variables, and overrides.

    Sources are applied in order (later wins):
        1. YAML config file (if ``config_path`` is given and exists).
        2. ``SIP_*`` environment variables (see ``sipstuff/README.md``).
        3. ``overrides`` dict (e.g. from CLI arguments).

    Args:
        config_path: Path to a YAML configuration file.  ``None`` skips
            file loading.
        overrides: Key/value overrides applied on top of file and env
            values.  Keys may be flat SIP field names (``"server"``,
            ``"timeout"``, ``"tts_model"``, …) or NAT field names
            (``"stun_servers"``, ``"ice_enabled"``, …).

    Returns:
        A fully validated ``SipCallerConfig`` instance.

    Raises:
        pydantic.ValidationError: If required fields are missing or
            values fail validation.
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
        "SIP_WAIT_FOR_SILENCE": ("call", "wait_for_silence"),
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
            elif key in ("timeout", "pre_delay", "post_delay", "inter_delay", "repeat", "wait_for_silence"):
                data.setdefault("call", {})[key] = val
            elif key == "tts_model":
                data.setdefault("tts", {})["model"] = val
            elif key == "tts_sample_rate":
                data.setdefault("tts", {})["sample_rate"] = val
            elif key in nat_override_keys:
                data.setdefault("nat", {})[key] = val

    return SipCallerConfig(**data)
