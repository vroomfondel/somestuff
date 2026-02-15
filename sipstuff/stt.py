"""Speech-to-text transcription using faster-whisper.

Transcribes WAV files (e.g. recorded by ``SipCaller.make_call(record_path=…)``)
to text using CTranslate2-accelerated Whisper models.

Models are auto-downloaded on first use into a persistent cache directory
(default: ``~/.local/share/faster-whisper-models``, override with the
``WHISPER_DATA_DIR`` environment variable).

Environment Variables:
    WHISPER_DATA_DIR: Directory for downloaded Whisper models
        (default: ``~/.local/share/faster-whisper-models``).
    WHISPER_MODEL: Default model size
        (default: ``medium``, options: tiny/base/small/medium/large-v3).
    WHISPER_DEVICE: Compute device (default: ``cpu``, or ``cuda``).
    WHISPER_COMPUTE_TYPE: Quantization type
        (default: ``int8`` for CPU, ``float16`` for CUDA).
"""

import os
from pathlib import Path

from loguru import logger

try:
    from faster_whisper import WhisperModel

    FASTER_WHISPER_AVAILABLE = True
except ImportError:
    WhisperModel = None  # type: ignore[assignment,misc]
    FASTER_WHISPER_AVAILABLE = False

_WHISPER_DATA_DIR = Path(os.getenv("WHISPER_DATA_DIR", Path.home() / ".local" / "share" / "faster-whisper-models"))
_DEFAULT_MODEL = os.getenv("WHISPER_MODEL", "medium")
_DEFAULT_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
_DEFAULT_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "")


class SttError(Exception):
    """Raised when speech-to-text transcription fails."""


def _require_faster_whisper() -> None:
    """Raise ``SttError`` if faster-whisper is not installed."""
    if not FASTER_WHISPER_AVAILABLE:
        raise SttError("faster-whisper not available. Install with: pip install faster-whisper")


def transcribe_wav(
    wav_path: str | Path,
    model: str | None = None,
    language: str = "de",
    device: str | None = None,
    compute_type: str | None = None,
    data_dir: str | Path | None = None,
) -> str:
    """Transcribe a WAV file to text using faster-whisper.

    Args:
        wav_path: Path to the WAV file to transcribe.
        model: Whisper model size (``tiny``, ``base``, ``small``,
            ``medium``, ``large-v3``).  ``None`` uses the
            ``WHISPER_MODEL`` env var or ``"medium"``.
        language: Language code for transcription (e.g. ``"de"``, ``"en"``).
        device: Compute device (``"cpu"`` or ``"cuda"``).
            ``None`` uses ``WHISPER_DEVICE`` env var or ``"cpu"``.
        compute_type: Quantization type (``"int8"``, ``"float16"``,
            ``"float32"``).  ``None`` auto-selects based on device.
        data_dir: Directory for model cache.
            ``None`` uses ``WHISPER_DATA_DIR`` env var or
            ``~/.local/share/faster-whisper-models``.

    Returns:
        The transcribed text.

    Raises:
        SttError: If faster-whisper is not installed, the WAV file
            does not exist, or transcription fails.
    """
    _require_faster_whisper()

    wav_path = Path(wav_path).resolve()
    if not wav_path.is_file():
        raise SttError(f"WAV file not found: {wav_path}")

    model = model or _DEFAULT_MODEL
    device = device or _DEFAULT_DEVICE
    if compute_type is None:
        compute_type = _DEFAULT_COMPUTE_TYPE or ("float16" if device == "cuda" else "int8")
    model_dir = Path(data_dir) if data_dir else _WHISPER_DATA_DIR
    model_dir.mkdir(parents=True, exist_ok=True)

    log = logger.bind(classname="STT")
    log.info(f"Transcribing {wav_path.name} (model={model}, lang={language}, device={device}, compute={compute_type})")

    try:
        whisper = WhisperModel(model, device=device, compute_type=compute_type, download_root=str(model_dir))
        segments, info = whisper.transcribe(str(wav_path), language=language)
        text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
    except Exception as exc:
        raise SttError(f"Transcription failed: {exc}") from exc

    log.info(
        f"Transcribed {info.duration:.1f}s audio ({language}, p={info.language_probability:.2f}): {len(text)} chars"
    )
    return text
