# CAM Dashboard

CAM Dashboard is a LAN-hosted, event-driven dashboard for multiple ONVIF and Thingino cameras. It provides an event Gallery, low-latency Live view, PTZ controls, retained Local media, configuration, logs, image analysis, and rule-based alerts.

## Capture flow

Thingino cameras publish normalized motion events to an authenticated Mosquitto listener. Other ONVIF cameras can use PullPoint event subscriptions. Each trigger starts a main-stream RTSP recording and captures three main-stream snapshots one second apart. All snapshots are analyzed, detections are merged, and alert rules run once for the event.

`ch0` is the main/high-resolution stream. `ch1` is the low-resolution stream used by default for Thingino Live view. Snapshot endpoints are used for discrete stills; MJPEG is used only for low-latency browser viewing.

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

Camera passwords, HTTP tokens, broker passwords, private addresses, and personal notification settings must never be committed. The Settings API masks stored secrets and preserves them when a password field is left blank.

## Deployment

1. Clone the repository on the target Linux host and create `.venv` from `requirements.txt`.
2. Copy `config.example.yaml` to `~/.camdash/config.yaml`, replace placeholders locally, and set mode `0600`.
3. Copy `.env.example` to `~/.config/camdash/camdash.env`, populate optional secrets, and set mode `0600`.
4. Customize `deploy/camdash.service.example`, install it as `camdash.service`, then enable and start it.
5. Merge the authenticated listener example into Mosquitto without changing existing listeners. Create one write-only user per camera and one read-only service user. Validate the broker config before restarting it.
6. Configure each Thingino camera with `scripts/configure-thingino.sh`. The script backs up both changed camera files before updating MQTT and SD motion-copy settings.
7. Enter ONVIF credentials for non-Thingino cameras in Settings, run Probe, and enable the camera only after media and event services succeed.

The dashboard binds to port 8081 by default. It has no application login and must remain on a trusted LAN.

