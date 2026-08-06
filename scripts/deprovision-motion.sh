#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <ssh-host>" >&2
  exit 2
fi

host=$1

# Retires on-camera IVS motion detection and its MQTT publish now that
# Surveillance Station owns motion detection and calls the camdash webhook
# directly. camdash-motor.cgi (PTZ health/centering) is untouched.
ssh "$host" sh -s -- <<'REMOTE'
set -eu
stamp=$(date +%Y%m%d-%H%M%S)
cp /etc/send2.json "/etc/send2.json.camdash-$stamp"
cp /etc/prudynt.json "/etc/prudynt.json.camdash-$stamp"

jct /etc/send2.json set mqtt.enabled false
jct /etc/prudynt.json set motion.enabled false

/etc/init.d/S31prudynt restart

echo "Disabled on-camera motion detection and MQTT publish; backups: /etc/send2.json.camdash-$stamp and /etc/prudynt.json.camdash-$stamp"
REMOTE
