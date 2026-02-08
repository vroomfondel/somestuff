"""Core SIP calling logic using PJSUA2.

Provides SipCaller (high-level, context-manager) and SipCall (PJSUA2 callback handler).
"""

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


class SipCallError(Exception):
    """Raised on SIP call errors (registration, transport, WAV issues)."""


class WavInfo:
    """WAV file metadata extracted via the wave module."""

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
        """Warn about non-standard formats but don't block playback."""
        if self.sample_width != 2:
            logger.warning(f"WAV sample width is {self.sample_width * 8}-bit, expected 16-bit PCM")
        if self.channels != 1:
            logger.warning(f"WAV has {self.channels} channels, expected mono")
        if self.framerate not in (8000, 16000, 44100, 48000):
            logger.warning(f"WAV sample rate is {self.framerate} Hz, typical SIP rates: 8000 or 16000 Hz")
        logger.info(
            f"WAV: {self.path.name} — {self.duration:.1f}s, {self.framerate}Hz, {self.channels}ch, {self.sample_width * 8}bit"
        )


def _require_pjsua2() -> None:
    if not PJSUA2_AVAILABLE:
        raise SipCallError(
            "pjsua2 Python bindings not available. "
            "Install PJSIP with Python bindings — see sipstuff/install_pjsip.sh"
        )


class SipCall(pj.Call if PJSUA2_AVAILABLE else object):  # type: ignore[misc]
    """PJSUA2 Call with callbacks for state changes and media."""

    def __init__(self, account: "pj.Account", call_id: int = pj.PJSUA_INVALID_ID if PJSUA2_AVAILABLE else -1) -> None:
        if PJSUA2_AVAILABLE:
            pj.Call.__init__(self, account, call_id)
        self.connected_event = threading.Event()
        self.disconnected_event = threading.Event()
        self.media_ready_event = threading.Event()
        self.wav_player: pj.AudioMediaPlayer | None = None
        self._wav_path: str | None = None
        self._audio_media: Any = None
        self._account = account
        self._disconnect_reason: str = ""
        self._autoplay: bool = True

    def set_wav_path(self, wav_path: str | None, autoplay: bool = True) -> None:
        self._wav_path = wav_path
        self._autoplay = autoplay

    def onCallState(self, prm: "pj.OnCallStateParam") -> None:  # noqa: N802
        ci = self.getInfo()
        logger.info(f"Call state: {ci.stateText} (last code: {ci.lastStatusCode})")

        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            self.connected_event.set()
        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self._disconnect_reason = ci.lastReason
            self.disconnected_event.set()
            self.connected_event.set()  # unblock waiters

    def onCallMediaState(self, prm: "pj.OnCallMediaStateParam") -> None:  # noqa: N802
        ci = self.getInfo()
        for mi in ci.media:
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                self._audio_media = self.getAudioMedia(mi.index)
                self.media_ready_event.set()
                if self._autoplay and self._wav_path:
                    self.play_wav()
                break

    def play_wav(self) -> bool:
        """Start playing the configured WAV file. Returns True on success."""
        if not self._wav_path or not self._audio_media:
            return False
        try:
            self.wav_player = pj.AudioMediaPlayer()
            self.wav_player.createPlayer(self._wav_path, pj.PJMEDIA_FILE_NO_LOOP)
            self.wav_player.startTransmit(self._audio_media)
            logger.info(f"Playing WAV: {self._wav_path}")
            return True
        except Exception as exc:
            logger.error(f"Failed to play WAV: {exc}")
            return False

    def stop_wav(self) -> None:
        """Stop current WAV playback."""
        if self.wav_player and self._audio_media:
            try:
                self.wav_player.stopTransmit(self._audio_media)
            except Exception:
                pass
            self.wav_player = None


