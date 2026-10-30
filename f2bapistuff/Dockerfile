# fail2ban + a remote ban API in one image, three roles (entrypoint.sh):
#   client:  fail2ban sidecar next to the protected service, bans go to the ban API via action hubapi (+ f2b_hub_sync.py)
#   server:  fail2ban-server owning the remote-<client> jails and the host firewall (NET_ADMIN, host network)
#   server:  `api` — f2b_api.py, talks only to that server's socket, no capabilities
ARG debian_version=trixie-slim
FROM debian:${debian_version}

ARG buildtime=unknown
LABEL org.opencontainers.image.created="${buildtime}"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        fail2ban iptables whois curl ca-certificates msmtp-mta python3-typer tini \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/fail2ban/jail.d/defaults-debian.conf

COPY msmtprc /etc/msmtprc
COPY dovecot24.conf /etc/fail2ban/filter.d/dovecot24.conf
COPY remote-ban.conf /etc/fail2ban/filter.d/remote-ban.conf
COPY hubapi.conf /etc/fail2ban/action.d/hubapi.conf
COPY --chmod=0755 f2b_api.py /usr/local/bin/f2b_api.py
COPY --chmod=0755 f2b_hub_sync.py /usr/local/bin/f2b_hub_sync.py
COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["fail2ban"]
