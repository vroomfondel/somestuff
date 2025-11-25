# FROM python:3.13-trixie
FROM python:3.13-bookworm

# https://docs.docker.com/develop/develop-images/dockerfile_best-practices/

RUN apt update && \
    apt -y full-upgrade && \
    apt -y install htop procps iputils-ping python3-pdfminer locales vim tini fonts-noto-core bind9-dnsutils && \
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

COPY --chown=${UID}:${GID} requirements.txt /
RUN pip3 install --no-cache-dir --upgrade -r /requirements.txt

COPY --chown=${UID}:${GID} dinogame /app/dinogame
COPY --chown=${UID}:${GID} ecowitt /app/ecowitt
COPY --chown=${UID}:${GID} llmstuff /app/llmstuff

COPY --chown=${UID}:${GID} config.py config.yaml Helper.py README.md /app/
# COPY --chown=${UID}:${GID} config.local.yaml /app/

WORKDIR /app

# set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ENV PYTHONPATH="${PYTHONPATH}:/app"

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
