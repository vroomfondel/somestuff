#!/usr/bin/env python3
"""CLI entry point for sipstuff: SIP calls, TTS, and STT.

Provides the ``python -m sipstuff.cli`` command with three subcommands:

- **tts**: Generate a WAV file from text using piper TTS.
- **stt**: Transcribe a WAV file to text using faster-whisper.
- **call**: Register with a SIP server, dial a destination, play a WAV file
  or piper-TTS-generated speech, and hang up.

Examples:
    # TTS: generate a WAV file from text
    $ python -m sipstuff.cli tts "Hallo Welt" -o hello.wav
    $ python -m sipstuff.cli tts "Hello World" -o hello.wav --model en_US-lessac-high --sample-rate 8000

    # STT: transcribe a WAV file to text
    $ python -m sipstuff.cli stt recording.wav
    $ python -m sipstuff.cli stt recording.wav --language en --model small --json
    $ python -m sipstuff.cli stt recording.wav --no-vad  # disable Silero VAD pre-filtering

    # Call: place a SIP call
    $ python -m sipstuff.cli call --dest +491234567890 --wav alert.wav
    $ python -m sipstuff.cli call --dest +491234567890 --text "Achtung!"
    $ python -m sipstuff.cli call --dest +491234567890 --wav alert.wav \
        --wait-for-silence 1.0 --record /tmp/recording.wav --transcribe
    $ python -m sipstuff.cli call --server 192.168.123.123 --port 5060 \
        --transport udp --srtp disabled --user sipuser --password sippasword \
        --dest +491234567890 --text "Houston, wir haben ein Problem." \
        --pre-delay 3.0 --post-delay 1.0 --inter-delay 2.1 --repeat 3 -v
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from tabulate import tabulate

from sipstuff import __version__, configure_logging
from sipstuff.sip_caller import SipCaller, SipCallError
from sipstuff.sipconfig import load_config


def _print_banner() -> None:
    """Log a startup banner with version, build time, and project URLs.

    Renders a ``tabulate`` mixed-grid table with a Unicode box-drawing
    title row and emits it via loguru in raw mode.
    """
    startup_rows = [
        ["version", __version__],
        ["buildtime", os.environ.get("BUILDTIME", "n/a")],
        ["github", "https://github.com/vroomfondel/somestuff"],
        ["Docker Hub", "https://hub.docker.com/r/xomoxcc/somestuff"],
    ]
    table_str = tabulate(startup_rows, tablefmt="mixed_grid")
    lines = table_str.split("\n")
    table_width = len(lines[0])
    title = "sipstuff starting up"
    title_border = "\u250d" + "\u2501" * (table_width - 2) + "\u2511"
    title_row = "\u2502 " + title.center(table_width - 4) + " \u2502"
    separator = lines[0].replace("\u250d", "\u251d").replace("\u2511", "\u2525").replace("\u252f", "\u253f")

    logger.opt(raw=True).info(
        "\n{}\n", title_border + "\n" + title_row + "\n" + separator + "\n" + "\n".join(lines[1:])
    )


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the sipstuff CLI.

    Returns:
        Parsed argument namespace.  The ``command`` attribute identifies
        the subcommand (currently only ``"call"``).
    """
    parser = argparse.ArgumentParser(
        prog="sipstuff",
        description="sipstuff — SIP calls, text-to-speech, and speech-to-text",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── tts subcommand ──────────────────────────────────────────────
    tts_parser = sub.add_parser("tts", help="Generate a WAV file from text using piper TTS")
    tts_parser.add_argument("text", help="Text to synthesize")
    tts_parser.add_argument("--output", "-o", required=True, help="Output WAV file path")
    tts_parser.add_argument(
        "--model", "-m", default="de_DE-thorsten-high", help="Piper voice model (default: de_DE-thorsten-high)"
    )
    tts_parser.add_argument(
        "--sample-rate", dest="sample_rate", type=int, default=0, help="Resample output to this rate in Hz (0 = native)"
    )
    tts_parser.add_argument(
        "--data-dir", dest="data_dir", help="Directory for piper voice models (default: ~/.local/share/piper-voices)"
    )
    tts_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging (DEBUG level)")

    # ── stt subcommand ──────────────────────────────────────────────
    stt_parser = sub.add_parser("stt", help="Transcribe a WAV file to text using faster-whisper")
    stt_parser.add_argument("wav", help="Path to WAV file to transcribe")
    stt_parser.add_argument(
        "--model", "-m", help="Whisper model size (default: medium, options: tiny/base/small/medium/large-v3)"
    )
    stt_parser.add_argument("--language", "-l", default="de", help="Language code for transcription (default: de)")
    stt_parser.add_argument("--device", choices=["cpu", "cuda"], help="Compute device (default: cpu)")
    stt_parser.add_argument("--compute-type", dest="compute_type", help="Quantization type (int8/float16/float32)")
    stt_parser.add_argument(
        "--data-dir",
        dest="data_dir",
        help="Directory for Whisper models (default: ~/.local/share/faster-whisper-models)",
    )
    stt_parser.add_argument(
        "--json", dest="json_output", action="store_true", help="Output result as JSON (includes metadata)"
    )
    stt_parser.add_argument(
        "--no-vad", dest="no_vad", action="store_true", help="Disable Silero VAD pre-filtering (VAD is on by default)"
    )
    stt_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging (DEBUG level)")

    # ── call subcommand ─────────────────────────────────────────────
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
    call_parser.add_argument(
        "--inter-delay", dest="inter_delay", type=float, help="Seconds to wait between WAV repeats (default: 0)"
    )
    call_parser.add_argument("--repeat", type=int, help="Number of times to play the WAV (default: 1)")
    call_parser.add_argument(
        "--wait-for-silence",
        dest="wait_for_silence",
        type=float,
        help="Wait for N seconds of remote silence before playback (e.g. 1.0 to let callee finish 'Hello?')",
    )
    call_parser.add_argument("--record", dest="record_path", help="Record remote party audio to this WAV file path")
    call_parser.add_argument(
        "--stt-data-dir",
        dest="stt_data_dir",
        help="Directory for Whisper STT models (default: ~/.local/share/faster-whisper-models)",
    )
    call_parser.add_argument(
        "--stt-model",
        dest="stt_model",
        help="Whisper model size for transcription (default: medium, options: tiny/base/small/medium/large-v3)",
    )
    call_parser.add_argument(
        "--stt-language",
        dest="stt_language",
        default="de",
        help="Language code for STT transcription (default: de)",
    )
    call_parser.add_argument(
        "--transcribe",
        action="store_true",
        default=False,
        help="Transcribe recorded audio via STT and write a JSON call report (requires --record)",
    )

    # NAT traversal
    nat_group = call_parser.add_argument_group("NAT traversal")
    nat_group.add_argument(
        "--stun-servers", dest="stun_servers", help="Comma-separated STUN servers (e.g. stun.l.google.com:19302)"
    )
    nat_group.add_argument("--ice", dest="ice_enabled", action="store_true", default=None, help="Enable ICE for media")
    nat_group.add_argument("--turn-server", dest="turn_server", help="TURN relay server (host:port)")
    nat_group.add_argument("--turn-username", dest="turn_username", help="TURN username")
    nat_group.add_argument("--turn-password", dest="turn_password", help="TURN password")
    nat_group.add_argument(
        "--turn-transport", dest="turn_transport", choices=["udp", "tcp", "tls"], help="TURN transport (default: udp)"
    )
    nat_group.add_argument("--keepalive", dest="keepalive_sec", type=int, help="UDP keepalive interval in seconds")
    nat_group.add_argument(
        "--public-address", dest="public_address", help="Public IP to advertise in SDP/Contact (e.g. K3s node IP)"
    )

    # Logging
    call_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging (DEBUG level)")

    return parser.parse_args()


def cmd_tts(args: argparse.Namespace) -> int:
    """Execute the ``tts`` subcommand.

    Generates a WAV file from text using piper TTS.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    from sipstuff.tts import TtsError, generate_wav

    try:
        result_path = generate_wav(
            text=args.text,
            model=args.model,
            output_path=args.output,
            sample_rate=args.sample_rate,
            data_dir=args.data_dir,
        )
        logger.info(f"WAV written to {result_path}")
        return 0
    except TtsError as exc:
        logger.error(f"TTS failed: {exc}")
        return 1


def cmd_stt(args: argparse.Namespace) -> int:
    """Execute the ``stt`` subcommand.

    Transcribes a WAV file to text using faster-whisper.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    from sipstuff.stt import SttError, transcribe_wav

    try:
        text, meta = transcribe_wav(
            wav_path=args.wav,
            model=args.model,
            language=args.language,
            device=args.device,
            compute_type=args.compute_type,
            data_dir=args.data_dir,
            vad_filter=not args.no_vad,
        )
    except SttError as exc:
        logger.error(f"STT failed: {exc}")
        return 1

    if args.json_output:
        output = {"text": text, **meta}
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print(text)

    return 0


def cmd_call(args: argparse.Namespace) -> int:
    """Execute the ``call`` subcommand.

    Loads configuration (YAML / env / CLI overrides), optionally generates
    a TTS WAV file, places the SIP call, and cleans up temporary files.

    Args:
        args: Parsed CLI arguments from ``parse_args``.

    Returns:
        Exit code: 0 on success, 1 on failure (config error, TTS error,
        SIP error, or unanswered call).
    """

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
        "inter_delay",
        "repeat",
        "wait_for_silence",
        "tts_model",
        "tts_sample_rate",
        "ice_enabled",
        "turn_server",
        "turn_username",
        "turn_password",
        "turn_transport",
        "keepalive_sec",
        "public_address",
    ):
        val = getattr(args, key, None)
        if val is not None:
            overrides[key] = val

    # --stun-servers: comma-separated → list
    if args.stun_servers:
        overrides["stun_servers"] = [s.strip() for s in args.stun_servers.split(",") if s.strip()]
    # --turn-server implies turn_enabled
    if args.turn_server:
        overrides["turn_enabled"] = True

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

    if args.transcribe and not args.record_path:
        logger.error("--transcribe requires --record")
        return 1

    logger.info(f"Calling {args.dest} via {config.sip.server}:{config.sip.port}")

    pjsip_logs: list[str] = []
    try:
        with SipCaller(config) as caller:
            success = caller.make_call(
                args.dest,
                wav_path,
                record_path=args.record_path,
                wait_for_silence=config.call.wait_for_silence or None,
            )
            call_result = caller.last_call_result
            pjsip_logs = caller.get_pjsip_logs()
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

        # Transcribe recording and write JSON report if --transcribe was given
        if args.transcribe and args.record_path and os.path.isfile(args.record_path):
            from sipstuff.stt import SttError, transcribe_wav

            transcript_text: str | None = None
            stt_meta: dict[str, object] = {}
            try:
                transcript_text, stt_meta = transcribe_wav(
                    args.record_path,
                    model=args.stt_model,
                    language=args.stt_language,
                    data_dir=args.stt_data_dir,
                )
                logger.info(f"Transcript: {transcript_text}")
            except SttError as exc:
                logger.error(f"STT transcription failed: {exc}")

            # Build and write JSON call report
            report: dict[str, object] = {
                "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
                "destination": args.dest,
                "wav_file": args.wav or "(tts)",
                "tts_text": args.text,
                "tts_model": args.tts_model,
                "record_path": args.record_path,
                "call_duration": call_result.call_duration if call_result else None,
                "answered": call_result.answered if call_result else None,
                "disconnect_reason": call_result.disconnect_reason if call_result else None,
                "playback": {
                    "repeat": config.call.repeat,
                    "pre_delay": config.call.pre_delay,
                    "post_delay": config.call.post_delay,
                    "inter_delay": config.call.inter_delay,
                    "timeout": config.call.timeout,
                },
                "recording_duration": stt_meta.get("audio_duration"),
                "transcript": transcript_text,
                "stt": {"model": args.stt_model or "medium", **stt_meta},
                "pjsip_log": pjsip_logs,
            }

            report_json = json.dumps(report, indent=2, ensure_ascii=False)
            report_path = Path(args.record_path).with_suffix(".json")
            report_path.write_text(report_json)
            logger.info(f"Call report written to {report_path}")
            logger.opt(raw=True).info(
                "\n{border}\n***** CALL REPORT *****\n{border}\n{report}\n{border}\n",
                border="*" * 60,
                report=report_json,
            )

        return 0
    else:
        logger.warning("Call was not answered or failed")
        return 1


def main() -> int:
    """CLI entry point: print the startup banner, parse args, and dispatch.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """

    args = parse_args()
    if args.verbose:
        os.environ.setdefault("LOGURU_LEVEL", "DEBUG")
        configure_logging()
    else:
        os.environ.setdefault("LOGURU_LEVEL", "INFO")
        configure_logging()

    _print_banner()

    if args.command == "call":
        return cmd_call(args)
    elif args.command == "tts":
        return cmd_tts(args)
    elif args.command == "stt":
        return cmd_stt(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
