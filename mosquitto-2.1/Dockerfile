FROM ubuntu:24.04

# to run with defaults:
# docker run -it -p 1884:1883 -p 8884:8883 -p 9884:9883 -p 9002:9001 \
# -v $(pwd)/mosquitto_test.conf:/mosquitto/config/mosquitto.conf \
# -v $(pwd)/mosquitto_test.passwd:/mosquitto/config/mosquitto.passwd  \
# -v $(pwd)/mosquitto_dynamic-security_test.json:/mosquitto/data/dynamic-security.json \
# -v $(pwd)/mosquitto_test.acl:/mosquitto/config/mosquitto.acl --rm xomoxcc/mosquitto:2.1

# or just:
# docker run -it -p 1884:1883 -p 8884:8883 -p 9884:9883 -p 9002:9001 --rm xomoxcc/mosquitto:2.1

# start haproxy with proxy-protocol_v2 forwarding
# docker run -it --rm --name haproxy --network=host -v $(pwd)/haproxy_test.cfg:/usr/local/etc/haproxy/haproxy.cfg  haproxy:latest


# THIS ONE LACKS SOME CONFIG/LIBRARIES...

# FROM registry.cedalo.com/eclipse-mosquitto-2.1-testing/mosquitto:latest

# ldd /usr/bin/mosquitto_ctrl
#	/lib/ld-musl-x86_64.so.1 (0x7f4a2c290000)
#	libcjson.so.1 => /usr/lib/libcjson.so.1 (0x7f4a2c25c000)
#	libmosquitto.so.1 => /usr/lib/libmosquitto.so.1 (0x7f4a2c238000)
#	libcrypto.so.3 => /usr/lib/libcrypto.so.3 (0x7f4a2bd73000)
#Error loading shared library libedit.so.0: No such file or directory (needed by /usr/bin/mosquitto_ctrl)
#	libc.musl-x86_64.so.1 => /lib/ld-musl-x86_64.so.1 (0x7f4a2c290000)
#	libssl.so.3 => /usr/lib/libssl.so.3 (0x7f4a2bca5000)

# / # ldd /usr/lib/mosquitto_password_file.so
#	/lib/ld-musl-x86_64.so.1 (0x7f8bc9937000)
#Error loading shared library ../../libcommon/libmosquitto_common.so.1: No such file or directory (needed by /usr/lib/mosquitto_password_file.so)
#	libc.musl-x86_64.so.1 => /lib/ld-musl-x86_64.so.1 (0x7f8bc9937000)


# ORIG FILE from https://github.com/eclipse-mosquitto/mosquitto/archive/refs/tags/2.1.0-test1.tar.gz :: docker :: 2.1-ubuntu

ENV SRC="https://github.com/eclipse-mosquitto/mosquitto/archive/refs/tags/2.1.0-test1.tar.gz"

RUN set -x && \
    apt-get update && \
    apt-get install -y \
        build-essential \
        cmake \
        gnupg \
        libargon2-dev \
        libcjson-dev \
        libedit-dev \
        libmicrohttpd-dev \
        libssl-dev \
        libsqlite3-dev \
        wget \
        tini \
        gettext-base

RUN set -x && wget ${SRC} -O /tmp/mosq.tar.gz

RUN set -x && mkdir -p /build/mosq && \
    tar --strip=1 -xf /tmp/mosq.tar.gz -C /build/mosq && \
    rm /tmp/mosq.tar.gz && \
    make -C /build/mosq -j "$(nproc)" \
        CFLAGS="-Wall -O2 -I/build -DHTTP_API_DIR=\\\"/usr/share/mosquitto/dashboard\\\"" \
        WITH_ADNS=no \
        WITH_DOCS=no \
        WITH_SHARED_LIBRARIES=yes \
        WITH_SRV=no \
        WITH_STRIP=yes \
        WITH_WEBSOCKETS=yes \
        prefix=/usr \
        binary && \
    addgroup --system --quiet --gid 1883 mosquitto 2>/dev/null && \
    adduser --system --quiet --no-create-home --ingroup mosquitto --uid 1883 --home /var/empty --shell /usr/sbin/nologin mosquitto 2>/dev/null && \
    mkdir -p /mosquitto/config /mosquitto/data /mosquitto/log

#RUN ls -haltr /build/mosq/
#RUN ls -haltr /build/mosq/libcommon

