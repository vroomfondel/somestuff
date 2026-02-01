# FROM python:3.13-trixie
# FROM python:3.13-bookworm
# FROM python:3.14-trixie
# FROM pypy:bookwork  # ist aktuell python 3.11.13
# FROM pypy:trixie  # ist aktuell bei python 3.11.13
# https://blog.miguelgrinberg.com/post/python-3-14-is-here-how-fast-is-it

ARG python_version=3.14
ARG debian_version=slim-trixie

FROM python:${python_version}-${debian_version}

# repeat without defaults in this build-stage
ARG python_version
ARG debian_version

# https://docs.docker.com/develop/develop-images/dockerfile_best-practices/

RUN apt update && \
    apt -y full-upgrade && \
    apt -y install htop procps iputils-ping locales vim tini bind9-dnsutils ipset git libimage-exiftool-perl && \
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

USER ${UNAME}

ENV PATH="/home/pythonuser/.local/bin:$PATH"

COPY --chown=${UID}:${GID} requirements.txt /
RUN pip3 install --no-cache-dir --upgrade -r /requirements.txt

# flickr_download from GitHub — includes unreleased fix for issue #166
RUN pip3 install --no-cache-dir git+https://github.com/beaufour/flickr-download.git

# ADD --chown=${UID}:${GID} "https://www.random.org/cgi-bin/randbyte?nbytes=10&format=h" skipcache

COPY --chown=${UID}:${GID} dinogame /app/dinogame
COPY --chown=${UID}:${GID} ecowittstuff /app/ecowittstuff
COPY --chown=${UID}:${GID} llmstuff /app/llmstuff
COPY --chown=${UID}:${GID} dnsstuff /app/dnsstuff
COPY --chown=${UID}:${GID} netatmostuff /app/netatmostuff
COPY --chown=${UID}:${GID} hydromailstuff /app/hydromailstuff
COPY --chown=${UID}:${GID} k3shelperstuff /app/k3shelperstuff
COPY --chown=${UID}:${GID} gcalstuff /app/gcalstuff

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
