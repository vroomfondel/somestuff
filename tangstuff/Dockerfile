ARG python_version=3.14
ARG debian_version=slim-trixie

FROM python:${python_version}-${debian_version}

ARG debian_version

# https://docs.docker.com/develop/develop-images/dockerfile_best-practices/

RUN apt update && \
    apt -y full-upgrade && \
    apt -y install htop procps iputils-ping locales vim less tini bind9-dnsutils ipset tang && \
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

WORKDIR /

# set environment variables
ARG gh_ref=gh_ref_is_undefined
ENV GITHUB_REF=$gh_ref
ARG gh_sha=gh_sha_is_undefined
ENV GITHUB_SHA=$gh_sha
ARG buildtime=buildtime_is_undefined
ENV BUILDTIME=$buildtime

USER _tang

# https://hynek.me/articles/docker-signals/

# STOPSIGNAL SIGINT
# ENTRYPOINT ["/usr/bin/tini", "--"]

# ENV TINI_SUBREAPER=yes
# ENV TINI_KILL_PROCESS_GROUP=yes
# ENV TINI_VERBOSITY=3

# https://github.com/latchset/tang

ARG PORT=9090
ARG DATADIR=/var/lib/tang

ENV DATADIR=$DATADIR
ENV PORT=$PORT

EXPOSE $PORT

COPY Dockerfile /Dockerfile
COPY README.md /README.md

COPY --chown=_tang:_tang --chmod=0755 <<EOF /runtang.sh
#!/bin/bash
echo now starting "/usr/libexec/tangd  -p ${PORT} -l ${DATADIR}"
/usr/libexec/tangd  -p ${PORT} -l ${DATADIR}
EOF

ENTRYPOINT ["tini", "--"]
CMD ["/runtang.sh"]
# CMD ["tail", "-f", "/dev/null"]
# uid=100(_tang) gid=101(_tang) groups=101(_tang)
# CMD ["/usr/libexec/tangd", "-p", "${PORT}", "-l", "${DATADIR}"]
# CMD ["python3", "main.py"]
