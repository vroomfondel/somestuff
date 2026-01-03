ARG debian_version=trixie-slim

FROM debian:${debian_version}

ARG BPATH=.
ARG S6_OVERLAY_VERSION=3.2.1.0
ARG debian_version

# https://docs.docker.com/develop/develop-images/dockerfile_best-practices/

RUN apt update && \
    apt -y full-upgrade && \
    apt -y install htop procps iputils-ping locales vim less tini bind9-dnsutils ipset gettext-base wget xz-utils tang && \
    apt -y install nginx && \
    apt -y install haproxy && \
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


ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /tmp
RUN tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz

ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/syslogd-overlay-noarch.tar.xz /tmp
RUN tar -C / -Jxpf /tmp/syslogd-overlay-noarch.tar.xz

RUN S6_ARCH=x86_64 ; if [ ${TARGETARCH} != amd64 ] ; then S6_ARCH=aarch64 ; fi && \
    echo TARGETARCH: $TARGETARCH && echo S6_ARCH: ${S6_ARCH} \
    && wget -q https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz -O /tmp/s6-overlay-${S6_ARCH}.tar.xz \
    && tar -C / -Jxpf /tmp/s6-overlay-${S6_ARCH}.tar.xz


WORKDIR /

# set environment variables
ARG gh_ref=gh_ref_is_undefined
ENV GITHUB_REF=$gh_ref
ARG gh_sha=gh_sha_is_undefined
ENV GITHUB_SHA=$gh_sha
ARG buildtime=buildtime_is_undefined
ENV BUILDTIME=$buildtime


