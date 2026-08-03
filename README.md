# CAM Dashboard

CAM Dashboard is a LAN-hosted, event-driven dashboard for multiple ONVIF and Thingino cameras. It provides an event Gallery, low-latency Live view, PTZ controls, retained Local media, configuration, logs, image analysis, and rule-based alerts.

## Capture flow

Thingino cameras publish normalized motion events to an authenticated Mosquitto listener. Other ONVIF cameras can use PullPoint event subscriptions. Each trigger starts a main-stream RTSP recording and captures three main-stream snapshots one second apart. All snapshots are analyzed, detections are merged, and alert rules run once for the event.

`ch0` is the main/high-resolution stream. `ch1` is the low-resolution stream used by default for Thingino Live view. Snapshot endpoints are used for discrete stills; MJPEG is used only for low-latency browser viewing.

## Live view and PTZ

Thingino Live view uses one warm upstream MJPEG connection per camera and channel. CAM Dashboard fans complete, latest-only frames from that connection to every browser viewer, so opening the same camera on multiple machines does not consume one camera stream per viewer. The relay reconnects after upstream failures and can fall back to the camera's alternate MJPEG channel. RTSP is independent: multiple clients, including Surveillance Station, can connect concurrently subject to the camera's resource limits.

For Thingino PTZ cameras, Settings displays motor health and the current motor position. The center command reads the camera's persistent `motors.pos_0` value from `/etc/thingino.json`; this is the position used after camera reboots as well as by CAM Dashboard. Center and double-click-center operations use a longer timeout because an absolute move can take substantially longer than a directional step.

CAM Dashboard suppresses capture events while Thingino reports that its motors are moving. Commands issued by CAM Dashboard also suppress motion events for five seconds after movement, reducing false alerts caused by the camera moving through the scene. Thingino firmware can leave the motor daemon missing, blocked in kernel wait, or unresponsive; Settings reports these states. CAM Dashboard does not attempt an unsafe daemon restart and reports when the camera must be rebooted.

## Development

```shell
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
cp config.example.yaml ~/.camdash/config.yaml
export CAMDASH_CONFIG="$HOME/.camdash/config.yaml"
uvicorn camdash.main:app --reload --port 8081
pytest
```

The application requires `ffmpeg` and `ffprobe` for RTSP recording and HLS.

## Local configuration and secrets

- Runtime configuration: `~/.camdash/config.yaml`, mode `0600`.
- Optional environment file: `~/.config/camdash/camdash.env`, mode `0600`.
- Alert rules: `~/.camdash/alerts.yaml`.
- Database and media: `~/.camdash/`.

The Settings page exposes the Gardepro-style alert controls: global enablement, configured email status and test delivery, per-rule cooldown and switches, plus optional removal of newly analyzed person-only events. Person alerts and person-only removal are mutually exclusive. Rule definitions and SMTP credentials remain server-local.

Analysis settings expose backend-specific model fields, prompt, maximum output tokens, thinking budget (zero disables thinking), temperature, and image chat. Empty responses and results whose detections all have confidence 3 or lower are retried once with twice the configured thinking budget; the configured value is not changed.

Camera passwords, HTTP tokens, broker passwords, private addresses, and personal notification settings must never be committed. The Settings API masks stored secrets and preserves them when a password field is left blank.

## Deployment

1. Clone the repository on the target Linux host and create `.venv` from `requirements.txt`.
2. Copy `config.example.yaml` to `~/.camdash/config.yaml`, replace placeholders locally, and set mode `0600`.
3. Copy `.env.example` to `~/.config/camdash/camdash.env`, populate optional secrets, and set mode `0600`.
4. Customize `deploy/camdash.service.example`, install it as `camdash.service`, then enable and start it.
5. Merge the authenticated listener example into Mosquitto without changing existing listeners. Create one write-only user per camera and one read-only service user. Validate the broker config before restarting it.
6. Configure each Thingino camera with `scripts/configure-thingino.sh`. The script backs up changed camera files, installs the camera-side `camdash-motion` callback and authenticated `camdash-motor.cgi` health/centering helper, and updates MQTT and SD motion-copy settings. The callback publishes CAM Dashboard MQTT notifications immediately; local SD media processing continues afterward. Home Assistant integration is optional.
7. Enter ONVIF credentials for non-Thingino cameras in Settings, run Probe, and enable the camera only after media and event services succeed.

The dashboard binds to port 8081 by default. It has no application login and must remain on a trusted LAN.
