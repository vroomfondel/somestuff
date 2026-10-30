#!/bin/sh
set -eu

case "${1:-fail2ban}" in
    api)
        shift
        exec python3 /usr/local/bin/f2b_api.py "$@"
        ;;
    fail2ban)
        ;;
    *)
        exec "$@"
        ;;
esac

# Settings the mounted jails cannot carry (fail2ban.conf level).
cat > /etc/fail2ban/fail2ban.d/f2b-api.local <<EOF
[Definition]
logtarget = ${F2B_LOGTARGET:-STDOUT}
dbfile = ${F2B_DBFILE:-/var/lib/fail2ban/fail2ban.sqlite3}
dbpurgeage = ${F2B_DBPURGEAGE:-8d}
EOF

# A jail whose log file is missing fails the whole server start: F2B_TOUCH="path[:uid] ...".
for entry in ${F2B_TOUCH:-}; do
    path="${entry%%:*}"
    touch "${path}"
    [ "${entry}" = "${path}" ] || chown "${entry#*:}" "${path}"
done

mkdir -p /run/fail2ban /var/lib/fail2ban

if [ "${F2B_HUB_SYNC:-0}" = "1" ]; then
    python3 /usr/local/bin/f2b_hub_sync.py &
fi

exec fail2ban-server -f -x -v start
