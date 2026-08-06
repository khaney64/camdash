#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <ssh-host>" >&2
  exit 2
fi

host=$1

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
scp -O "$script_dir/camdash-motor.cgi" "$host:/tmp/camdash-motor.cgi"

ssh "$host" sh -s -- <<'REMOTE'
set -eu
stamp=$(date +%Y%m%d-%H%M%S)

[ ! -f /var/www/x/camdash-motor.cgi ] || cp /var/www/x/camdash-motor.cgi "/var/www/x/camdash-motor.cgi.camdash-$stamp"
cp /tmp/camdash-motor.cgi /var/www/x/camdash-motor.cgi
chmod 755 /var/www/x/camdash-motor.cgi
rm -f /tmp/camdash-motor.cgi

echo "Configured PTZ health endpoint; backup: /var/www/x/camdash-motor.cgi.camdash-$stamp"
REMOTE
