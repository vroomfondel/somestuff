# FROM python:3.13-trixie
# FROM python:3.13-bookworm
# FROM python:3.14-trixie
# FROM pypy:bookwork  # ist aktuell python 3.11.13
# FROM pypy:trixie  # ist aktuell bei python 3.11.13
# https://blog.miguelgrinberg.com/post/python-3-14-is-here-how-fast-is-it

ARG python_version=3.14
ARG debian_version=slim-trixie

# ─── Stage 1: Build PJSIP with Python bindings ─────────────────────────────
FROM python:${python_version}-${debian_version} AS pjsip-builder

ARG PJSIP_VERSION=2.16

RUN apt update && \
    apt -y install build-essential python3-dev swig \
        libasound2-dev libssl-dev libopus-dev wget && \
    pip install --no-cache-dir setuptools && \
    rm -rf /var/lib/apt/lists/*

# Snapshot existing libs, build PJSIP, then stage only what was added
RUN ls /usr/local/lib/*.so* 2>/dev/null | sort > /tmp/_libs_before.txt || true

RUN cd /tmp && \
    wget -q "https://github.com/pjsip/pjproject/archive/refs/tags/${PJSIP_VERSION}.tar.gz" -O pjproject.tar.gz && \
    tar xzf pjproject.tar.gz && \
    cd "pjproject-${PJSIP_VERSION}" && \
    ./configure \
        --enable-shared \
        --disable-video \
        --disable-v4l2 \
        --disable-libyuv \
        --disable-libwebrtc \
        --with-external-opus \
        CFLAGS="-O2 -fPIC" \
        CXXFLAGS="-O2 -fPIC" && \
    make -j"$(nproc)" dep && \
    make -j"$(nproc)" && \
    make install && \
    cd pjsip-apps/src/swig/python && \
    make && \
    make install && \
    ldconfig && \
    mkdir -p /pjsip-libs /pjsip-python && \
    ls /usr/local/lib/*.so* 2>/dev/null | sort > /tmp/_libs_after.txt && \
    comm -13 /tmp/_libs_before.txt /tmp/_libs_after.txt | xargs -I{} cp -P {} /pjsip-libs/ && \
    python3 -c "\
import pjsua2, _pjsua2, os, shutil;\
dst='/pjsip-python';\
shutil.copy2(pjsua2.__file__, dst);\
shutil.copy2(_pjsua2.__file__, dst);\
print('Staged libs:', os.listdir('/pjsip-libs'));\
print('Staged python:', os.listdir(dst))" && \
    rm -rf /tmp/pjproject*

# ─── Stage 2: piper-tts with Python 3.12 ──────────────────────────────────
# piper-tts depends on piper-phonemize which ships native C++ extensions.
# piper-phonemize only publishes cpython wheels for 3.9–3.12 (last release 2023),
# so it cannot be installed under Python 3.14+.  We build a self-contained
# Python 3.12 venv here and copy it into the main image; sipstuff/tts.py
# invokes the piper CLI via subprocess.
FROM python:3.12-${debian_version} AS piper-builder

RUN python3 -m venv /opt/piper-venv && \
    /opt/piper-venv/bin/pip install --no-cache-dir piper-tts pathvalidate && \
    /opt/piper-venv/bin/python -c "from piper.__main__ import main; print('piper-tts OK')"

# Collect portable Python 3.12 runtime for the venv
RUN mkdir -p /opt/python312/bin /opt/python312/lib && \
    cp /usr/local/bin/python3.12 /opt/python312/bin/ && \
    cp -P /usr/local/lib/libpython3.12*.so* /opt/python312/lib/ && \
    cp -a /usr/local/lib/python3.12 /opt/python312/lib/python3.12 && \
    # Trim stdlib (tests, tkinter, idle, etc.)
    rm -rf /opt/python312/lib/python3.12/test \
           /opt/python312/lib/python3.12/idlelib \
           /opt/python312/lib/python3.12/tkinter \
           /opt/python312/lib/python3.12/turtledemo \
           /opt/python312/lib/python3.12/lib2to3 \
           /opt/python312/lib/python3.12/ensurepip && \
    find /opt/python312 -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true

# Repoint venv symlinks to the portable runtime location
RUN rm -f /opt/piper-venv/bin/python /opt/piper-venv/bin/python3 /opt/piper-venv/bin/python3.12 && \
    ln -s /opt/python312/bin/python3.12 /opt/piper-venv/bin/python && \
    ln -s /opt/python312/bin/python3.12 /opt/piper-venv/bin/python3 && \
    ln -s /opt/python312/bin/python3.12 /opt/piper-venv/bin/python3.12 && \
    sed -i 's|/usr/local|/opt/python312|g' /opt/piper-venv/pyvenv.cfg && \
    find /opt/piper-venv -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true

# ─── Stage 3: Main image ───────────────────────────────────────────────────
FROM python:${python_version}-${debian_version}

# repeat without defaults in this build-stage
ARG python_version
ARG debian_version

# https://docs.docker.com/develop/develop-images/dockerfile_best-practices/

RUN apt update && \
    apt -y full-upgrade && \
    apt -y install htop procps iputils-ping locales vim tini bind9-dnsutils ipset git libimage-exiftool-perl \
        libasound2t64 libssl3t64 libopus0 ffmpeg && \
    pip install --upgrade pip && \
    rm -rf /var/lib/apt/lists/*

RUN sed -i -e 's/# de_DE.UTF-8 UTF-8/de_DE.UTF-8 UTF-8/' /etc/locale.gen && \
    locale-gen && \
    update-locale LC_ALL=de_DE.UTF-8 LANG=de_DE.UTF-8 && \
    rm -f /etc/localtime && \
    ln -s /usr/share/zoneinfo/Europe/Berlin /etc/localtime


# MULTIARCH-BUILD-INFO: https://itnext.io/building-multi-cpu-architecture-docker-images-for-arm-and-x86-1-the-basics-2fa97869a99b
ARG TARGETOS
ARG TARGETARCH
RUN echo "I'm building for $TARGETOS/$TARGETARCH"

# default UID and GID are the ones used for selenium in seleniarm/standalone-chromium:107.0

ARG UID=1200
ARG GID=1201
ARG UNAME=pythonuser
RUN groupadd -g ${GID} -o ${UNAME} && \
    useradd -m -u ${UID} -g ${GID} -o -s /bin/bash ${UNAME}

# PJSIP shared libraries from builder (all .so files that PJSIP added)
COPY --from=pjsip-builder /pjsip-libs/ /usr/local/lib/
# Python bindings staged to /pjsip-python/ in builder (avoids site-packages vs dist-packages path issues)
COPY --from=pjsip-builder /pjsip-python/ /tmp/pjsip-python/
RUN PYDIR=$(python3 -c "import site; print(site.getsitepackages()[0])") && \
    cp /tmp/pjsip-python/* "$PYDIR/" && \
    rm -rf /tmp/pjsip-python && \
    ldconfig

# Python 3.12 runtime + piper-tts venv (piper-phonemize has no Python 3.14 wheels)
COPY --from=piper-builder /opt/python312 /opt/python312
COPY --from=piper-builder /opt/piper-venv /opt/piper-venv
RUN echo "/opt/python312/lib" > /etc/ld.so.conf.d/python312.conf && ldconfig

USER ${UNAME}

ENV PATH="/home/pythonuser/.local/bin:$PATH"

COPY --chown=${UID}:${GID} requirements.txt /
RUN pip3 install --no-cache-dir --upgrade -r /requirements.txt

# ADD --chown=${UID}:${GID} "https://www.random.org/cgi-bin/randbyte?nbytes=10&format=h" skipcache

COPY --chown=${UID}:${GID} dinogame /app/dinogame
COPY --chown=${UID}:${GID} ecowittstuff /app/ecowittstuff
COPY --chown=${UID}:${GID} llmstuff /app/llmstuff
COPY --chown=${UID}:${GID} dnsstuff /app/dnsstuff
COPY --chown=${UID}:${GID} netatmostuff /app/netatmostuff
COPY --chown=${UID}:${GID} hydromailstuff /app/hydromailstuff
COPY --chown=${UID}:${GID} k3shelperstuff /app/k3shelperstuff
COPY --chown=${UID}:${GID} gcalstuff /app/gcalstuff
COPY --chown=${UID}:${GID} sipstuff /app/sipstuff
COPY --chown=${UID}:${GID} dhcpstuff /app/dhcpstuff

COPY --chown=${UID}:${GID} config.py config.yaml Helper.py README.md /app/

# RUN rm skipcache

WORKDIR /app

# set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

#ENV PYTHONPATH=${PYTHONPATH:+${PYTHONPATH}:}/app:/app/mqttstuff
ENV PYTHONPATH=/app

ARG gh_ref=gh_ref_is_undefined
ENV GITHUB_REF=$gh_ref
ARG gh_sha=gh_sha_is_undefined
ENV GITHUB_SHA=$gh_sha
ARG buildtime=buildtime_is_undefined
ENV BUILDTIME=$buildtime

# https://hynek.me/articles/docker-signals/

# STOPSIGNAL SIGINT
# ENTRYPOINT ["/usr/bin/tini", "--"]

# ENV TINI_SUBREAPER=yes
# ENV TINI_KILL_PROCESS_GROUP=yes
# ENV TINI_VERBOSITY=3

ENTRYPOINT ["tini", "--"]
CMD ["tail", "-f", "/dev/null"]
# CMD ["python3", "main.py"]
