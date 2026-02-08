"""SIP caller module — make phone calls and play WAV files or TTS via PJSUA2."""

import os
from pathlib import Path

from sipstuff.sip_caller import SipCallError, SipCaller
from sipstuff.sipconfig import SipCallerConfig, load_config
from sipstuff.tts import TtsError, generate_wav

__all__ = [
    "make_sip_call",
    "SipCallError",
    "SipCaller",
    "SipCallerConfig",
    "TtsError",
    "generate_wav",
    "load_config",
]


def make_sip_call(
    server: str,
    user: str,
    password: str,
    destination: str,
    wav_file: str | Path | None = None,
    text: str | None = None,
    port: int = 5060,
    timeout: int = 60,
    transport: str = "udp",
    pre_delay: float = 0.0,
    post_delay: float = 0.0,
    inter_delay: float = 0.0,
    repeat: int = 1,
    tts_model: str = "de_DE-thorsten-high",
) -> bool:
    """Convenience function: register, call, play WAV or TTS, hang up.

    Provide either wav_file or text (not both).

    Args:
        server: PBX hostname or IP.
        user: SIP extension / username.
        password: SIP password.
        destination: Phone number or SIP URI.
        wav_file: Path to WAV file to play on answer.
        text: Text to synthesize via piper TTS.
        port: SIP server port.
        timeout: Seconds to wait for answer.
        transport: "udp", "tcp", or "tls".
        pre_delay: Seconds to wait after answer before playback.
        post_delay: Seconds to wait after playback before hangup.
        inter_delay: Seconds to wait between WAV repeats.
        repeat: Number of times to play the WAV.
        tts_model: Piper voice model for TTS.

    Returns:
        True if call was answered and WAV played (at least partially).

    Raises:
        SipCallError: On registration, transport, or WAV issues.
        TtsError: If TTS generation fails.
        ValueError: If neither wav_file nor text is provided.
    """
    if wav_file is None and text is None:
        raise ValueError("Provide either wav_file or text")
    if wav_file is not None and text is not None:
        raise ValueError("Provide either wav_file or text, not both")

    config = load_config(
        overrides={
            "server": server,
            "user": user,
            "password": password,
            "port": port,
            "timeout": timeout,
            "transport": transport,
            "pre_delay": pre_delay,
            "post_delay": post_delay,
            "inter_delay": inter_delay,
            "repeat": repeat,
            "tts_model": tts_model,
        }
    )

    tts_wav_path: str | None = None
    try:
        if text is not None:
            tts_wav_path = str(generate_wav(text=text, model=config.tts.model, sample_rate=config.tts.sample_rate))
            wav_file = tts_wav_path

        with SipCaller(config) as caller:
            return caller.make_call(destination, wav_file)  # type: ignore[arg-type]
    finally:
        if tts_wav_path:
            try:
                os.unlink(tts_wav_path)
            except OSError:
                pass
