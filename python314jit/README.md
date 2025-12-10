[![Docker Pulls](https://img.shields.io/docker/pulls/xomoxcc/python314-jit?logo=docker)](https://hub.docker.com/r/xomoxcc/python314-jit/tags)

Python 3.14 with Experimental JIT (Docker)

This directory builds a Docker image that contains CPython 3.14 compiled from source with the experimental JIT enabled. It also includes a small Python script to inspect build/runtime JIT status.

Why this is useful
- Try out CPython's experimental JIT on Linux without modifying your host system.
- Compare behavior and performance with/without JIT at runtime using the `PYTHON_JIT` environment variable.
- Produce multi-architecture images (amd64 and arm64) with Docker Buildx.

Contents
- Dockerfile: Compiles CPython 3.14 with `--enable-experimental-jit`, shared libs, LTO, and installs it to `/usr/local`. The default command is `python3`.
- build.sh: Helper script to build or build+push the image using Docker Buildx.
- checkpython.py: Diagnostic script that prints Python version/build info and probes for a JIT executor via `_opcode.get_executor(...)` after warming up a function.

Prerequisites
- Docker with Buildx enabled.
- For cross-arch builds: binfmt/QEMU support (the script can install via `tonistiigi/binfmt`).
- Optional: Docker registry credentials if you want to push images.

Security and configuration
The build helper sources `../scripts/include.sh`, which may in turn load a local, untracked file such as `scripts/include.local.sh`. Use that file to store credentials locally and keep secrets out of version control.

Example `scripts/include.local.sh` (do not commit):
```
export DOCKER_TOKENUSER="your-docker-username"
export DOCKER_TOKEN="<your-access-token>"
```

Quick start
1) Build locally (no push):
```
./build.sh onlylocal
```
This creates a local image tagged by default as `xomoxcc/python314-jit:trixie` and also adds `:latest`.

2) Run an interactive shell with JIT enabled:
```
docker run -it --rm -e PYTHON_JIT=1 xomoxcc/python314-jit:trixie
```
The Dockerfile sets `PYTHON_JIT=1` by default. You can override with `-e PYTHON_JIT=0` to disable JIT at runtime.

3) Verify build and JIT behavior using the diagnostic script:
```
# from this directory
docker run -i --rm -e PYTHON_JIT=1 xomoxcc/python314-jit:trixie < checkpython.py
```
The script prints:
- Python version and build tag.
- Whether the configure step included `--enable-experimental-jit`.
- Runtime selection based on `PYTHON_JIT`.
- A deep probe that warms up a function and tries to obtain an executor from `_opcode` at various bytecode offsets. If found, JIT appears active.

Multi-arch build and push
To build for amd64 and arm64 and push to the registry configured by `DOCKER_IMAGE` in `build.sh`:
```
./build.sh
```
The script will:
- Log in (if credentials are available via the include file).
- Ensure Buildx builder `mbuilder` exists.
- Build and push multi-arch images.

Notes about CPython 3.14 JIT
- Status: experimental; internals and APIs may change. The `_opcode` probing used by `checkpython.py` is a best-effort indicator and may need adjustments for future builds.
- Build-time vs runtime:
  - Build-time `--enable-experimental-jit` enables JIT capability.
  - Runtime `PYTHON_JIT=1` requests JIT; `PYTHON_JIT=0` disables it.
- GIL: This image uses the standard GIL-enabled build. The script prints GIL status when available (`sys._is_gil_enabled()` in 3.13+).

Troubleshooting
- "No include.local.sh file[s] found.": Harmless for local builds. Create `scripts/include.local.sh` if you need to push.
- Buildx/builder errors: Ensure Docker Buildx is available and that you can install binfmt (may require privileged Docker on the host).
- JIT appears inactive: Ensure `PYTHON_JIT=1`, let the warmup loop run, and note that internals can change across Python minor releases.

License
See the repository's license files in the project root (`LICENSE.md`, `LICENSEMIT.md`, etc.).
