# ============================================================================
# Flickr Download Docker Image
# With Firefox and X11 support for OAuth authentication
# Compatible with Docker and Podman
# ============================================================================

ARG python_version=3.14
ARG debian_version=slim

FROM python:${python_version}-${debian_version}

ARG python_version
ARG debian_version

LABEL maintainer="Flickr Backup Script"
LABEL description="Flickr Download with browser support for OAuth"

#ARG buildtime
#ENV BUILDTIME=${buildtime}

# Install system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    # ExifTool for metadata
    libimage-exiftool-perl \
    # Browsers (both for flexibility)
    chromium \
    firefox-esr \
    # xdg-utils for xdg-open
    xdg-utils \
    # X11 libraries
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libdbus-1-3 \
    dbus \
    libxt6 \
    libnss3 \
    libnspr4 \
    libasound2t64 \
    # Fonts
    fonts-liberation \
    fonts-dejavu-core \
    # D-Bus (gdbus for XDG Desktop Portal browser opening)
    libglib2.0-bin \
    # Tools
    bash \
    procps \
    curl \
    iputils-ping \
    bind9-dnsutils \
    npm \
    ca-certificates \
    git \
    # Clean up
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

ENV HOME=/root

#RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
#RUN . "/root/.nvm/nvm.sh" &&  nvm install 24

RUN node -v && npm -v

RUN npm install -g '@immich/cli'

# Browser name symlinks ($BROWSER compatibility)
# chrome/google-chrome -> chromium
# firefox stays firefox-esr
RUN ln -sf /usr/bin/chromium /usr/bin/chrome && \
    ln -sf /usr/bin/chromium /usr/bin/google-chrome && \
    ln -sf /usr/bin/firefox-esr /usr/bin/firefox

# Python packages
RUN pip install --no-cache-dir \
    git+https://github.com/beaufour/flickr-download.git \
    PyYAML

# Patch: flickr_download crashes on photos with unknown "taken" date
# (Flickr returns "0000-00-00 00:00:00" which dateutil cannot parse)
RUN UTILS_PATH=$(python3 -c "from importlib.util import find_spec; print(find_spec('flickr_download.utils').origin)") && \
    sed -i '/taken = parser.parse(taken_str)/i\    if not taken_str or taken_str.startswith("0000"):\n        return' "$UTILS_PATH"

# Home directory for Podman (keep-id) - must be writable by all users
RUN mkdir -p /home/poduser && chmod 777 /home/poduser

# Mozilla directory for Firefox profile (both homes)
RUN mkdir -p /root/.mozilla /home/poduser/.mozilla && \
    chmod -R 777 /root/.mozilla /home/poduser/.mozilla

# Environment variables
ENV PYTHONUNBUFFERED=1

# url-opener: forwards browser-open requests to the host via a Unix socket.
# Used by USE_DSOCKET mode; installed unconditionally (mode is selected at runtime).
COPY --chmod=755 url-opener /usr/local/bin/url-opener

# url-dbus-opener: opens a URL on the host via the XDG Desktop Portal over D-Bus.
# Used by USE_DBUS mode; installed unconditionally (mode is selected at runtime).
COPY --chmod=755 url-dbus-opener /usr/local/bin/url-dbus-opener

COPY --chmod=755 flickr-docker.sh /usr/local/bin/flickr-docker.sh
COPY --chmod=755 flickr-list-albums.py /usr/local/bin/flickr-list-albums.py
COPY --chmod=755 flickr-download-wrapper.py /usr/local/bin/flickr-download-wrapper.py
COPY --chmod=755 upload-to-immich.sh /usr/local/bin/upload-to-immich.sh
COPY --chmod=755 immich-uploader-wrapped.py /usr/local/bin/immich-uploader-wrapped.py

# Entrypoint script with improved shell support
COPY --chmod=755 entrypoint.sh /entrypoint.sh

ARG buildtime
ENV BUILDTIME=${buildtime}

ENTRYPOINT ["/entrypoint.sh"]
CMD ["--help"]
