"""Text-to-speech using piper CLI (subprocess).

Generates WAV files from text, suitable for SIP playback.
Uses piper-tts installed in a Python 3.12 virtualenv at /opt/piper-venv
(piper-phonemize has no Python 3.14 wheels).
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

# Persistent model cache directory
_PIPER_DATA_DIR = Path(os.getenv("PIPER_DATA_DIR", Path.home() / ".local" / "share" / "piper-voices"))

# Python 3.12 venv containing piper-tts (override paths with env vars)
_PIPER_VENV_BIN = Path(os.getenv("PIPER_BIN", "/opt/piper-venv/bin/piper"))
_PIPER_VENV_PYTHON = Path(os.getenv("PIPER_PYTHON", "/opt/piper-venv/bin/python"))


class TtsError(Exception):
    """Raised when TTS generation fails."""


def _find_piper() -> tuple[str, str]:
    """Find the piper CLI binary and its venv Python.

    Returns:
        (piper_bin, piper_python) paths.
    """
    piper_bin: str | None = None
    if _PIPER_VENV_BIN.is_file():
        piper_bin = str(_PIPER_VENV_BIN)
    else:
        piper_bin = shutil.which("piper")

    if not piper_bin:
        raise TtsError(f"piper CLI not found at {_PIPER_VENV_BIN} or on PATH. " "Install with: pip install piper-tts")

    piper_python: str | None = None
    if _PIPER_VENV_PYTHON.is_file():
        piper_python = str(_PIPER_VENV_PYTHON)
    else:
        piper_python = shutil.which("python3")

    if not piper_python:
        raise TtsError("Python interpreter for piper venv not found")

    return piper_bin, piper_python


def _ensure_model(model: str, data_dir: Path, piper_python: str) -> None:
    """Download voice model if not already present.

    Uses piper.download_voices via the Python 3.12 venv.
    """
    model_path = data_dir / f"{model}.onnx"
    if model_path.exists():
        return

    logger.info(f"TTS: downloading voice model '{model}' (first time only)...")
    try:
        result = subprocess.run(
            [
                piper_python,
                "-c",
                f"from piper.download_voices import download_voice; "
                f"from pathlib import Path; "
                f"download_voice({model!r}, Path({str(data_dir)!r}))",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise TtsError(f"Model download timed out for '{model}'") from exc

    if result.returncode != 0:
        raise TtsError(f"Failed to download voice model '{model}': {result.stderr}")

    if not model_path.exists():
        raise TtsError(f"Model download reported success but {model_path} not found")

    logger.info(f"TTS: model downloaded to {model_path}")


def generate_wav(
    text: str,
    model: str = "de_DE-thorsten-high",
    output_path: str | Path | None = None,
    sample_rate: int = 0,
    data_dir: str | Path | None = None,
) -> Path:
    """Generate a WAV file from text using piper CLI.

    Args:
        text: Text to synthesize.
        model: Piper model name (auto-downloaded on first use).
        output_path: Output WAV path. None = auto-generated temp file.
        sample_rate: Resample to this rate (0 = keep piper native rate).
                     Use 8000 for narrowband SIP or 16000 for wideband.
        data_dir: Directory for voice models. None = PIPER_DATA_DIR env or ~/.local/share/piper-voices.

    Returns:
        Path to the generated WAV file.

    Raises:
        TtsError: If piper is not found or synthesis fails.
    """
    if not text.strip():
        raise TtsError("Empty text provided for TTS")

    piper_bin, piper_python = _find_piper()
    model_dir = Path(data_dir) if data_dir else _PIPER_DATA_DIR

    if output_path is None:
        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="sipstuff_tts_")
        os.close(fd)
        output_path = Path(tmp)
    else:
        output_path = Path(output_path)

    logger.info(f"TTS: generating speech for {len(text)} chars with model '{model}'")

    model_dir.mkdir(parents=True, exist_ok=True)
    _ensure_model(model, model_dir, piper_python)

    cmd = [
        piper_bin,
        "--model",
        model,
        "--data-dir",
        str(model_dir),
        "--output_file",
        str(output_path),
    ]

    try:
        result = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as exc:
        raise TtsError(f"piper binary not found at {piper_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TtsError("piper TTS timed out after 120 seconds") from exc

    if result.returncode != 0:
        raise TtsError(f"piper failed (exit {result.returncode}): {result.stderr}")

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise TtsError("piper produced no output")

    # Resample if requested
    if sample_rate > 0:
        _resample_wav(output_path, sample_rate)

    logger.info(f"TTS: generated {output_path} ({output_path.stat().st_size} bytes)")
    return output_path


def _resample_wav(wav_path: Path, target_rate: int) -> None:
    """Resample a WAV file in-place using ffmpeg."""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise TtsError("ffmpeg not found — required for resampling TTS output")

    tmp_path = wav_path.with_suffix(".tmp.wav")
    result = subprocess.run(
        ["ffmpeg", "-i", str(wav_path), "-ar", str(target_rate), "-ac", "1", "-y", str(tmp_path)],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        raise TtsError(f"ffmpeg resampling failed: {result.stderr.decode(errors='replace')}")

    tmp_path.replace(wav_path)
