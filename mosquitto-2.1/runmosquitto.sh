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
chmod 0600 ${confdir}/mosquitto.passwd

echo
if ! [ -e ${datadir}/dynamic-security.json ] ; then
  echo creating default initial dynamic-security \(by copying over default file\) at ${datadir}/dynamic-security.json for user \"admin\" with password \"public\"
  echo /mosquitto_dynamic-security_test.json was created by calling:
  echo -e \\t/usr/bin/mosquitto_ctrl dynsec init ${datadir}/dynamic-security.json admin public
  cp -v /mosquitto_dynamic-security_test.json ${datadir}/dynamic-security.json
fi
chown mosquitto:mosquitto ${datadir}/dynamic-security.json
chmod 0600 ${datadir}/dynamic-security.json

echo
if ! [ -e ${confdir}/mosquitto.acl ] ; then
  echo creating \(by copying over default acl file\) default acl for user \"admin\" at ${confdir}/mosquitto.acl
  cp -v /mosquitto_test.acl ${confdir}/mosquitto.acl
fi
chown mosquitto:mosquitto ${confdir}/mosquitto.acl
chmod 0600 ${confdir}/mosquitto.acl

echo
if ! [ -e ${confdir}/mosquitto.conf ] ; then
  echo creating \(by copying over default conf file\) ${confdir}/mosquitto.conf
  cp -v /mosquitto_test.conf ${confdir}/mosquitto.conf
fi
chown mosquitto:mosquitto ${confdir}/mosquitto.conf
chmod 0600 ${confdir}/mosquitto.conf

echo
/docker-entrypoint.sh /usr/sbin/mosquitto -c ${confdir}/mosquitto.conf
