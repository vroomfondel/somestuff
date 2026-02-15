#!/usr/bin/env python3
"""CLI entry point for making SIP calls with WAV playback or TTS.

Provides the ``python -m sipstuff.cli`` command which registers with a SIP
server, dials a destination, plays a WAV file or piper-TTS-generated speech,
and hangs up.

Example:
    .. code-block:: bash

        python -m sipstuff.cli call --dest +491234567890 --wav alert.wav
        python -m sipstuff.cli call --dest +491234567890 --text "Achtung!"
        python -m sipstuff.cli call --config sip_config.yaml --dest +491234567890 --wav alert.wav
        python -m sipstuff.cli call --server 192.168.123.123 --port 5060 --transport udp --srtp disabled --user sipuser --password sippasword --dest +491234567890 --text "Houston, wir haben ein Problem." --pre-delay 3.0 --post-delay 1.0 --inter-delay 2.1 --repeat 3 -v
"""

import argparse
import os
import sys

from loguru import logger
from tabulate import tabulate

from sipstuff import __version__
from sipstuff.sip_caller import SipCallError, SipCaller
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
    call_parser.add_argument(
        "--inter-delay", dest="inter_delay", type=float, help="Seconds to wait between WAV repeats (default: 0)"
    )
    call_parser.add_argument("--repeat", type=int, help="Number of times to play the WAV (default: 1)")

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
        "inter_delay",
        "repeat",
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
    """CLI entry point: print the startup banner, parse args, and dispatch.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    _print_banner()
    args = parse_args()
    if args.command == "call":
        return cmd_call(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
