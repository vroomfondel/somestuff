"""SIP caller package — place phone calls and play WAV files or TTS via PJSUA2.

Provides a high-level convenience function (``make_sip_call``) for one-shot
calls and a context-manager class (``SipCaller``) for placing multiple calls
on a single SIP registration.  Text-to-speech is handled by piper TTS
(``generate_wav``).

Typical usage::

    from sipstuff import make_sip_call

    make_sip_call(
        server="pbx.local",
        user="1000",
        password="secret",
        destination="+491234567890",
        wav_file="alert.wav",
    )

See ``sipstuff/README.md`` for full CLI, library, and Docker usage examples.
"""

import os
from pathlib import Path

__version__ = "0.1.0"

from sipstuff.sip_caller import SipCallError, SipCaller
from sipstuff.sipconfig import SipCallerConfig, load_config
from sipstuff.tts import TtsError, generate_wav

__all__ = [
    "__version__",
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
    """Convenience function: register, call, play WAV or TTS, and hang up.

    One-shot wrapper around ``SipCaller`` that handles endpoint lifecycle
    and TTS temp-file cleanup automatically.  Provide exactly one of
    ``wav_file`` or ``text`` (not both, not neither).

    Args:
        server: PBX hostname or IP address.
        user: SIP extension / username.
        password: SIP authentication password.
        destination: Phone number or full SIP URI to call.
        wav_file: Path to the WAV file to play on answer.
            Mutually exclusive with ``text``.
        text: Text to synthesize via piper TTS and play on answer.
            Mutually exclusive with ``wav_file``.
        port: SIP server port (default: 5060).
        timeout: Maximum seconds to wait for the remote party to answer.
        transport: SIP transport protocol (``"udp"``, ``"tcp"``, or ``"tls"``).
        pre_delay: Seconds to wait after answer before starting playback.
        post_delay: Seconds to wait after playback completes before hanging up.
        inter_delay: Seconds of silence between WAV repeats.
        repeat: Number of times to play the WAV file.
        tts_model: Piper voice model name for TTS (auto-downloaded on
            first use).

    Returns:
        ``True`` if the call was answered and the WAV played (at least
        partially).  ``False`` if the call was not answered or timed out.

    Raises:
        SipCallError: On SIP registration, transport, or WAV playback errors.
        TtsError: If piper TTS generation fails.
        ValueError: If neither ``wav_file`` nor ``text`` is provided,
            or if both are provided.
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
