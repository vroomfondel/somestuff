[![https://github.com/vroomfondel/somestuff/raw/main/python314pandasmultiarch/Gemini_Generated_Image_python314pandasmultiarch_1scj0x1scj0x1scj_250x250.png](https://github.com/vroomfondel/somestuff/raw/main/python314pandasmultiarch/Gemini_Generated_Image_python314pandasmultiarch_1scj0x1scj0x1scj_250x250.png)](https://hub.docker.com/r/xomoxcc/pythonpandasmultiarch/tags)

Python 3.14 + Pandas 2.2.3 (Multi‑Arch Docker Base Image)

This directory provides a minimal, multi‑architecture Docker base image that ships with:
- Python 3.14 (Debian trixie base)
- Pandas 2.2.3 preinstalled in a virtual environment

It targets both amd64 and arm64 (aarch64) and is intended as a fast, reliable base for images or CI jobs that require exactly Python 3.14 with Pandas 2.2.3 — especially useful on arm64 where prebuilt wheels may be unavailable and compiling pandas can take a long time.

Why this is useful
- No prebuilt wheel for Pandas 2.2.3 on Python 3.14 aarch64 at the time of writing → local builds are slow and fragile.
- Ready‑to‑use venv at `/python_venv` with Pandas pinned to 2.2.3.
- Multi‑arch image published to Docker Hub for easy consumption in CI/CD and reproducible builds.

Image name and tags
- Registry: `xomoxcc/pythonpandasmultiarch`
- Default tag produced by the helper script: `python-3.14-pandas-2.2.3-trixie`
- The build script also tags `:latest` alongside the versioned tag.

Supported platforms
- linux/amd64
- linux/arm64 (aarch64)

What’s inside
- `Dockerfile`: two‑stage build. The builder stage creates a Python venv at `/python_venv` and installs `pandas==2.2.3`. The final image copies the venv and sets up a lean runtime with `tini` as entrypoint.
- `build.sh`: builds and optionally pushes multi‑arch images using Docker Buildx. Also adds a `:latest` tag if the selected tag is not already `latest`.
- `python_venv.sh`: container `CMD` wrapper that activates the venv and then execs `python3` with any arguments you provide.

Key defaults and environment
- Base: `python:3.14-trixie` (configurable via build args)
- Venv path: `/python_venv`
- Entry point: `tini --`
- Default command: `/app/python_venv.sh`
  - With no extra arguments, this starts an interactive Python 3.14 REPL inside the prebuilt venv.
  - With arguments, they are passed to `python3` (after venv activation).
- Locale/timezone: `de_DE.UTF-8`, `Europe/Berlin` (set in image)
- Non‑root user: `pythonuser` (UID/GID 1234)

Quick start
Run an interactive shell with the venv (and Pandas 2.2.3) active:
```bash
docker run -it --rm xomoxcc/pythonpandasmultiarch:python-3.14-pandas-2.2.3-trixie
```
You’ll land in a Python REPL. Verify versions:
```bash
>>> import pandas, sys
>>> pandas.__version__, sys.version
('2.2.3', '3.14.0 (… )')
```

Run a one‑off command:
```bash
docker run --rm xomoxcc/pythonpandasmultiarch:python-3.14-pandas-2.2.3-trixie -c "import pandas as pd; print(pd.__version__)"
```

Mount your project into `/app` and run a script:
```bash
docker run --rm \
  -v "$PWD":/app \
  -w /app \
  xomoxcc/pythonpandasmultiarch:python-3.14-pandas-2.2.3-trixie your_script.py
```

Use as a base image
In your own Dockerfile:
```Dockerfile
FROM xomoxcc/pythonpandasmultiarch:python-3.14-pandas-2.2.3-trixie

# Optional: install more Python deps into the prebuilt venv
COPY requirements.txt /app/
RUN . /python_venv/bin/activate && pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
CMD ["/app/python_venv.sh", "-m", "your.package.or.module"]
```

Build locally (no push)
```bash
./build.sh onlylocal
```
This creates a local image tagged like:
```
xomoxcc/pythonpandasmultiarch:python-3.14-pandas-2.2.3-trixie
xomoxcc/pythonpandasmultiarch:latest
```

Multi‑arch build and push
```bash
./build.sh
```
The script will:
- Ensure a Buildx builder exists (creates/uses `mbuilder`).
- Optionally log in to Docker Hub if credentials are provided (see below).
- Build for `linux/amd64,linux/arm64` and push to Docker Hub.

Credentials and configuration
The `build.sh` script sources `../scripts/include.sh`, which may load a local, untracked file such as `scripts/include.local.sh` for secrets. Example:
```bash
export DOCKER_TOKENUSER="your-docker-username"
export DOCKER_TOKEN="<your-access-token>"
```
Do not commit your local include file.

Customizing build args
You can override versions via build args if you build manually, e.g.:
```bash
docker build \
  -f Dockerfile \
  --build-arg python_version=3.14 \
  --build-arg pandas_version=2.2.3 \
  --build-arg debian_version=trixie \
  -t your/image:tag .
```

Troubleshooting
- Slow or failing Pandas build on arm64: use this prebuilt image to avoid compiling on CI.
- No `scripts/include.local.sh`: harmless for local builds; required only if you need to push to Docker Hub.
- Locale/timezone differences: the image sets `de_DE.UTF-8` and `Europe/Berlin`. Override at runtime if needed with `-e LANG=en_US.UTF-8` etc.
- Running as non‑root: the image uses user `pythonuser` (UID/GID 1234). Adjust volumes/permissions accordingly.

License
See the repository’s license files in the project root (e.g. `LICENSE.md`, `LICENSEMIT.md`).



## ⚠️ Note

This is a development/experimental project. For production use, review security settings, customize configurations, and test thoroughly in your environment. Provided "as is" without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose and noninfringement. In no event shall the authors or copyright holders be liable for any claim, damages or other liability, whether in an action of contract, tort or otherwise, arising from, out of or in connection with the software or the use or other dealings in the software. Use at your own risk.