RUN set -x && install -d /usr/sbin/ && mkdir -p /libcommon && \
    install -s -m755 /build/mosq/client/mosquitto_pub /usr/bin/mosquitto_pub && \
    install -s -m755 /build/mosq/client/mosquitto_rr /usr/bin/mosquitto_rr && \
    install -s -m755 /build/mosq/client/mosquitto_sub /usr/bin/mosquitto_sub && \
    install -s -m644 /build/mosq/lib/libmosquitto.so.1 /usr/lib/libmosquitto.so.1 && \
    install -s -m755 /build/mosq/src/mosquitto /usr/sbin/mosquitto && \
    install -s -m755 /build/mosq/apps/mosquitto_ctrl/mosquitto_ctrl /usr/bin/mosquitto_ctrl && \
    install -s -m755 /build/mosq/apps/mosquitto_passwd/mosquitto_passwd /usr/bin/mosquitto_passwd && \
    install -s -m755 /build/mosq/apps/mosquitto_signal/mosquitto_signal /usr/bin/mosquitto_signal && \
    install -s -m755 /build/mosq/plugins/acl-file/mosquitto_acl_file.so /usr/lib/mosquitto_acl_file.so && \
    install -s -m755 /build/mosq/libcommon/libmosquitto_common.so.1 /libcommon/libmosquitto_common.so.1 && \
    install -s -m755 /build/mosq/plugins/dynamic-security/mosquitto_dynamic_security.so /usr/lib/mosquitto_dynamic_security.so && \
    install -s -m755 /build/mosq/plugins/password-file/mosquitto_password_file.so /usr/lib/mosquitto_password_file.so && \
    install -s -m755 /build/mosq/plugins/persist-sqlite/mosquitto_persist_sqlite.so /usr/lib/mosquitto_persist_sqlite.so && \
    install -s -m755 /build/mosq/plugins/sparkplug-aware/mosquitto_sparkplug_aware.so /usr/lib/mosquitto_sparkplug_aware.so && \
    install -m644 /build/mosq/docker/2.1-ubuntu/mosquitto.conf /mosquitto/config/mosquitto.conf && \
    install -d /usr/share/mosquitto && \
    cp -r /build/mosq/dashboard/src /usr/share/mosquitto/dashboard && \
    install -Dm644 /build/mosq/epl-v20 /usr/share/licenses/mosquitto/epl-v20 && \
    install -Dm644 /build/mosq/edl-v10 /usr/share/licenses/mosquitto/edl-v10 && \
    chown -R mosquitto:mosquitto /mosquitto && \
    apt-get install \
        ca-certificates \
        libargon2-1 \
        libcjson1 \
        libmicrohttpd12 \
        libsqlite3-0 && \
    apt-get clean && \
    apt-get remove --purge --auto-remove -y build-essential cmake gnupg && \
    rm -rf /var/lib/apt/lists/* && \
    rm -rf /build


COPY --chmod=0755 <<'EOF' /docker-entrypoint.sh
#!/bin/sh
set -e

# Set permissions
user="$(id -u)"
if [ "$PUID" = "" ]; then
	PUID="mosquitto"
fi
if [ "$PGID" = "" ]; then
	PGID="mosquitto"
fi
if [ "$user" = '0' ]; then
	[ -d "/mosquitto/data" ] && chown -R ${PUID}:${PGID} /mosquitto/data || true
fi

exec "$@"
EOF

EXPOSE 1883
EXPOSE 8883
EXPOSE 9883
EXPOSE 9001

# https://hynek.me/articles/docker-signals/
STOPSIGNAL SIGINT
#ENTRYPOINT ["/usr/bin/tini", "--"]

# ENV TINI_SUBREAPER=yes
# ENV TINI_KILL_PROCESS_GROUP=yes
# ENV TINI_VERBOSITY=3
COPY Dockerfile /Dockerfile

# just copying dummy files
COPY mosquitto_test.acl /mosquitto_test.acl
COPY mosquitto_test.passwd /mosquitto_test.passwd
COPY mosquitto_test.conf /mosquitto_test.conf
COPY mosquitto_dynamic-security_test.json /mosquitto_dynamic-security_test.json
COPY README.md /README.md
COPY k3s_mosquitto_deployment.yml /k3s_mosquitto_deployment.yml


RUN rm -vf /mosquitto/config/mosquitto.conf

COPY --chown=mosquitto:mosquitto --chmod=0755 <<'EOF' /runmosquitto.sh
#!/bin/bash

confdir=${CONFDIR:-/mosquitto/config}
datadir=${DATADIR:-/mosquitto/data}
mkdir -p ${confdir}
mkdir -p ${datadir}

chown -R mosquitto:mosquitto ${datadir}

echo
if ! [ -e ${confdir}/mosquitto.passwd ] ; then
  echo creating default user \"admin\" with password \"public\" at ${confdir}/mosquitto.passwd
  /usr/bin/mosquitto_passwd -c -b ${confdir}/mosquitto.passwd admin public
fi
chown mosquitto:mosquitto ${confdir}/mosquitto.passwd
chmod 0700 ${confdir}/mosquitto.passwd

echo
if ! [ -e ${datadir}/dynamic-security.json ] ; then
  echo creating default initial dynamic-security \(by copying over default file\) at ${datadir}/dynamic-security.json for user \"admin\" with password \"public\"
  echo /mosquitto_dynamic-security_test.json was created by calling:
  echo -e \\t/usr/bin/mosquitto_ctrl dynsec init ${datadir}/dynamic-security.json admin public
  cp -v /mosquitto_dynamic-security_test.json ${datadir}/dynamic-security.json
fi
chown mosquitto:mosquitto ${datadir}/dynamic-security.json
chmod 0700 ${datadir}/dynamic-security.json

echo
if ! [ -e ${confdir}/mosquitto.acl ] ; then
  echo creating \(by copying over default acl file\) default acl for user \"admin\" at ${confdir}/mosquitto.acl
  cp -v /mosquitto_test.acl ${confdir}/mosquitto.acl
fi
chown mosquitto:mosquitto ${confdir}/mosquitto.acl
chmod 0700 ${confdir}/mosquitto.acl

echo
if ! [ -e ${confdir}/mosquitto.conf ] ; then
  echo creating \(by copying over default conf file\) ${confdir}/mosquitto.conf
  cp -v /mosquitto_test.conf ${confdir}/mosquitto.conf
fi
chown mosquitto:mosquitto ${confdir}/mosquitto.conf
chmod 0700 ${confdir}/mosquitto.conf

echo
/docker-entrypoint.sh /usr/sbin/mosquitto -c ${confdir}/mosquitto.conf

EOF


ENTRYPOINT ["tini", "--"]

#CMD ["tail", "-f", "/dev/null"]

CMD [ "/runmosquitto.sh" ]
