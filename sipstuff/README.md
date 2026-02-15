# sipstuff

SIP caller module — place phone calls and play WAV files or TTS-generated speech via [PJSUA2](https://www.pjsip.org/). Includes speech-to-text transcription of recorded calls via [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

## Overview

Registers with a SIP/PBX server, dials a destination, plays a WAV file (or synthesizes text via [piper TTS](https://github.com/rhasspy/piper)) on answer, and hangs up. Designed for headless/container operation (uses a null audio device, no sound card required). Supports UDP, TCP, and TLS transports with optional SRTP media encryption.

## Prerequisites

PJSIP with Python bindings (`pjsua2`) must be installed. The main project Dockerfile builds PJSIP from source in a multi-stage build. For local development:

```bash
# Debian/Ubuntu prerequisites
sudo apt install build-essential python3-dev swig \
    libasound2-dev libssl-dev libopus-dev wget

# Build and install PJSIP (default: 2.16)
./sipstuff/install_pjsip.sh

# Or specify a version
PJSIP_VERSION=2.14.1 ./sipstuff/install_pjsip.sh
```

Other Python dependencies: `pydantic`, `ruamel.yaml`, `loguru`.

For TTS support: install `piper-tts` (optional, only needed for `--text`). Because `piper-phonemize` has no Python 3.14 wheels, the Docker image uses a separate Python 3.12 virtualenv at `/opt/piper-venv`. Resampling TTS output requires `ffmpeg`.

For STT support: `pip install faster-whisper` (optional, only needed for transcription). Whisper models are auto-downloaded on first use (~1.5 GB for the `medium` model).

## CLI Usage

```bash
# Minimal — using a config file with a WAV file
python -m sipstuff.cli call --config sip_config.yaml --dest +491234567890 --wav alert.wav

# Using CLI flags directly (no config file)
python -m sipstuff.cli call \
    --server pbx.local --user 1000 --password secret \
    --dest +491234567890 --wav alert.wav

# Connection via environment variables, only destination and audio via CLI
export SIP_SERVER=pbx.local SIP_USER=1000 SIP_PASSWORD=secret
python -m sipstuff.cli call --dest +491234567890 --wav alert.wav

# TTS instead of a WAV file (no audio file needed)
python -m sipstuff.cli call \
    --server pbx.local --user 1000 --password secret \
    --dest +491234567890 \
    --text "Achtung! Wasserstand kritisch!" \
    --tts-model de_DE-thorsten-high --tts-sample-rate 8000

# TTS with a custom model directory
python -m sipstuff.cli call \
    --config sip_config.yaml --dest +491234567890 \
    --text "Server offline!" \
    --tts-data-dir /opt/piper-voices

# Calling a SIP URI directly (instead of a phone number)
python -m sipstuff.cli call \
    --server pbx.local --user 1000 --password secret \
    --dest sip:conference@pbx.local --wav announcement.wav

# TLS transport with SRTP encryption and playback options
python -m sipstuff.cli call \
    --config sip_config.yaml \
    --transport tls --srtp mandatory \
    --dest +491234567890 --wav alert.wav \
    --pre-delay 1.5 --post-delay 2.0 --inter-delay 1.0 --repeat 3 \
    --timeout 30 -v

# Record remote-party audio and auto-transcribe (STT)
python -m sipstuff.cli call \
    --server pbx.local --user 1000 --password secret \
    --dest +491234567890 --wav alert.wav \
    --record /tmp/recording.wav

# Record with explicit STT options
python -m sipstuff.cli call \
    --config sip_config.yaml --dest +491234567890 --wav alert.wav \
    --record /tmp/recording.wav \
    --stt-model small --stt-language en \
    --stt-data-dir /opt/whisper-models

# NAT traversal — STUN + ICE behind a NAT gateway
python -m sipstuff.cli call \
    --server pbx.example.com --user 1000 --password secret \
    --stun-servers stun.l.google.com:19302,stun1.l.google.com:19302 \
    --ice \
    --dest +491234567890 --wav alert.wav -v
```

### CLI Flags

| Flag | Description |
|------|-------------|
| `--config`, `-c` | Path to YAML config file |
| `--server`, `-s` | PBX hostname or IP |
| `--port`, `-p` | SIP port (default: 5060) |
| `--user`, `-u` | SIP extension / username |
| `--password` | SIP password |
| `--transport` | `udp`, `tcp`, or `tls` (default: udp) |
| `--srtp` | `disabled`, `optional`, or `mandatory` (default: disabled) |
| `--tls-verify` | Verify TLS server certificate |
| `--dest`, `-d` | Destination phone number or SIP URI (required) |
| `--wav`, `-w` | Path to WAV file to play (mutually exclusive with `--text`) |
| `--text` | Text to synthesize via piper TTS (mutually exclusive with `--wav`) |
| `--tts-model` | Piper voice model (default: `de_DE-thorsten-high`) |
| `--tts-sample-rate` | Resample TTS output to this rate in Hz (default: native/22050) |
| `--tts-data-dir` | Directory for piper voice models (default: `~/.local/share/piper-voices`) |
| `--timeout`, `-t` | Call timeout in seconds (default: 60) |
| `--pre-delay` | Seconds to wait after answer before playback (default: 0) |
| `--post-delay` | Seconds to wait after playback before hangup (default: 0) |
| `--inter-delay` | Seconds to wait between WAV repeats (default: 0) |
| `--repeat` | Number of times to play the WAV (default: 1) |
| `--record` | Record remote-party audio to this WAV file path (parent dirs created automatically) |
| `--stt-data-dir` | Directory for Whisper STT models (default: `~/.local/share/faster-whisper-models`) |
| `--stt-model` | Whisper model size for transcription (default: `medium`, options: `tiny`/`base`/`small`/`medium`/`large-v3`) |
| `--stt-language` | Language code for STT transcription (default: `de`) |
| `--stun-servers` | Comma-separated STUN servers (e.g. `stun.l.google.com:19302`) |
| `--ice` | Enable ICE for media NAT traversal |
| `--turn-server` | TURN relay server (`host:port`) |
| `--turn-username` | TURN username |
| `--turn-password` | TURN password |
| `--turn-transport` | TURN transport: `udp`, `tcp`, or `tls` (default: udp) |
| `--keepalive` | UDP keepalive interval in seconds (0 = disabled) |
| `--public-address` | Public IP to advertise in SDP/Contact (e.g. K3s node IP) |
| `--verbose`, `-v` | Debug logging |

## Docker / Podman Example

Convert a WAV file to 8 kHz mono PCM and place a TLS+SRTP call from a container:

```bash
ffmpeg -i alert.wav -ar 8000 -ac 1 -sample_fmt s16 -y /tmp/alert.wav 2>/dev/null && \
podman run --network=host -it --rm --userns=keep-id:uid=1200,gid=1201 \
    -v /tmp/alert.wav:/app/alert.wav:ro \
    xomoxcc/somestuff:latest \
    python3 -m sipstuff.cli \
    call --server pbx.example.com \
    --port 5161 --transport tls --srtp mandatory \
    --user 1000 \
    --password changeme \
    --dest +491234567890 \
    --wav /app/alert.wav \
    --pre-delay 3.0 \
    --post-delay 1.0 \
    --repeat 3 -v
```

TTS call from a container (no WAV file needed on the host):

```bash
podman run --network=host -it --rm --userns=keep-id:uid=1200,gid=1201 \
    xomoxcc/somestuff:latest \
    python3 -m sipstuff.cli \
    call --server pbx.example.com \
    --port 5161 --transport tls --srtp mandatory \
    --user 1000 \
    --password changeme \
    --dest +491234567890 \
    --text "Achtung! Wasserstand kritisch!" \
    --tts-sample-rate 8000 \
    --pre-delay 3.0 \
    --post-delay 1.0 \
    --repeat 3 -v
```

TTS with persistent voice models (avoids re-downloading on every `--rm` run):

```bash
podman run --network=host -it --rm --userns=keep-id:uid=1200,gid=1201 \
    -v ~/.local/share/piper-voices:/data/piper \
    xomoxcc/somestuff:latest \
    python3 -m sipstuff.cli \
    call --server pbx.example.com \
    --port 5161 --transport tls --srtp mandatory \
    --user 1000 \
    --password changeme \
    --dest +491234567890 \
    --text "Achtung! Wasserstand kritisch!" \
    --tts-data-dir /data/piper \
    --tts-sample-rate 8000 \
    --pre-delay 3.0 -v
```

Passing connection parameters via environment variables:

```bash
podman run --network=host -it --rm --userns=keep-id:uid=1200,gid=1201 \
    -e SIP_SERVER=pbx.example.com \
    -e SIP_PORT=5161 \
    -e SIP_USER=1000 \
    -e SIP_PASSWORD=changeme \
    -e SIP_TRANSPORT=tls \
    -e SIP_SRTP=mandatory \
    xomoxcc/somestuff:latest \
    python3 -m sipstuff.cli \
    call --dest +491234567890 \
    --text "Server nicht erreichbar!" \
    --tts-sample-rate 8000 -v
```

Notes:
- `--network=host` is needed for SIP/RTP media traffic.
- `--userns=keep-id:uid=1200,gid=1201` maps the container's `pythonuser` to your host user (rootless Podman).
- The `ffmpeg` step in the WAV example ensures the file is in a SIP-friendly format (8 kHz, mono, 16-bit PCM).
- The TTS example needs no host-side WAV file — piper generates speech inside the container. Use `--tts-sample-rate 8000` to resample for narrowband SIP.

## Library Usage

```python
from sipstuff import make_sip_call

# Simple call with a WAV file
success = make_sip_call(
    server="pbx.local",
    user="1000",
    password="secret",
    destination="+491234567890",
    wav_file="/path/to/alert.wav",
    transport="udp",
    repeat=2,
)

# Call with TTS (no WAV file needed)
success = make_sip_call(
    server="pbx.local",
    user="1000",
    password="secret",
    destination="+491234567890",
    text="Achtung! Wasserstand kritisch!",
    tts_model="de_DE-thorsten-high",
    pre_delay=1.0,
    post_delay=1.0,
)

# TLS + SRTP call with all options
success = make_sip_call(
    server="pbx.example.com",
    user="1000",
    password="secret",
    destination="+491234567890",
    wav_file="/path/to/alert.wav",
    port=5061,
    transport="tls",
    timeout=30,
    pre_delay=1.5,
    post_delay=2.0,
    inter_delay=1.0,
    repeat=3,
)
```

### Context Manager for Multiple Calls

```python
from sipstuff import SipCaller, load_config

config = load_config(config_path="sip_config.yaml")
with SipCaller(config) as caller:
    caller.make_call("+491234567890", "alert.wav")
    caller.make_call("+490987654321", "other.wav")
```

### Using TTS Directly

```python
from sipstuff import generate_wav, TtsError

# Generate a WAV file from text (auto-downloads model on first use)
try:
    wav_path = generate_wav(
        text="Server nicht erreichbar!",
        model="de_DE-thorsten-high",
        sample_rate=8000,  # resample for narrowband SIP (0 = keep native)
    )
    # wav_path is a temporary file — use it, then clean up
    print(f"Generated: {wav_path}")
except TtsError as exc:
    print(f"TTS failed: {exc}")

# With a specific output path and model directory
wav_path = generate_wav(
    text="Wasserstand kritisch!",
    output_path="/tmp/alert_tts.wav",
    data_dir="/opt/piper-voices",
)
```

### Using STT (Speech-to-Text) Directly

```python
from sipstuff import transcribe_wav, SttError

# Transcribe a recorded call (auto-downloads model on first use)
try:
    text = transcribe_wav("/tmp/recording.wav")  # default: medium model, German
    print(f"Transcription: {text}")
except SttError as exc:
    print(f"STT failed: {exc}")

# English transcription with a different model
text = transcribe_wav("/tmp/recording.wav", language="en", model="large-v3")

# Custom model cache directory (useful for Docker volumes)
text = transcribe_wav(
    "/tmp/recording.wav",
    data_dir="/opt/whisper-models",
)
```

### Call with Recording and Transcription

```python
from sipstuff import SipCaller, load_config, transcribe_wav

config = load_config(config_path="sip_config.yaml")
with SipCaller(config) as caller:
    success = caller.make_call(
        "+491234567890", "alert.wav", record_path="/tmp/recording.wav"
    )
    if success:
        text = transcribe_wav("/tmp/recording.wav")
        print(f"Remote party said: {text}")
```

### Error Handling

```python
from sipstuff import make_sip_call, SipCallError, TtsError, SttError

try:
    success = make_sip_call(
        server="pbx.local",
        user="1000",
        password="secret",
        destination="+491234567890",
        wav_file="alert.wav",
    )
    if not success:
        print("Call was not answered")
except SipCallError as exc:
    print(f"SIP error: {exc}")  # registration, transport, or WAV issues
except TtsError as exc:
    print(f"TTS error: {exc}")  # piper not found, synthesis failed
except SttError as exc:
    print(f"STT error: {exc}")  # faster-whisper not found, transcription failed
```

## Configuration

Configuration is loaded with the following priority (highest first): CLI flags / overrides > environment variables > YAML file.

### YAML Config

See `example_config.yaml` for a full example:

```yaml
sip:
  server: "pbx.example.com"
  port: 5060
  user: "1000"
  password: "changeme"
  transport: "udp"        # udp, tcp, or tls
  srtp: "disabled"        # disabled, optional, or mandatory
  tls_verify_server: false
  local_port: 0           # 0 = auto

call:
  timeout: 60
  pre_delay: 0.0
  post_delay: 0.0
  inter_delay: 0.0
  repeat: 1

tts:
  model: "de_DE-thorsten-high"  # piper voice model (auto-downloaded on first use)
  sample_rate: 0                # 0 = keep native (22050), use 8000 for narrowband SIP
```

### NAT Traversal

For hosts behind NAT, add a `nat:` section to the YAML config:

```yaml
nat:
  stun_servers:                # STUN servers for public IP discovery
    - "stun.l.google.com:19302"
  stun_ignore_failure: true    # continue if STUN unreachable (default: true)
  ice_enabled: true            # ICE for media NAT traversal
  turn_enabled: false          # TURN relay (requires turn_server)
  turn_server: ""              # host:port
  turn_username: ""
  turn_password: ""
  turn_transport: "udp"        # udp, tcp, or tls
  keepalive_sec: 15            # UDP keepalive interval (0 = disabled)
  public_address: ""           # Manual public IP for SDP/Contact (e.g. K3s node IP)
```

- **STUN only** — sufficient when the NAT gateway preserves port mappings (cone NAT). Lets PJSIP discover its public IP for the SDP `c=` line.
- **STUN + ICE** — adds connectivity checks so both sides probe multiple candidate pairs. Handles most residential/corporate NATs.
- **TURN** — needed when the NAT is symmetric or a firewall blocks direct UDP. All media is relayed through the TURN server, adding latency but guaranteeing connectivity.
- **Keepalive** — sends periodic UDP packets to keep NAT bindings alive during long calls or idle registrations.

#### Static NAT / K3s

When running in a K3s pod (10.x.x.x pod IP) calling a SIP server in a DMZ (e.g. Fritzbox), the auto-detected local IP is the pod IP which is unreachable from the SIP server. STUN doesn't help either — it returns the WAN IP, not the node IP. Use `public_address` to manually set the IP that appears in SDP and SIP Contact headers, while the socket stays bound to the pod IP:

```yaml
nat:
  public_address: "192.168.1.50"  # K3s node IP reachable from the SIP server
```

Or via CLI / environment variable:

```bash
# CLI
python -m sipstuff.cli call --public-address 192.168.1.50 --dest +491234567890 --wav alert.wav

# Environment variable
export SIP_PUBLIC_ADDRESS=192.168.1.50
```

This works because K3s uses SNAT (conntrack) for pod-to-external traffic — reply packets from the Fritzbox to the node IP are translated back to the pod IP by the kernel's connection tracking.

### Environment Variables

All settings can be set via `SIP_` prefixed environment variables:

| Variable | Maps to |
|----------|---------|
| `SIP_SERVER` | `sip.server` |
| `SIP_PORT` | `sip.port` |
| `SIP_USER` | `sip.user` |
| `SIP_PASSWORD` | `sip.password` |
| `SIP_TRANSPORT` | `sip.transport` |
| `SIP_SRTP` | `sip.srtp` |
| `SIP_TLS_VERIFY_SERVER` | `sip.tls_verify_server` |
| `SIP_LOCAL_PORT` | `sip.local_port` |
| `SIP_TIMEOUT` | `call.timeout` |
| `SIP_PRE_DELAY` | `call.pre_delay` |
| `SIP_POST_DELAY` | `call.post_delay` |
| `SIP_INTER_DELAY` | `call.inter_delay` |
| `SIP_REPEAT` | `call.repeat` |
| `SIP_TTS_MODEL` | `tts.model` |
| `SIP_TTS_SAMPLE_RATE` | `tts.sample_rate` |
| `SIP_STUN_SERVERS` | `nat.stun_servers` (comma-separated) |
| `SIP_STUN_IGNORE_FAILURE` | `nat.stun_ignore_failure` |
| `SIP_ICE_ENABLED` | `nat.ice_enabled` |
| `SIP_TURN_ENABLED` | `nat.turn_enabled` |
| `SIP_TURN_SERVER` | `nat.turn_server` |
| `SIP_TURN_USERNAME` | `nat.turn_username` |
| `SIP_TURN_PASSWORD` | `nat.turn_password` |
| `SIP_TURN_TRANSPORT` | `nat.turn_transport` |
| `SIP_KEEPALIVE_SEC` | `nat.keepalive_sec` |
| `SIP_PUBLIC_ADDRESS` | `nat.public_address` |

TTS runtime environment variables (for overriding piper binary paths):

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPER_BIN` | `/opt/piper-venv/bin/piper` | Path to piper CLI binary |
| `PIPER_PYTHON` | `/opt/piper-venv/bin/python` | Python interpreter for piper venv |
| `PIPER_DATA_DIR` | `~/.local/share/piper-voices` | Directory for downloaded voice models |

STT (speech-to-text) environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_DATA_DIR` | `~/.local/share/faster-whisper-models` | Directory for downloaded Whisper models |
| `WHISPER_MODEL` | `medium` | Default model size (`tiny`, `base`, `small`, `medium`, `large-v3`) |
| `WHISPER_DEVICE` | `cpu` | Compute device (`cpu` or `cuda`) |
| `WHISPER_COMPUTE_TYPE` | `int8` (CPU) / `float16` (CUDA) | Quantization type (`int8`, `float16`, `float32`) |

PJSIP native log routing:

| Variable | Default | Description |
|----------|---------|-------------|
| `PJSIP_LOG_LEVEL` | `3` | PJSIP log verbosity routed through loguru (0 = none … 6 = trace) |
| `PJSIP_CONSOLE_LEVEL` | `4` | PJSIP native console output printed directly to stdout (4 = PJSIP default, 0 = suppressed) |

All PJSIP native output is captured by a `pj.LogWriter` subclass and forwarded to loguru (`classname="pjsip"`). `PJSIP_LOG_LEVEL` controls what the writer receives; `PJSIP_CONSOLE_LEVEL` controls what PJSIP additionally prints to stdout on its own. Set `PJSIP_CONSOLE_LEVEL=0` to suppress native output and rely solely on loguru.

Both values can also be passed directly to the `SipCaller` constructor (`pjsip_log_level=`, `pjsip_console_level=`), which takes highest priority over env vars and class defaults.

## Troubleshooting

### One-way audio on multi-homed hosts

On hosts with multiple network interfaces PJSIP may auto-detect the wrong local IP and advertise it in the SDP `c=` line. The SIP gateway then sends RTP to the wrong address, resulting in **one-way audio** (you can see `RX total 0pkt` in the call stats). `sip_caller.py` works around this by calling `_local_address_for()` at startup — a non-sending UDP connect to the SIP server that lets the kernel's routing table select the correct source address. Both the SIP transport and the account media transport are then bound to that IP.

Symptom in logs:
```
pjsua_acc.c !....IP address change detected for account 0 (192.168.x.x --> 192.168.y.y)
```

### `PJSIP_ETPNOTSUITABLE` warning on INVITE

```
Temporary failure in sending Request msg INVITE ... Unsuitable transport selected (PJSIP_ETPNOTSUITABLE)
```

This is a PJSIP-internal transport selection warning that appears when the SIP URI includes `;transport=udp` and the bound transport doesn't match PJSIP's initial candidate. The call still succeeds on the retry. It does not affect media or audio quality.

### `conference.c Remove port failed` warning on hangup

```
conference.c !.Remove port failed: Invalid value or argument (PJ_EINVAL)
```

A cosmetic PJSIP warning during WAV player cleanup. The player's conference port is invalidated when the call's media is torn down; the subsequent C++ destructor tries to remove it again. Audio and call lifecycle are unaffected.

## WAV File Requirements

The module accepts standard WAV files. Recommended format for SIP:
- 16-bit PCM, mono, 8000 or 16000 Hz sample rate

Non-standard formats (stereo, different bit depths/rates) will produce warnings but playback is still attempted.

## Module Structure

| File | Purpose |
|------|---------|
| `__init__.py` | Public API: `make_sip_call`, `SipCaller`, `SipCallError`, `SipCallerConfig`, `TtsError`, `generate_wav`, `SttError`, `transcribe_wav`, `load_config` |
| `sip_caller.py` | Core calling logic: `SipCaller` (context manager), `SipCall` (PJSUA2 callbacks), `WavInfo` |
| `sipconfig.py` | Pydantic config models with YAML / env / override loading |
| `tts.py` | Piper TTS integration: text-to-WAV generation with optional resampling (uses `/opt/piper-venv`) |
| `stt.py` | Speech-to-text via faster-whisper: WAV transcription with configurable model and language |
| `cli.py` | CLI entry point (`python -m sipstuff.cli call ...`) |
| `install_pjsip.sh` | Build script for PJSIP with Python bindings (default: 2.16) |
| `example_config.yaml` | Sample configuration file |

## Tests

```bash
pytest tests/test_sipstuff.py -v
```

Tests cover config validation, WAV validation, and mocked PJSUA2 caller behavior (no real SIP server needed).
