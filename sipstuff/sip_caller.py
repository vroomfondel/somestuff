"""Core SIP calling logic using PJSUA2.

Provides SipCaller (high-level, context-manager) and SipCall (PJSUA2 callback handler).

PJSIP Log Routing:
    Native PJSIP log output is captured by a ``pj.LogWriter`` subclass and
    forwarded to loguru (``classname="pjsip"``).  Two levels control verbosity:

    * **pjsip_log_level** -- verbosity passed to the loguru writer
      (0 = none … 6 = trace, default: 3).
    * **pjsip_console_level** -- native PJSIP console output that goes
      directly to stdout in addition to the writer (default: 4, matching
      PJSIP's own default).

    Resolution order (highest priority first):
        1. Constructor argument (``pjsip_log_level=…``, ``pjsip_console_level=…``)
        2. Environment variable (``PJSIP_LOG_LEVEL``, ``PJSIP_CONSOLE_LEVEL``)
        3. Class default (``SipCaller.DEFAULT_PJSIP_LOG_LEVEL``,
           ``SipCaller.DEFAULT_PJSIP_CONSOLE_LEVEL``)

    Set ``PJSIP_CONSOLE_LEVEL=0`` (or pass ``pjsip_console_level=0``) to
    suppress native console output and rely solely on the loguru writer.
"""

import array
import dataclasses
import math
import os
import statistics
import socket
import threading
import time
import wave
from pathlib import Path
from typing import Any

from loguru import logger

from sipstuff.sipconfig import SipCallerConfig

try:
    import pjsua2 as pj

    PJSUA2_AVAILABLE = True
except ImportError:
    pj = None  # type: ignore[assignment]
    PJSUA2_AVAILABLE = False


@dataclasses.dataclass
class CallResult:
    """Result of a SIP call placed by ``SipCaller.make_call``.

    Attributes:
        success: Whether the call was answered and playback started.
        call_start: Epoch timestamp when the call was initiated.
        call_end: Epoch timestamp when the call finished.
        call_duration: Wall-clock duration of the call in seconds.
        answered: Whether the remote party answered.
        disconnect_reason: SIP disconnect reason string from PJSIP.
    """

    success: bool
    call_start: float
    call_end: float
    call_duration: float
    answered: bool
    disconnect_reason: str


class SipCallError(Exception):
    """Raised on SIP call errors (registration, transport, WAV issues)."""


class WavInfo:
    """WAV file metadata extracted via the ``wave`` module.

    Reads channel count, sample width, framerate, frame count, and
    computed duration on construction.

    Args:
        path: Path to the WAV file.

    Raises:
        SipCallError: If the file does not exist or cannot be parsed.

    Attributes:
        path: Resolved absolute path to the WAV file.
        channels: Number of audio channels.
        sample_width: Sample width in bytes (2 = 16-bit).
        framerate: Sample rate in Hz.
        n_frames: Total number of audio frames.
        duration: Duration in seconds.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise SipCallError(f"WAV file not found: {self.path}")

        try:
            with wave.open(str(self.path), "rb") as wf:
                self.channels: int = wf.getnchannels()
                self.sample_width: int = wf.getsampwidth()
                self.framerate: int = wf.getframerate()
                self.n_frames: int = wf.getnframes()
                self.duration: float = self.n_frames / self.framerate if self.framerate else 0.0
        except wave.Error as exc:
            raise SipCallError(f"Cannot read WAV file {self.path}: {exc}") from exc

    def validate(self) -> None:
        """Log warnings for non-standard WAV formats without blocking playback.

        Warns on non-16-bit samples, stereo, or unusual sample rates.
        Always logs a summary line with file name, duration, and format.
        """
        if self.sample_width != 2:
            logger.warning(f"WAV sample width is {self.sample_width * 8}-bit, expected 16-bit PCM")
        if self.channels != 1:
            logger.warning(f"WAV has {self.channels} channels, expected mono")
        if self.framerate not in (8000, 16000, 44100, 48000):
            logger.warning(f"WAV sample rate is {self.framerate} Hz, typical SIP rates: 8000 or 16000 Hz")
        logger.info(
            f"WAV: {self.path.name} — {self.duration:.1f}s, {self.framerate}Hz, {self.channels}ch, {self.sample_width * 8}bit"
        )


def _local_address_for(remote_host: str, remote_port: int = 5060) -> str:
    """Return the local IP address that the OS would use to reach *remote_host*.

    Opens a UDP socket and connects (no data sent) so the kernel selects
    the correct source address based on the routing table.  This avoids
    multi-homed hosts advertising the wrong IP in SDP.

    Args:
        remote_host: Hostname or IP of the remote SIP server.
        remote_port: Port on the remote host (default 5060).

    Returns:
        Local IP address string selected by the OS routing table.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((remote_host, remote_port))
        return s.getsockname()[0]