COPY <<'EOF' /etc/nginx/sites-available/tang-local.conf.pre
server {
	listen ${NGINXPORT} default_server ${NGINXACCEPT};
	listen [::]:${NGINXPORT} default_server ${NGINXACCEPT};

    # Erlaubt allen IPv4 und IPv6-Adressen, Proxy-Header zu senden UNSICHER!!!
	set_real_ip_from 0.0.0.0/0;
    set_real_ip_from ::/0;

    # 3. Sagen, woher die IP kommt
    real_ip_header proxy_protocol;

	root /var/www/html;

	# Add index.php to the list if you are using PHP
	index index.html index.htm index.nginx-debian.html;

	server_name _;

    # Diese Header sind für Tang technisch egal, aber gut für Nginx-Logs

    log_format main_ext '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for" "$proxy_protocol_addr"';

    access_log /var/log/nginx/tang_access.log main_ext;

	location / {
	    # try_files $uri $uri/ =404;
        try_files $uri $uri/ @proxy;
    }
    location @proxy {
        proxy_pass http://${TANGENDPOINT}:${TANGPORT};
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
# curl --haproxy-protocol -v http://localhost:80/adv
EOF

COPY <<'EOF' /etc/haproxy/haproxy.cfg.pre
global
	log stderr 	local0 debug
	# log stderr	local1 debug

	chroot /var/lib/haproxy
	stats socket /var/lib/haproxyadmin.sock mode 660 level admin expose-fd listeners
	stats timeout 30s
	user haproxy
	group haproxy
	# daemon

	# Default SSL material locations
	ca-base /etc/ssl/certs
	crt-base /etc/ssl/private

	# See: https://ssl-config.mozilla.org/#server=haproxy&server-version=2.0.3&config=intermediate
        ssl-default-bind-ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384
        ssl-default-bind-ciphersuites TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256
        ssl-default-bind-options ssl-min-ver TLSv1.2 no-tls-tickets

defaults
	log	global
	mode	http
	option	httplog
	option	dontlognull
    timeout connect 5000
    timeout client  50000
    timeout server  50000
	errorfile 400 /etc/haproxy/errors/400.http
	errorfile 403 /etc/haproxy/errors/403.http
	errorfile 408 /etc/haproxy/errors/408.http
	errorfile 500 /etc/haproxy/errors/500.http
	errorfile 502 /etc/haproxy/errors/502.http
	errorfile 503 /etc/haproxy/errors/503.http
	errorfile 504 /etc/haproxy/errors/504.http

# ---------------------------------------------------------------------
# FRONTEND: Hier kommen die Anfragen von Clevis an
# ---------------------------------------------------------------------
frontend tang_frontend
    bind *:${HAPROXYPORT} ${HAPROXYACCEPT}

    # Falls HTTPS gewünscht ist (empfohlen für Produktion):
    # bind *:443 ssl crt /etc/haproxy/certs/site.pem

    # 1. Option: Zugriffskontrolle (Security)
    # Definieren, welche Netze erlaubt sind
    # acl network_allowed src 192.168.123.210/24 10.0.0.0/8

    # Alles blockieren, was NICHT in der ACL ist
    # http-request deny if !network_allowed

    # acl from_proxy src 10.0.0.0/8 192.168.123.0/24 127.0.0.1
    acl from_proxy src -f /etc/haproxy/trusted_proxies.lst
    http-request set-src hdr(x-forwarded-for) if from_proxy

    # 2. Option: Logging & Header
    # Fügt den "X-Forwarded-For" Header hinzu
    option forwardfor
    # option forwardfor header X-Real-IP

    # Weiterleitung ans Backend
    default_backend tang_backend

# ---------------------------------------------------------------------
# BACKEND: Der eigentliche Tang Server
# ---------------------------------------------------------------------
backend tang_backend
    mode http
    balance roundrobin

    option forwarded

    server tang1 127.0.0.1:${TANGPORT} check

EOF

RUN printf "10.0.0.0/8\n127.0.0.1" > /etc/haproxy/trusted_proxies.lst


# https://github.com/latchset/tang
#

ARG USENGINX=0
ARG NGINXPORT=80
ARG USEHAPROXY=0
ARG HAPROXYPORT=80
ARG HAPROXYACCEPTPROXY=1
ARG NGINXACCEPTPROXY=1

ARG TANGPORT=9090
ARG TANGDATADIR=/var/lib/tang

ENV USENGINX=$USENGINX
ENV USEHAPROXY=$USEHAPROXY

ENV NGINXPORT=$NGINXPORT
ENV HAPROXYPORT=$HAPROXYPORT
ENV TANGDATADIR=$TANGDATADIR
ENV TANGPORT=$TANGPORT
ENV HAPROXYACCEPTPROXY=$HAPROXYACCEPTPROXY
ENV NGINXACCEPTPROXY=$NGINXACCEPTPROXY

EXPOSE $PORT

COPY Dockerfile /Dockerfile
COPY README.md /README.md


COPY --chown=_tang:_tang --chmod=0755 <<'EOF' /runtang.sh
#!/command/with-contenv bash

echo executing /runtang.sh

if [ $USENGINX -eq 1 ] || [ $USEHAPROXY -eq 1 ] ; then
    # env
    # test if i can set iptables needes either --privileged or --cap-add=NET_ADMIN
    iptables -L -n >/dev/null 2>&1
    if [ $? -eq 0 ] ; then
      iptables -A INPUT -s 127.0.0.1 -j ACCEPT
      iptables -A INPUT -p tcp --dport ${TANGPORT} -j REJECT
      iptables -A INPUT -p tcp --dport ${TANGPORT} -m limit --limit 1/sec -j LOG --log-prefix "IPTABLES_INPUT_TANGPORT_DROP " --log-level 4
      iptables -A INPUT -p tcp --dport ${TANGPORT} -j REJECT

      ip6tables -A INPUT -p tcp --dport ${TANGPORT} -m limit --limit 1/sec -j LOG --log-prefix "IP6TABLES_INPUT_TANGPORT_DROP " --log-level 4
      ip6tables -A INPUT -p tcp --dport ${TANGPORT} -j REJECT
    else
      echo could not add ip limit for tangport - please ensure ip block from outside or add either --privileged or --cap-add=NET_ADMIN
    fi
else
  echo neither USENGINX=$USENGINX nor USEHAPROXY=$USEHAPROXY is set to 1
fi

px="s6-setuidgid _tang"
echo now starting ${px} /usr/libexec/tangd  -p ${TANGPORT} -l ${TANGDATADIR}
exec ${px} /usr/libexec/tangd  -p ${TANGPORT} -l ${TANGDATADIR}

# switch user without s6:
#euname=$(id -u --name)
#px=""
#if [ "${euname}" != _tang ] ; then
#  px="su -p -s /bin/bash _tang -c"
#fi
## exec /usr/libexec/tangd  -p ${TANGPORT} -l -e ${TANGENDPOINT} ${TANGDATADIR}
#echo now starting ${px} /usr/libexec/tangd  -p ${TANGPORT} -l -e ${TANGENDPOINT} ${TANGDATADIR}
#exec ${px} "/usr/libexec/tangd  -p ${TANGPORT} -l -e ${TANGENDPOINT} ${TANGDATADIR}"
EOF

# https://skarnet.org/software/s6-linux-init/overview.html
#   Run user s6-rc services declared in /etc/s6-overlay/s6-rc.d, following dependencies

ADD $BPATH/s6-initstuff/ /etc/

# /etc/cont-init.d/01-check-env

# ENTRYPOINT ["tini", "--"]
# CMD ["/runtang.sh"]

ENTRYPOINT ["/init"]
CMD ["/runtang.sh"]
