#!/usr/bin/env python3
"""CLI for making SIP calls with WAV playback or TTS.

Usage:
    python -m sipstuff.cli call --dest +491234567890 --wav alert.wav
    python -m sipstuff.cli call --dest +491234567890 --text "Achtung! Wasserstand kritisch!"
    python -m sipstuff.cli call --config sip_config.yaml --dest +491234567890 --wav alert.wav
"""

import argparse
import os
import sys

from loguru import logger

from sipstuff.sip_caller import SipCallError, SipCaller
from sipstuff.sipconfig import load_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sipstuff",
        description="SIP caller — place a call and play a WAV file or TTS message",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    call_parser = sub.add_parser("call", help="Place a SIP call")

    # Config source
    call_parser.add_argument("--config", "-c", help="Path to YAML config file")

    # SIP overrides (override config file / env vars)
    call_parser.add_argument("--server", "-s", help="PBX hostname or IP")
    call_parser.add_argument("--port", "-p", type=int, help="SIP port (default: 5060)")
    call_parser.add_argument("--user", "-u", help="SIP extension / username")
    call_parser.add_argument("--password", help="SIP password")
    call_parser.add_argument("--transport", choices=["udp", "tcp", "tls"], help="SIP transport (default: udp)")
    call_parser.add_argument(
        "--srtp", choices=["disabled", "optional", "mandatory"], help="SRTP encryption (default: disabled)"
    )
    call_parser.add_argument(
        "--tls-verify",
        dest="tls_verify_server",
        action="store_true",
        default=None,
        help="Verify TLS server certificate",
    )

    # Audio source (WAV or TTS — at least one required)
    audio_group = call_parser.add_mutually_exclusive_group(required=True)
    audio_group.add_argument("--wav", "-w", help="Path to WAV file to play")
    audio_group.add_argument("--text", help="Text to synthesize via piper TTS")

    # TTS options
    call_parser.add_argument("--tts-model", dest="tts_model", help="Piper voice model (default: de_DE-thorsten-high)")
    call_parser.add_argument(
        "--tts-sample-rate", dest="tts_sample_rate", type=int, help="Resample TTS output to this rate (default: native)"
    )
    call_parser.add_argument(
        "--tts-data-dir",
        dest="tts_data_dir",
        help="Directory for piper voice models (default: ~/.local/share/piper-voices)",
    )

    # Call parameters
    call_parser.add_argument("--dest", "-d", required=True, help="Destination phone number or SIP URI")
    call_parser.add_argument("--timeout", "-t", type=int, help="Call timeout in seconds (default: 60)")
    call_parser.add_argument(
        "--pre-delay", dest="pre_delay", type=float, help="Seconds to wait after answer before playback (default: 0)"
    )
    call_parser.add_argument(
        "--post-delay", dest="post_delay", type=float, help="Seconds to wait after playback before hangup (default: 0)"
    )
    call_parser.add_argument("--repeat", type=int, help="Number of times to play the WAV (default: 1)")

    # Logging
    call_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging (DEBUG level)")

    return parser.parse_args()


def cmd_call(args: argparse.Namespace) -> int:
    if args.verbose:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG")
    else:
        logger.remove()
        logger.add(sys.stderr, level="INFO")

    overrides: dict[str, object] = {}
    for key in (
        "server",
        "port",
        "user",
        "password",
        "transport",
        "srtp",
        "tls_verify_server",
        "timeout",
        "pre_delay",
        "post_delay",
        "repeat",
        "tts_model",
        "tts_sample_rate",
    ):
        val = getattr(args, key, None)
        if val is not None:
            overrides[key] = val

    try:
        config = load_config(config_path=args.config, overrides=overrides)
    except Exception as exc:
        logger.error(f"Configuration error: {exc}")
        return 1

    # Resolve audio source
    wav_path = args.wav
    tts_wav_path: str | None = None

    if args.text:
        from sipstuff.tts import TtsError, generate_wav

        try:
            tts_wav_path = str(
                generate_wav(
                    text=args.text,
                    model=config.tts.model,
                    sample_rate=config.tts.sample_rate,
                    data_dir=args.tts_data_dir,
                )
            )
            wav_path = tts_wav_path
        except TtsError as exc:
            logger.error(f"TTS failed: {exc}")
            return 1

    logger.info(f"Calling {args.dest} via {config.sip.server}:{config.sip.port}")

    try:
        with SipCaller(config) as caller:
            success = caller.make_call(args.dest, wav_path)
    except SipCallError as exc:
        logger.error(f"SIP call failed: {exc}")
        return 1
    finally:
        # Clean up TTS temp file
        if tts_wav_path:
            try:
                os.unlink(tts_wav_path)
            except OSError:
                pass

    if success:
        logger.info("Call completed successfully")
        return 0
    else:
        logger.warning("Call was not answered or failed")
        return 1


def main() -> int:
    args = parse_args()
    if args.command == "call":
        return cmd_call(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
