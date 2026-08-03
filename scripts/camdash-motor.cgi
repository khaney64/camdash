#!/bin/sh
# Authenticated Thingino motor health and positioning helper for CAM Dashboard.

. /var/www/x/auth.sh
require_auth

printf 'Content-Type: application/json\r\n'
printf 'Cache-Control: no-store\r\n'
printf 'Connection: close\r\n'
printf '\r\n'

motor_pid() {
	pid=$(cat /var/run/motors-daemon 2>/dev/null || true)
	if [ -z "$pid" ] || [ ! -r "/proc/$pid/stat" ]; then
		pid=$(pidof motors-daemon 2>/dev/null | awk '{print $1}')
	fi
	printf '%s' "$pid"
}

motor_health() {
	restarted=${1:-false}
	pid=$(motor_pid)
	if [ -z "$pid" ] || [ ! -r "/proc/$pid/stat" ]; then
		printf '{"supported":true,"healthy":false,"state":"missing","restartable":false,"requires_reboot":true,"restarted":%s,"error":"motor daemon is not running"}\n' "$restarted"
		return
	fi

	state=$(awk '{print $3}' "/proc/$pid/stat" 2>/dev/null || echo unknown)
	if [ "$state" = "D" ]; then
		printf '{"supported":true,"healthy":false,"state":"D","pid":%s,"restartable":false,"requires_reboot":true,"restarted":%s,"error":"motor daemon is blocked in kernel wait"}\n' "$pid" "$restarted"
		return
	fi
	payload=$(timeout 3 motors -j 2>/dev/null)
	if [ $? -ne 0 ] || [ -z "$payload" ]; then
		printf '{"supported":true,"healthy":false,"state":"%s","pid":%s,"restartable":false,"requires_reboot":true,"restarted":%s,"error":"motor status command timed out"}\n' "$state" "$pid" "$restarted"
		return
	fi

	case "$payload" in
		\{*\}) printf '{"supported":true,"healthy":true,"state":"%s","pid":%s,"restartable":false,"requires_reboot":false,"restarted":%s,"motor":%s}\n' "$state" "$pid" "$restarted" "$payload" ;;
		*) printf '{"supported":true,"healthy":false,"state":"%s","pid":%s,"restartable":false,"requires_reboot":true,"restarted":%s,"error":"motor status returned invalid data"}\n' "$state" "$pid" "$restarted" ;;
	esac
}

query_param() {
	printf '%s' "$QUERY_STRING" | tr '&' '\n' | sed -n "s/^$1=//p" | head -1
}

center_motors() {
	pos_0=$(jct /etc/thingino.json get motors.pos_0 2>/dev/null)
	x=$(printf '%s' "$pos_0" | cut -d, -f1)
	y=$(printf '%s' "$pos_0" | cut -d, -f2)
	case "$x:$y" in
		*[!0-9,:]*) printf '{"supported":true,"healthy":false,"requires_reboot":false,"error":"Thingino motor home position is invalid"}\n'; return ;;
		:) printf '{"supported":true,"healthy":false,"requires_reboot":false,"error":"Thingino motor home position is not configured"}\n'; return ;;
	esac

	payload=$(timeout 3 motors -j 2>/dev/null) || {
		motor_health false
		return
	}
	invert=$(printf '%s' "$payload" | sed -n 's/.*"invert":"\([0-9][0-9]*\)".*/\1/p')
	[ -n "$invert" ] || invert=0
	restore_x=false
	restore_y=false
	case "$invert" in 1|3) motors -I x >/dev/null; restore_x=true ;; esac
	case "$invert" in 2|3) motors -I y >/dev/null; restore_y=true ;; esac
	restore_inversion() {
		[ "$restore_x" = false ] || motors -I x >/dev/null 2>&1
		[ "$restore_y" = false ] || motors -I y >/dev/null 2>&1
	}
	trap restore_inversion EXIT HUP INT TERM

	motors -d h -x "$x" -y "$y" >/dev/null 2>&1 || {
		restore_inversion
		trap - EXIT HUP INT TERM
		printf '{"supported":true,"healthy":false,"requires_reboot":false,"error":"motor center command failed"}\n'
		return
	}
	i=0
	while [ "$(motors -b 2>/dev/null)" = 1 ] && [ "$i" -lt 45 ]; do
		sleep 1
		i=$((i + 1))
	done
	restore_inversion
	trap - EXIT HUP INT TERM
	motor_health false
}

case "$(query_param action)" in
	center) center_motors ;;
	*) motor_health false ;;
esac