def _require_pjsua2() -> None:
    """Raise ``SipCallError`` if the pjsua2 Python bindings are not installed.

    Raises:
        SipCallError: If the ``pjsua2`` module could not be imported.
    """
    if not PJSUA2_AVAILABLE:
        raise SipCallError(
            "pjsua2 Python bindings not available. "
            "Install PJSIP with Python bindings — see sipstuff/install_pjsip.sh"
        )


class SilenceDetector(pj.AudioMediaPort if PJSUA2_AVAILABLE else object):  # type: ignore[misc]
    """PJSUA2 audio port that monitors incoming RMS energy and signals when
    continuous silence exceeds a configurable duration.

    Attach to the call's audio media via ``startTransmit`` to receive remote-
    party audio frames.  The ``silence_event`` is set once the incoming RMS
    stays below ``threshold`` for ``duration`` seconds.

    Note:
        PJSUA2 SWIG bindings expose ``MediaFrame.buf`` as a ``pj.ByteVector``
        (C++ ``std::vector<unsigned char>``), **not** Python ``bytes``.
        ``array.frombytes()`` requires a bytes-like object, so an explicit
        ``bytes()`` conversion is needed.

    Args:
        duration: Required seconds of continuous silence (default: 1.0).
        threshold: RMS threshold below which audio is considered silence
            (16-bit PCM scale, default: 200).
    """

    def __init__(self, duration: float = 1.0, threshold: int = 200) -> None:
        super().__init__()
        self._duration = duration
        self._threshold = threshold
        self._silence_start: float | None = None
        self._last_log: float = 0.0
        self._rms_buf: list[int] = []
        self.silence_event = threading.Event()
        self._log = logger.bind(classname="SilenceDetector")

    def _flush_rms_stats(self, now: float, label: str) -> None:
        """Log buffered RMS stats (avg/median/stddev) and reset the buffer."""
        if not self._rms_buf:
            return
        avg = statistics.mean(self._rms_buf)
        med = statistics.median(self._rms_buf)
        std = statistics.stdev(self._rms_buf) if len(self._rms_buf) > 1 else 0.0
        self._log.info(f"{label} (n={len(self._rms_buf)}, avg={avg:.0f}, med={med:.0f}, std={std:.0f})")
        self._rms_buf.clear()
        self._last_log = now

    def onFrameReceived(self, frame: "pj.MediaFrame") -> None:  # noqa: N802
        """Called by PJSUA2 for every incoming audio frame (~20 ms)."""
        if self.silence_event.is_set():
            return

        try:
            samples = array.array("h")
            samples.frombytes(bytes(frame.buf))  # ByteVector -> bytes for array.frombytes()
            if len(samples) == 0:
                return
            rms = math.isqrt(sum(s * s for s in samples) // len(samples))
        except Exception:
            return

        now = time.monotonic()
        self._rms_buf.append(rms)
        if rms < self._threshold:
            if self._silence_start is None:
                self._silence_start = now
            elif now - self._silence_start >= self._duration:
                self._flush_rms_stats(now, "Silence detected")
                self._log.info(f"Silence threshold reached ({self._duration}s, last_rms={rms})")
                self.silence_event.set()
        else:
            self._silence_start = None

        if now - self._last_log >= 0.5:
            # log not more often than 0.5s
            self._flush_rms_stats(now, "Audio activity")


class SipCall(pj.Call if PJSUA2_AVAILABLE else object):  # type: ignore[misc]
    """PJSUA2 Call subclass with callbacks for state changes and media.

    Exposes threading events so the caller can synchronously wait for
    connection, media readiness, or disconnection.

    Args:
        account: The PJSUA2 account that owns this call.
        call_id: Existing PJSUA2 call ID, or ``PJSUA_INVALID_ID`` for a new
            outgoing call.

    Attributes:
        connected_event: Set when the call enters CONFIRMED state.
        disconnected_event: Set when the call enters DISCONNECTED state.
        media_ready_event: Set when an active audio media channel is available.
        wav_player: The active ``AudioMediaPlayer``, or ``None``.
        audio_recorder: The active ``AudioMediaRecorder``, or ``None``.
    """

    def __init__(self, account: "pj.Account", call_id: int = pj.PJSUA_INVALID_ID if PJSUA2_AVAILABLE else -1) -> None:
        if PJSUA2_AVAILABLE:
            pj.Call.__init__(self, account, call_id)
        self.connected_event = threading.Event()
        self.disconnected_event = threading.Event()
        self.media_ready_event = threading.Event()
        self.wav_player: pj.AudioMediaPlayer | None = None
        self.audio_recorder: pj.AudioMediaRecorder | None = None
        self._wav_path: str | None = None
        self._record_path: str | None = None
        self._audio_media: Any = None
        self._account = account
        self._disconnect_reason: str = ""
        self._autoplay: bool = True

    def set_record_path(self, record_path: str | None) -> None:
        """Configure the output WAV path for recording remote-party audio.

        Args:
            record_path: Path to the output WAV file, or ``None`` to disable recording.
        """
        self._record_path = record_path

    def set_wav_path(self, wav_path: str | None, autoplay: bool = True) -> None:
        """Configure the WAV file to play and whether to start on media ready.

        Args:
            wav_path: Path to the WAV file, or ``None`` to disable playback.
            autoplay: If ``True``, playback starts automatically when the
                media channel becomes active.  Set to ``False`` when
                ``SipCaller.make_call`` manages playback timing.
        """
        self._wav_path = wav_path
        self._autoplay = autoplay

    def onCallState(self, prm: "pj.OnCallStateParam") -> None:  # noqa: N802
        """PJSUA2 callback invoked on call state changes.

        Sets ``connected_event`` on CONFIRMED and ``disconnected_event``
        on DISCONNECTED.  On disconnect, ``connected_event`` is also set
        to unblock any thread waiting for an answer.

        Args:
            prm: PJSUA2 call-state callback parameter (unused directly).
        """
        ci = self.getInfo()
        logger.info(f"Call state: {ci.stateText} (last code: {ci.lastStatusCode})")

        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            self.connected_event.set()
        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self._disconnect_reason = ci.lastReason
            self.disconnected_event.set()
            self.connected_event.set()  # unblock waiters

    def onCallMediaState(self, prm: "pj.OnCallMediaStateParam") -> None:  # noqa: N802
        """PJSUA2 callback invoked when media state changes.

        Finds the first active audio media channel, stores it, sets
        ``media_ready_event``, starts recording if a record path is
        configured, and optionally starts WAV playback if ``autoplay``
        is enabled.

        Args:
            prm: PJSUA2 media-state callback parameter (unused directly).
        """
        ci = self.getInfo()
        for mi in ci.media:
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                self._audio_media = self.getAudioMedia(mi.index)
                self.media_ready_event.set()
                if self._record_path:
                    self.start_recording()
                if self._autoplay and self._wav_path:
                    self.play_wav()
                break

    def play_wav(self) -> bool:
        """Start playing the configured WAV file in loop mode.

        The player is created once and loops continuously.  Repeat
        count and timing are managed by ``SipCaller.make_call``; the loop
        keeps the conference port alive for clean ``stopTransmit`` teardown.

        Returns:
            ``True`` if playback started successfully, ``False`` if no WAV
            path or audio media is configured, or if an error occurred.
        """
        if not self._wav_path or not self._audio_media:
            return False
        try:
            if self.wav_player is None:
                self.wav_player = pj.AudioMediaPlayer()
                self.wav_player.createPlayer(self._wav_path)
                self.wav_player.startTransmit(self._audio_media)
            logger.info(f"Playing WAV: {self._wav_path}")
            return True
        except Exception as exc:
            logger.error(f"Failed to play WAV: {exc}")
            return False

    def stop_wav(self, _orphan_store: list[Any] | None = None) -> None:
        """Stop current WAV playback and disconnect from the conference bridge.

        If ``_orphan_store`` is provided the player object is moved there
        instead of being destroyed immediately -- this avoids the PJSIP
        "Remove port failed" warning that occurs when CPython's ref-counting
        triggers the C++ destructor while the conference bridge is still
        active.  ``SipCaller.stop`` clears the store before ``libDestroy``
        when cleanup is safe.

        Args:
            _orphan_store: Optional list to receive the detached player
                reference for deferred destruction.
        """
        if self.wav_player is not None:
            if self._audio_media is not None:
                try:
                    self.wav_player.stopTransmit(self._audio_media)
                except Exception:
                    pass
            if _orphan_store is not None:
                _orphan_store.append(self.wav_player)
            self.wav_player = None

    def start_recording(self) -> bool:
        """Start recording remote-party audio to the configured WAV file.

        Creates an ``AudioMediaRecorder`` and connects the call's audio
        media to it (reverse direction of playback).

        Returns:
            ``True`` if recording started successfully, ``False`` if no
            record path or audio media is configured, or if an error occurred.
        """
        if not self._record_path or not self._audio_media:
            return False
        try:
            if self.audio_recorder is None:
                self.audio_recorder = pj.AudioMediaRecorder()
                self.audio_recorder.createRecorder(self._record_path)
                self._audio_media.startTransmit(self.audio_recorder)
            logger.info(f"Recording remote audio to: {self._record_path}")
            return True
        except Exception as exc:
            logger.error(f"Failed to start recording: {exc}")
            return False

    def stop_recording(self, _orphan_store: list[Any] | None = None) -> None:
        """Stop recording and disconnect the recorder from the conference bridge.

        Uses the same orphan pattern as ``stop_wav`` to avoid the PJSIP
        conference bridge teardown race.

        Args:
            _orphan_store: Optional list to receive the detached recorder
                reference for deferred destruction.
        """
        if self.audio_recorder is not None:
            if self._audio_media is not None:
                try:
                    self._audio_media.stopTransmit(self.audio_recorder)
                except Exception:
                    pass
            if _orphan_store is not None:
                _orphan_store.append(self.audio_recorder)
            self.audio_recorder = None


class _PjLogWriter(pj.LogWriter if PJSUA2_AVAILABLE else object):  # type: ignore[misc]
    """PJSUA2 ``LogWriter`` subclass that routes native PJSIP log output through loguru.

    Maps PJSIP log levels (1 = error … 6 = trace) to loguru level names
    and emits each message via a loguru logger bound to ``classname="pjsip"``.
    """

    _PJ_TO_LOGURU = {1: "ERROR", 2: "WARNING", 3: "INFO", 4: "DEBUG", 5: "TRACE", 6: "TRACE"}

    def __init__(self) -> None:
        if PJSUA2_AVAILABLE:
            pj.LogWriter.__init__(self)
        self._log = logger.bind(classname="pjsip")
        self._buffer: list[str] = []

    def write(self, entry: "pj.LogEntry") -> None:
        """Forward a single PJSIP log entry to loguru and capture it.

        Args:
            entry: PJSIP log entry containing ``level`` (int) and ``msg`` (str).
        """
        level = self._PJ_TO_LOGURU.get(entry.level, "DEBUG")
        msg = entry.msg.rstrip("\n")
        if msg:
            self._buffer.append(msg)
            self._log.log(level, "{}", msg)


class SipCaller:
    """High-level SIP caller with context-manager support.

    Wraps PJSUA2 endpoint creation, account registration, call placement,
    and WAV playback into a single context manager.

    Args:
        config: SIP caller configuration (from ``load_config``).
        pjsip_log_level: PJSIP log verbosity routed through loguru
            (0 = none … 6 = trace).  Falls back to the ``PJSIP_LOG_LEVEL``
            env var, then ``DEFAULT_PJSIP_LOG_LEVEL`` (3).
        pjsip_console_level: PJSIP native console output level that prints
            directly to stdout in addition to the loguru writer.  Falls back
            to the ``PJSIP_CONSOLE_LEVEL`` env var, then
            ``DEFAULT_PJSIP_CONSOLE_LEVEL`` (4, matching PJSIP's own default).
            Set to 0 to suppress native console output entirely.

    Attributes:
        DEFAULT_PJSIP_LOG_LEVEL: Class-level default for ``pjsip_log_level`` (3).
        DEFAULT_PJSIP_CONSOLE_LEVEL: Class-level default for ``pjsip_console_level`` (4).

    Examples:
        with SipCaller(config) as caller:
            success = caller.make_call("+491234567890", "/path/to/alert.wav")

        # Suppress native console output, bump writer verbosity
        with SipCaller(config, pjsip_log_level=5, pjsip_console_level=0) as caller:
            success = caller.make_call("+491234567890", "/path/to/alert.wav")
    """

    DEFAULT_PJSIP_LOG_LEVEL: int = 3
    DEFAULT_PJSIP_CONSOLE_LEVEL: int = 4

    def __init__(
        self,
        config: SipCallerConfig,
        pjsip_log_level: int | None = None,
        pjsip_console_level: int | None = None,
    ) -> None:
        _require_pjsua2()
        self.config = config
        self.pjsip_log_level: int = (
            pjsip_log_level
            if pjsip_log_level is not None
            else int(os.environ.get("PJSIP_LOG_LEVEL", str(self.DEFAULT_PJSIP_LOG_LEVEL)))
        )
        self.pjsip_console_level: int = (
            pjsip_console_level
            if pjsip_console_level is not None
            else int(os.environ.get("PJSIP_CONSOLE_LEVEL", str(self.DEFAULT_PJSIP_CONSOLE_LEVEL)))
        )
        self._ep: pj.Endpoint | None = None
        self._account: pj.Account | None = None
        self._transport: Any = None
        self._orphaned_players: list[Any] = []
        self._pj_log_writer: _PjLogWriter | None = None
        self._log = logger.bind(classname="SipCaller")
        self.last_call_result: CallResult | None = None

    def __enter__(self) -> "SipCaller":
        """Start the PJSUA2 endpoint and return ``self`` for use as a context manager."""
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        """Shut down the PJSUA2 endpoint on context-manager exit."""
        self.stop()

    def start(self) -> None:
        """Initialize the PJSUA2 endpoint, create a SIP transport, and register an account.

        Performs local-IP detection via ``_local_address_for`` and binds both
        SIP signaling and RTP media transports to that address.  Configures
        STUN/ICE/TURN/keepalive per ``self.config.nat`` and SRTP per
        ``self.config.sip.srtp``.

        Raises:
            SipCallError: If pjsua2 is unavailable or account registration fails.
        """
        _require_pjsua2()

        self._ep = pj.Endpoint()
        self._ep.libCreate()

        # Determine the local IP that routes to the SIP server so both
        # signaling and media (RTP) sockets bind to the correct interface.
        local_ip = _local_address_for(self.config.sip.server, self.config.sip.port)
        self._log.info(f"Local address for SIP server: {local_ip}")

        ep_cfg = pj.EpConfig()
        ep_cfg.logConfig.level = self.pjsip_log_level
        ep_cfg.logConfig.consoleLevel = self.pjsip_console_level
        self._pj_log_writer = _PjLogWriter()
        ep_cfg.logConfig.writer = self._pj_log_writer
        ep_cfg.logConfig.decor = 0  # skip PJSIP's own timestamp/prefix — loguru adds its own

        # STUN servers (endpoint-level)
        if self.config.nat.stun_servers:
            self._log.info(
                f"STUN servers: {self.config.nat.stun_servers} (ignore failure: {self.config.nat.stun_ignore_failure})"
            )
            for srv in self.config.nat.stun_servers:
                ep_cfg.uaConfig.stunServer.append(srv)
            ep_cfg.uaConfig.stunIgnoreFailure = self.config.nat.stun_ignore_failure

        self._ep.libInit(ep_cfg)

        # Transport(s) — also bound to the correct interface
        tp_cfg = pj.TransportConfig()
        tp_cfg.port = self.config.sip.local_port
        tp_cfg.boundAddress = local_ip
        if self.config.nat.public_address:
            tp_cfg.publicAddress = self.config.nat.public_address
            self._log.info(f"Public address override: {self.config.nat.public_address} (local bind: {local_ip})")

        if self.config.sip.transport == "tls":
            tp_type = pj.PJSIP_TRANSPORT_TLS
            tls_cfg = pj.TlsConfig()
            tls_cfg.method = pj.PJSIP_TLSV1_2_METHOD
            if not self.config.sip.tls_verify_server:
                tls_cfg.verifyServer = False
                tls_cfg.verifyClient = False
            tp_cfg.tlsConfig = tls_cfg
        elif self.config.sip.transport == "tcp":
            tp_type = pj.PJSIP_TRANSPORT_TCP
        else:
            tp_type = pj.PJSIP_TRANSPORT_UDP

        self._transport = self._ep.transportCreate(tp_type, tp_cfg)

        self._ep.libStart()

        # Use null audio device for headless / container operation (no sound card needed)
        self._ep.audDevManager().setNullDev()
        self._log.info("PJSUA2 endpoint started (null audio device)")

        # Account registration — bind to our transport so PJSIP never tries
        # an unsupported transport (avoids PJSIP_EUNSUPTRANSPORT on INVITE).
        acfg = pj.AccountConfig()
        scheme = "sips" if self.config.sip.transport == "tls" else "sip"
        tp_param = f";transport={self.config.sip.transport}"
        acfg.idUri = f"{scheme}:{self.config.sip.user}@{self.config.sip.server}"
        acfg.regConfig.registrarUri = f"{scheme}:{self.config.sip.server}:{self.config.sip.port}{tp_param}"
        acfg.sipConfig.transportId = self._transport

        cred = pj.AuthCredInfo("digest", "*", self.config.sip.user, 0, self.config.sip.password)
        acfg.sipConfig.authCreds.append(cred)

        # Bind RTP/media sockets to the correct interface (avoids SDP
        # advertising the wrong IP on multi-homed hosts).
        acfg.mediaConfig.transportConfig.boundAddress = local_ip
        if self.config.nat.public_address:
            acfg.mediaConfig.transportConfig.publicAddress = self.config.nat.public_address

        # SRTP media encryption
        srtp_map = {
            "disabled": pj.PJMEDIA_SRTP_DISABLED,
            "optional": pj.PJMEDIA_SRTP_OPTIONAL,
            "mandatory": pj.PJMEDIA_SRTP_MANDATORY,
        }
        acfg.mediaConfig.srtpUse = srtp_map[self.config.sip.srtp]
        acfg.mediaConfig.srtpSecureSignaling = 0 if self.config.sip.srtp == "disabled" else 1
        if self.config.sip.srtp != "disabled":
            self._log.info(f"SRTP: {self.config.sip.srtp}")

        # NAT traversal — ICE, TURN, keepalive (account-level)
        nat = self.config.nat
        if not nat.stun_servers and not nat.ice_enabled and not nat.turn_enabled and nat.keepalive_sec == 0:
            self._log.info("NAT traversal: disabled (no STUN/ICE/TURN/keepalive configured)")

        if self.config.nat.ice_enabled:
            self._log.info("ICE enabled for media transport")
            acfg.natConfig.iceEnabled = True

        if self.config.nat.turn_enabled:
            self._log.info(
                f"TURN relay: {self.config.nat.turn_server} (transport: {self.config.nat.turn_transport}, user: {self.config.nat.turn_username})"
            )
            acfg.natConfig.turnEnabled = True
            acfg.natConfig.turnServer = self.config.nat.turn_server
            acfg.natConfig.turnUserName = self.config.nat.turn_username
            acfg.natConfig.turnPassword = self.config.nat.turn_password
            acfg.natConfig.turnPasswordType = 0
            turn_tp = {"udp": pj.PJ_TURN_TP_UDP, "tcp": pj.PJ_TURN_TP_TCP, "tls": pj.PJ_TURN_TP_TLS}
            acfg.natConfig.turnConnType = turn_tp[self.config.nat.turn_transport]

        if self.config.nat.keepalive_sec > 0:
            self._log.info(f"UDP keepalive: {self.config.nat.keepalive_sec}s")
            acfg.natConfig.udpKaIntervalSec = self.config.nat.keepalive_sec
            acfg.natConfig.udpKaData = "\r\n"

        self._account = pj.Account()
        try:
            self._account.create(acfg)
        except Exception as exc:
            self.stop()
            raise SipCallError(f"SIP registration failed: {exc}") from exc

        # Give registration a moment
        time.sleep(1)
        self._log.info(f"SIP account registered: {acfg.idUri}")

    def stop(self) -> None:
        """Shut down the PJSUA2 endpoint and release all resources.

        Shuts down the SIP account, destroys orphaned WAV players while
        the conference bridge is still alive, calls ``libDestroy``, and
        releases the log writer.  Safe to call multiple times.
        """
        if self._account is not None:
            try:
                self._account.shutdown()
            except Exception:
                pass
            self._account = None

        # Destroy orphaned WAV players while the conference bridge
        # (owned by the endpoint) is still alive.
        self._orphaned_players.clear()

        if self._ep is not None:
            try:
                self._ep.libDestroy()
            except Exception:
                pass
            self._ep = None

        # Release log writer after endpoint is gone
        self._pj_log_writer = None

        self._log.info("PJSUA2 endpoint stopped")

    def get_pjsip_logs(self) -> list[str]:
        """Return captured PJSIP log messages."""
        if self._pj_log_writer is not None:
            return list(self._pj_log_writer._buffer)
        return []

    def make_call(
        self,
        destination: str,
        wav_file: str | Path,
        timeout: int | None = None,
        pre_delay: float | None = None,
        post_delay: float | None = None,
        inter_delay: float | None = None,
        repeat: int | None = None,
        record_path: str | Path | None = None,
        wait_for_silence: float | None = None,
    ) -> bool:
        """Place a SIP call, play a WAV file on answer, and hang up after playback.

        Builds a SIP URI from ``destination`` and the configured server,
        initiates the call, waits for an answer (up to ``timeout``), then
        plays the WAV file ``repeat`` times with optional pre/inter/post
        delays.  During inter-delays the WAV transmission is paused so the
        remote side hears silence.

        Args:
            destination: Phone number or SIP URI to call.
            wav_file: Path to the WAV file to play.
            timeout: Call timeout in seconds.  ``None`` uses the config value.
            pre_delay: Seconds to wait after answer before playback.
                ``None`` uses the config value.
            post_delay: Seconds to wait after playback before hangup.
                ``None`` uses the config value.
            inter_delay: Seconds of silence between WAV repeats.
                ``None`` uses the config value.
            repeat: Number of times to play the WAV.
                ``None`` uses the config value.
            record_path: Path to a WAV file for recording remote-party audio.
                ``None`` disables recording.
            wait_for_silence: Seconds of continuous silence from the remote
                party to wait for before starting playback (e.g. wait for
                "Hello?" to finish).  ``None`` or ``0`` disables silence
                detection.  Applied after ``pre_delay``.

        Returns:
            ``True`` if the call was answered and the WAV played (at least
            partially).  ``False`` if the call was not answered or timed out.

        Raises:
            SipCallError: If the caller is not started or the call cannot
                be initiated.
        """
        self.last_call_result = None
        call_start = time.time()

        if self._account is None:
            raise SipCallError("SipCaller not started — call start() or use context manager")

        timeout = timeout if timeout is not None else self.config.call.timeout
        pre_delay = pre_delay if pre_delay is not None else self.config.call.pre_delay
        post_delay = post_delay if post_delay is not None else self.config.call.post_delay
        inter_delay = inter_delay if inter_delay is not None else self.config.call.inter_delay
        repeat = repeat if repeat is not None else self.config.call.repeat
        wait_for_silence = wait_for_silence if wait_for_silence is not None else self.config.call.wait_for_silence

        # Validate WAV
        wav_info = WavInfo(wav_file)
        wav_info.validate()

        # Build SIP URI — always include ;transport= so PJSIP uses the correct
        # transport directly without NAPTR/SRV fallback attempts.
        scheme = "sips" if self.config.sip.transport == "tls" else "sip"
        tp_param = f";transport={self.config.sip.transport}"
        default_port = 5061 if self.config.sip.transport == "tls" else 5060
        if destination.startswith("sip:") or destination.startswith("sips:"):
            sip_uri = destination
        elif self.config.sip.port != default_port:
            sip_uri = f"{scheme}:{destination}@{self.config.sip.server}:{self.config.sip.port}{tp_param}"
        else:
            sip_uri = f"{scheme}:{destination}@{self.config.sip.server}{tp_param}"

        self._log.info(
            f"Calling {sip_uri} (timeout: {timeout}s, repeat: {repeat}x, pre: {pre_delay}s, inter: {inter_delay}s, post: {post_delay}s)"
        )

        # Don't autoplay — we manage playback timing ourselves
        call = SipCall(self._account)
        call.set_wav_path(str(wav_info.path), autoplay=False)
        if record_path is not None:
            resolved_record = Path(record_path).resolve()
            resolved_record.parent.mkdir(parents=True, exist_ok=True)
            call.set_record_path(str(resolved_record))

        prm = pj.CallOpParam(True)
        try:
            call.makeCall(sip_uri, prm)
        except Exception as exc:
            raise SipCallError(f"Failed to initiate call to {sip_uri}: {exc}") from exc

        # Outer try/finally: guarantee the call is hung up even if an
        # unexpected exception occurs anywhere after makeCall.
        try:
            # Wait for answer or timeout
            answered = call.connected_event.wait(timeout=timeout)

            if not answered or call.disconnected_event.is_set():
                reason = call._disconnect_reason or "timeout / no answer"
                self._log.warning(f"Call not answered: {reason}")
                if not call.disconnected_event.is_set():
                    try:
                        call.hangup(pj.CallOpParam())
                    except Exception:
                        pass
                call_end = time.time()
                self.last_call_result = CallResult(
                    success=False,
                    call_start=call_start,
                    call_end=call_end,
                    call_duration=call_end - call_start,
                    answered=False,
                    disconnect_reason=reason,
                )
                return False

            self._log.info("Call answered")

            # Wait for media to be ready
            if not call.media_ready_event.wait(timeout=5):
                self._log.error("Media channel not ready after 5s — hanging up")
                try:
                    call.hangup(pj.CallOpParam())
                except Exception:
                    pass
                call_end = time.time()
                self.last_call_result = CallResult(
                    success=False,
                    call_start=call_start,
                    call_end=call_end,
                    call_duration=call_end - call_start,
                    answered=True,
                    disconnect_reason="media not ready",
                )
                return False

            # Log negotiated media info for diagnostics
            try:
                ci = call.getInfo()
                for mi in ci.media:
                    if mi.type == pj.PJMEDIA_TYPE_AUDIO:
                        self._log.debug(f"Audio media: dir={mi.dir}, status={mi.status}")
            except Exception:
                pass

            # Pre-delay
            if pre_delay > 0:
                self._log.info(f"Pre-delay: {pre_delay}s")
                if call.disconnected_event.wait(timeout=pre_delay):
                    self._log.info("Remote party hung up during pre-delay")
                    call_end = time.time()
                    self.last_call_result = CallResult(
                        success=True,
                        call_start=call_start,
                        call_end=call_end,
                        call_duration=call_end - call_start,
                        answered=True,
                        disconnect_reason=call._disconnect_reason,
                    )
                    return True

            # Wait for silence from remote party before playback (e.g. wait
            # for callee's "Hello?" to finish).
            if wait_for_silence and wait_for_silence > 0 and call._audio_media is not None:
                silence_timeout = min(wait_for_silence + 10.0, timeout)
                self._log.info(f"Waiting for {wait_for_silence}s of silence (timeout: {silence_timeout}s)")
                detector = SilenceDetector(duration=wait_for_silence)
                try:
                    fmt = pj.MediaFormatAudio()
                    fmt.init(pj.PJMEDIA_FORMAT_PCM, 16000, 1, 20000, 16)
                    detector.createPort("silence_det", fmt)
                    call._audio_media.startTransmit(detector)
                    # Wait for silence or disconnect, whichever comes first
                    start_wait = time.monotonic()
                    while not detector.silence_event.is_set() and not call.disconnected_event.is_set():
                        remaining = silence_timeout - (time.monotonic() - start_wait)
                        if remaining <= 0:
                            self._log.warning("Silence wait timed out — proceeding with playback")
                            break
                        detector.silence_event.wait(timeout=min(remaining, 0.25))
                    call._audio_media.stopTransmit(detector)
                except Exception as exc:
                    self._log.warning(f"Silence detection failed ({exc}) — proceeding with playback")

                if call.disconnected_event.is_set():
                    self._log.info("Remote party hung up while waiting for silence")
                    call_end = time.time()
                    self.last_call_result = CallResult(
                        success=True,
                        call_start=call_start,
                        call_end=call_end,
                        call_duration=call_end - call_start,
                        answered=True,
                        disconnect_reason=call._disconnect_reason,
                    )
                    return True

            # Start the looping WAV player once; wait for duration × repeats.
            try:
                call.play_wav()
                for i in range(repeat):
                    if call.disconnected_event.is_set():
                        self._log.info("Remote party hung up during playback")
                        break

                    if repeat > 1:
                        self._log.info(f"Playing WAV pass ({i + 1}/{repeat})")

                    if call.disconnected_event.wait(timeout=wav_info.duration):
                        self._log.info("Remote party hung up during playback")
                        break

                    # Inter-delay between repeats (not after the last one)
                    if inter_delay > 0 and i < repeat - 1:
                        # Pause transmission so the remote side hears silence
                        if call.wav_player is not None and call._audio_media is not None:
                            try:
                                call.wav_player.stopTransmit(call._audio_media)
                            except Exception:
                                pass
                        self._log.info(f"Inter-delay: {inter_delay}s")
                        if call.disconnected_event.wait(timeout=inter_delay):
                            self._log.info("Remote party hung up during inter-delay")
                            break
                        # Resume transmission for the next repeat
                        if call.wav_player is not None and call._audio_media is not None:
                            try:
                                call.wav_player.startTransmit(call._audio_media)
                            except Exception:
                                pass
            finally:
                call.stop_wav(_orphan_store=self._orphaned_players)
                call.stop_recording(_orphan_store=self._orphaned_players)

            # Post-delay (skip if remote already hung up)
            if not call.disconnected_event.is_set() and post_delay > 0:
                self._log.info(f"Post-delay: {post_delay}s")
                if call.disconnected_event.wait(timeout=post_delay):
                    self._log.info("Remote party hung up during post-delay")

            # Hang up (if still connected)
            if not call.disconnected_event.is_set():
                self._log.info("Playback completed, hanging up")
                try:
                    call.hangup(pj.CallOpParam())
                except Exception:
                    pass
                call.disconnected_event.wait(timeout=5)

            call_end = time.time()
            self.last_call_result = CallResult(
                success=True,
                call_start=call_start,
                call_end=call_end,
                call_duration=call_end - call_start,
                answered=True,
                disconnect_reason=call._disconnect_reason,
            )
            return True
        finally:
            # Failsafe: if we exit make_call for any reason (exception, early
            # return) and the call is still active, send BYE so the remote
            # side doesn't stay connected indefinitely.
            if not call.disconnected_event.is_set():
                self._log.warning("Failsafe hangup — call still active on make_call exit")
                try:
                    call.hangup(pj.CallOpParam())
                except Exception:
                    pass
