#!/bin/sh
set -eu

if [ "$#" -ne 4 ]; then
  echo "usage: $0 <ssh-host> <camera-id> <broker-host> <mqtt-user>" >&2
  exit 2
fi

host=$1
camera_id=$2
broker=$3
mqtt_user=$4
case "$camera_id" in *[!a-z0-9-]*|'') echo "invalid camera id" >&2; exit 2;; esac

# The remote TTY prompt keeps the password out of arguments, files, and shell history.
ssh -tt "$host" sh -s -- "$camera_id" "$broker" "$mqtt_user" <<'REMOTE'
set -eu
camera_id=$1
broker=$2
mqtt_user=$3
printf "MQTT password for %s: " "$mqtt_user" >/dev/tty
stty -echo </dev/tty
IFS= read -r mqtt_password </dev/tty
stty echo </dev/tty
printf "\n" >/dev/tty
stamp=$(date +%Y%m%d-%H%M%S)
cp /etc/send2.json "/etc/send2.json.camdash-$stamp"
cp /etc/prudynt.json "/etc/prudynt.json.camdash-$stamp"

jct /etc/send2.json set mqtt.enabled true
jct /etc/send2.json set mqtt.client_id "$camera_id"
jct /etc/send2.json set mqtt.host "$broker"
jct /etc/send2.json set mqtt.port 1884
jct /etc/send2.json set mqtt.username "$mqtt_user"
jct /etc/send2.json set mqtt.password "$mqtt_password"
jct /etc/send2.json set mqtt.use_ssl false
jct /etc/send2.json set mqtt.topic "camdash/cameras/$camera_id/motion"
jct /etc/send2.json set mqtt.message "{\"schema\":1,\"camera_id\":\"$camera_id\",\"event\":\"motion\",\"timestamp\":\"%s\"}"

jct /etc/send2.json set storage.mount /mnt/mmcblk0p1
jct /etc/send2.json set storage.device_path "camdash/$camera_id/motion"
jct /etc/send2.json set storage.template "%Y%m%d/%H/%Y%m%dT%H%M%S"
jct /etc/send2.json set storage.send_photo true
jct /etc/send2.json set storage.send_video true
jct /etc/prudynt.json set motion.video_length 30
jct /etc/prudynt.json set recorder.mount /mnt/mmcblk0p1
jct /etc/prudynt.json set recorder.device_path "camdash/$camera_id/motion"
jct /etc/prudynt.json set recorder.limit 24
jct /etc/prudynt.json set recorder.min_free_mb 4096
jct /etc/prudynt.json set recorder.cleanup_enabled true

kill -HUP "$(cat /var/run/recordmgr.pid 2>/dev/null)" 2>/dev/null || true
echo "Configured $camera_id; backups: /etc/send2.json.camdash-$stamp and /etc/prudynt.json.camdash-$stamp"
REMOTE
