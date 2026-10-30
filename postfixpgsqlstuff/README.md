[![Docker Pulls](https://img.shields.io/docker/pulls/xomoxcc/postfix-pgsql?logo=docker)](https://hub.docker.com/r/xomoxcc/postfix-pgsql/tags)

[![Container Image](logo_postfixpgsqlstuff_250x250.png)](https://hub.docker.com/r/xomoxcc/postfix-pgsql/tags)

# postfix-pgsql

Debian trixie with `postfix`, `postfix-pgsql`, `postfix-policyd-spf-python` and `tini`. The image ships only the binaries: `main.cf`, `master.cf` and the pgsql map files are mounted by the deployment. Built for a Kubernetes mail pod next to a Dovecot container, with LMTP and SASL over unix sockets in a shared volume, but nothing in it is Kubernetes-specific.

## Entrypoint

`entrypoint.sh` runs `postfix check`, then `postfix start-fg` under `tini`. When `main.cf` sets `maillog_file` (Postfix logs to a file instead of syslog), a background loop rotates it daily with `postfix logrotate` followed by `postfix reload`.

## Mounts

| Path                      | Content                                                      |
|---------------------------|--------------------------------------------------------------|
| `/etc/postfix/main.cf`    | your `main.cf` (read-only)                                   |
| `/etc/postfix/master.cf`  | your `master.cf` (read-only)                                 |
| `/etc/postfix/pgsql/*.cf` | pgsql map files, readable by the `postfix` user (`proxymap`) |
| `/var/spool/postfix`      | the queue (persistent volume)                                |
| `/etc/mail-tls`           | certificate and key for `smtpd_tls_*` (optional path)        |

Ports: `EXPOSE 25 465 587`; PROXY-protocol listeners (`smtpd_upstream_proxy_protocol=haproxy`, `postscreen_upstream_proxy_protocol=haproxy`) are configured in `master.cf` like any other service.

## Build

`./build.sh` builds multi-arch (amd64, arm64) and pushes `docker.io/xomoxcc/postfix-pgsql:trixie-slim` and `:latest`.

Used by the mailserver pod of `dgxarley` (`roles/k8s_infra/tasks/apps/mailserver.yml`).
