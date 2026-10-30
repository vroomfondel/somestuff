# Postfix with PostgreSQL maps and the SPF policy daemon for a containerised mail server (Kubernetes pod next to
# Dovecot): main.cf, master.cf and the pgsql maps are mounted by the deployment, the image ships only the binaries.
ARG debian_version=trixie-slim
FROM debian:${debian_version}

ARG buildtime=unknown
LABEL org.opencontainers.image.created="${buildtime}"

RUN apt-get update \
    && echo "postfix postfix/main_mailer_type select No configuration" | debconf-set-selections \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        postfix postfix-pgsql postfix-policyd-spf-python ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh

EXPOSE 25 465 587

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