class SipCaller:
    """High-level SIP caller with context-manager support.

    Usage:
        with SipCaller(config) as caller:
            success = caller.make_call("+491234567890", "/path/to/alert.wav")
    """

    def __init__(self, config: SipCallerConfig) -> None:
        _require_pjsua2()
        self.config = config
        self._ep: pj.Endpoint | None = None
        self._account: pj.Account | None = None
        self._transport: Any = None
        self._log = logger.bind(classname="SipCaller")

    def __enter__(self) -> "SipCaller":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def start(self) -> None:
        """Initialize PJSUA2 endpoint, transport, and account."""
        _require_pjsua2()

        self._ep = pj.Endpoint()
        self._ep.libCreate()

        ep_cfg = pj.EpConfig()
        ep_cfg.logConfig.level = 3
        ep_cfg.logConfig.consoleLevel = 3
        self._ep.libInit(ep_cfg)

        # Transport(s)
        tp_cfg = pj.TransportConfig()
        tp_cfg.port = self.config.sip.local_port

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

        # Account registration
        acfg = pj.AccountConfig()
        scheme = "sips" if self.config.sip.transport == "tls" else "sip"
        acfg.idUri = f"{scheme}:{self.config.sip.user}@{self.config.sip.server}"
        acfg.regConfig.registrarUri = f"{scheme}:{self.config.sip.server}:{self.config.sip.port}"

        cred = pj.AuthCredInfo("digest", "*", self.config.sip.user, 0, self.config.sip.password)
        acfg.sipConfig.authCreds.append(cred)

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
        """Shutdown PJSUA2 endpoint and cleanup."""
        if self._account is not None:
            try:
                self._account.shutdown()
            except Exception:
                pass
            self._account = None

        if self._ep is not None:
            try:
                self._ep.libDestroy()
            except Exception:
                pass
            self._ep = None

        self._log.info("PJSUA2 endpoint stopped")

    def make_call(
        self,
        destination: str,
        wav_file: str | Path,
        timeout: int | None = None,
        pre_delay: float | None = None,
        post_delay: float | None = None,
        repeat: int | None = None,
    ) -> bool:
        """Place a SIP call, play WAV on answer, hang up after playback.

        Args:
            destination: Phone number or SIP URI to call.
            wav_file: Path to WAV file to play.
            timeout: Override call timeout (seconds). None = use config value.
            pre_delay: Seconds to wait after answer before playback. None = use config value.
            post_delay: Seconds to wait after playback before hangup. None = use config value.
            repeat: Number of times to play the WAV. None = use config value.

        Returns:
            True if call was answered and WAV played (at least partially).
        """
        if self._account is None:
            raise SipCallError("SipCaller not started — call start() or use context manager")

        timeout = timeout if timeout is not None else self.config.call.timeout
        pre_delay = pre_delay if pre_delay is not None else self.config.call.pre_delay
        post_delay = post_delay if post_delay is not None else self.config.call.post_delay
        repeat = repeat if repeat is not None else self.config.call.repeat

        # Validate WAV
        wav_info = WavInfo(wav_file)
        wav_info.validate()

        # Build SIP URI (include port for non-standard ports so PJSIP doesn't use defaults)
        scheme = "sips" if self.config.sip.transport == "tls" else "sip"
        default_port = 5061 if self.config.sip.transport == "tls" else 5060
        if destination.startswith("sip:") or destination.startswith("sips:"):
            sip_uri = destination
        elif self.config.sip.port != default_port:
            sip_uri = f"{scheme}:{destination}@{self.config.sip.server}:{self.config.sip.port}"
        else:
            sip_uri = f"{scheme}:{destination}@{self.config.sip.server}"

        self._log.info(
            f"Calling {sip_uri} (timeout: {timeout}s, repeat: {repeat}x, pre: {pre_delay}s, post: {post_delay}s)"
        )

        # Don't autoplay — we manage playback timing ourselves
        call = SipCall(self._account)
        call.set_wav_path(str(wav_info.path), autoplay=False)

        prm = pj.CallOpParam(True)
        try:
            call.makeCall(sip_uri, prm)
        except Exception as exc:
            raise SipCallError(f"Failed to initiate call to {sip_uri}: {exc}") from exc

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
            return False

        self._log.info("Call answered")

        # Wait for media to be ready
        call.media_ready_event.wait(timeout=5)

        # Pre-delay
        if pre_delay > 0:
            self._log.info(f"Pre-delay: {pre_delay}s")
            if call.disconnected_event.wait(timeout=pre_delay):
                self._log.info("Remote party hung up during pre-delay")
                return True

        # Play WAV repeat times
        for i in range(repeat):
            if call.disconnected_event.is_set():
                self._log.info("Remote party hung up during playback")
                return True

            if repeat > 1:
                self._log.info(f"Playing WAV ({i + 1}/{repeat})")

            call.play_wav()

            # Wait for WAV duration + small buffer
            playback_wait = wav_info.duration + 0.5
            if call.disconnected_event.wait(timeout=playback_wait):
                self._log.info("Remote party hung up during playback")
                return True

            call.stop_wav()

        # Post-delay
        if post_delay > 0:
            self._log.info(f"Post-delay: {post_delay}s")
            if call.disconnected_event.wait(timeout=post_delay):
                self._log.info("Remote party hung up during post-delay")
                return True

        # Hang up
        self._log.info("Playback completed, hanging up")
        try:
            call.hangup(pj.CallOpParam())
        except Exception:
            pass
        call.disconnected_event.wait(timeout=5)

        return True
