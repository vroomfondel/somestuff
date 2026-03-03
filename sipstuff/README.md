# sipstuff (moved)

The SIP caller module has been moved to its own standalone repository with significantly expanded features:

**[github.com/vroomfondel/sipstuff](https://github.com/vroomfondel/sipstuff)**

[![Docker Pulls](https://img.shields.io/docker/pulls/xomoxcc/sipstuff?logo=docker)](https://hub.docker.com/r/xomoxcc/sipstuff/tags)

Features (standalone repo):
- SIP caller via PJSUA2 — place phone calls with WAV playback or piper TTS
- Silence detection, call recording, speech-to-text transcription (faster-whisper)
- UDP, TCP, TLS transports with optional SRTP media encryption
- NAT traversal (STUN/ICE/TURN, static NAT/publicAddress)
- CLI, library API, and Kubernetes operator
- Standalone Docker image with multi-arch support (amd64 + arm64)

Docker image: [`xomoxcc/sipstuff`](https://hub.docker.com/r/xomoxcc/sipstuff/tags)
