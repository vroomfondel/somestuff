#!/bin/sh
set -eu

# Creates missing queue directories on the volume; set-permissions would chown the read-only mounted config.
postfix check

maillog_file="$(postconf -h maillog_file)"

# Daily: rotate a file maillog_file, and reload so running smtpd processes pick up cert-manager renewals.
reload_interval="${POSTFIX_RELOAD_INTERVAL:-86400}"
(
    while sleep "${reload_interval}"; do
        case "${maillog_file}" in
            "" | /dev/*) ;;
            *) postfix logrotate ;;
        esac
        postfix reload
    done
) &

exec postfix start-fg
