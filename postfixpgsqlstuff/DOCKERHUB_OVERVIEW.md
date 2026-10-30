[![https://github.com/vroomfondel/somestuff/raw/main/postfixpgsqlstuff/logo_postfixpgsqlstuff_250x250.png](https://github.com/vroomfondel/somestuff/raw/main/postfixpgsqlstuff/logo_postfixpgsqlstuff_250x250.png)](https://github.com/vroomfondel/somestuff/tree/main/postfixpgsqlstuff)

# postfix-pgsql

Debian trixie with `postfix`, `postfix-pgsql`, `postfix-policyd-spf-python` and `tini`. The image ships only the binaries: `main.cf`, `master.cf` and the pgsql map files are mounted by the deployment. Built for a Kubernetes mail pod next to a Dovecot container (LMTP and SASL over unix sockets in a shared volume), but nothing in it is Kubernetes-specific.

## Entrypoint

`postfix check`, then `postfix start-fg` under `tini`. When `main.cf` sets `maillog_file`, a background loop rotates it daily with `postfix logrotate` followed by `postfix reload`.

## Mounts

| Path                      | Content                                                      |
|---------------------------|--------------------------------------------------------------|
| `/etc/postfix/main.cf`    | your `main.cf` (read-only)                                   |
| `/etc/postfix/master.cf`  | your `master.cf` (read-only)                                 |
| `/etc/postfix/pgsql/*.cf` | pgsql map files, readable by the `postfix` user (`proxymap`) |
| `/var/spool/postfix`      | the queue (persistent volume)                                |
| `/etc/mail-tls`           | certificate and key for `smtpd_tls_*` (optional path)        |

Ports: `EXPOSE 25 465 587`; PROXY-protocol listeners are configured in `master.cf` like any other service.

## Tags

`trixie-slim` (the Debian base) and `latest`.

Source: [vroomfondel/somestuff/postfixpgsqlstuff](https://github.com/vroomfondel/somestuff/tree/main/postfixpgsqlstuff)
