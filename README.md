# CAM Dashboard

CAM Dashboard is a LAN-hosted, event-driven dashboard for multiple ONVIF and Thingino cameras. It provides an event Gallery, low-latency Live view, PTZ controls, retained Local media, configuration, logs, image analysis, and rule-based alerts.

## Capture flow

Synology Surveillance Station does motion detection for every camera and calls a CAM Dashboard webhook (one URL per camera, authenticated with a shared secret) when motion fires. The camera is identified from the URL path, not the request body, since Surveillance Station's payload schema isn't documented reliably; the raw payload (reported camera name, event text, thumbnail URL) is logged for diagnostics so a misrouted Action Rule is easy to spot during setup. Each trigger records the configured RTSP stream, then derives snapshots from the completed local clip at the configured interval. This avoids competing camera snapshot requests while recording. Derived snapshots become available after recording finishes; they are then analyzed, detections are merged, and alert rules run once for the event.

`ch0` is the main/high-resolution stream. `ch1` is the low-resolution stream used by default for Thingino Live view and event recording. Camera snapshot endpoints remain available for manual snapshot-only requests but are not used during recorded events. MJPEG is used only for low-latency browser viewing.

## Live view and PTZ

Thingino Live view uses one warm upstream MJPEG connection per camera and channel. CAM Dashboard fans complete, latest-only frames from that connection to every browser viewer, so opening the same camera on multiple machines does not consume one camera stream per viewer. The relay reconnects after upstream failures and can fall back to the camera's alternate MJPEG channel. ONVIF cameras without an MJPEG endpoint (e.g. the Reolink) use HLS instead, transcoding RTSP through ffmpeg. Both the MJPEG relay and HLS session for a camera release the upstream camera connection automatically after 30 seconds with no viewers — switching tabs, changing the HD toggle, or closing the browser all stop it immediately, and the idle timeout is a backstop for cases the client can't signal (a closed tab has no way to tell the server it's gone). This matters because RTSP recording competes with live view for the same limited concurrent-stream budget most cameras enforce; an orphaned live session can otherwise cause a concurrent recording to come up short. RTSP itself is independent: multiple clients, including Surveillance Station, can connect concurrently subject to the camera's resource limits.

For Thingino PTZ cameras, Settings displays motor health and the current motor position. The center command reads the camera's persistent `motors.pos_0` value from `/etc/thingino.json`; this is the position used after camera reboots as well as by CAM Dashboard. Center and double-click-center operations use a longer timeout because an absolute move can take substantially longer than a directional step.

CAM Dashboard suppresses capture events while Thingino reports that its motors are moving. Commands issued by CAM Dashboard also suppress webhook-triggered events for five seconds after movement, reducing false alerts caused by the camera moving through the scene. Thingino firmware can leave the motor daemon missing, blocked in kernel wait, or unresponsive; Settings reports these states. CAM Dashboard does not attempt an unsafe daemon restart and reports when the camera must be rebooted.

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

The Settings page exposes the Gardepro-style alert controls: global enablement, configured email status and test delivery, per-rule cooldown and switches, plus optional removal of newly analyzed person-only events. Person alerts and person-only removal are mutually exclusive. Alert cooldown is scoped per camera per rule, so a rule firing for one camera does not silence that same rule for another camera. Rule definitions and SMTP credentials remain server-local.

"Remove empty images" deletes the whole event (clip and all snapshots) when analysis finds nothing at all — useful for cameras whose own IR/night-vision LEDs occasionally trigger motion detection with nothing actually in frame. The initial snapshots are checked first; only if those come up empty does CAM Dashboard extract and analyze additional frames from the recorded clip at the configured scan interval (default every 5 seconds) before deciding the event is truly empty, so a subject that's only visible partway through the clip isn't mistakenly discarded.

Analysis settings expose backend-specific model fields, prompt, maximum output tokens, thinking budget (zero disables thinking), temperature, and image chat. Invalid responses (empty, truncated, or non-JSON — the model failed to produce usable output) and results whose detections all have confidence 3 or lower are each retried once with twice the configured thinking budget; the configured value is not changed. A well-formed response reporting no detections is not retried — it's a valid result, not a failure.

Camera passwords, HTTP tokens, the webhook shared secret, private addresses, and personal notification settings must never be committed. The Settings API masks stored secrets and preserves them when a password field is left blank.

## Deployment

1. Clone the repository on the target Linux host and create `.venv` from `requirements.txt`.
2. Copy `config.example.yaml` to `~/.camdash/config.yaml`, replace placeholders locally, and set mode `0600`.
3. Copy `.env.example` to `~/.config/camdash/camdash.env`, populate optional secrets, and set mode `0600`.
4. Customize `deploy/camdash.service.example`, install it as `camdash.service`, then enable and start it.
5. Generate a webhook shared secret and set it in Settings (or directly in `~/.camdash/config.yaml`). In Synology Surveillance Station, enable each camera, configure its motion detection, and add one Action Rule per camera that calls `http://<camdash-host>:8081/api/webhooks/surveillance/<camera-id>?secret=<shared-secret>` on motion. `<camera-id>` must match the camera's `id` in CAM Dashboard config.
6. Configure each Thingino camera's PTZ health endpoint with `scripts/configure-thingino.sh`, which installs the authenticated `camdash-motor.cgi` helper. Once the Surveillance Station webhook is confirmed working for a camera, run `scripts/deprovision-motion.sh` against it to disable the camera's own (unreliable) IVS motion detection and MQTT publish, since Surveillance Station is now the sole motion-detection source.
7. Enter ONVIF credentials for non-Thingino cameras in Settings, run Probe, and enable the camera only after media and event services succeed.

The dashboard binds to port 8081 by default. It has no application login and must remain on a trusted LAN.

## Running and monitoring the service

CAM Dashboard runs as a single systemd service — one `uvicorn` process (see `deploy/camdash.service.example`). There's nothing else to manage: `ffmpeg`/`ffprobe` are invoked as short-lived subprocesses per recording or live-view session and clean themselves up automatically (including the idle timeout described above), not standalone services.

```shell
sudo systemctl status camdash    # current state, recent stdout/stderr
sudo systemctl start camdash
sudo systemctl stop camdash
sudo systemctl restart camdash   # e.g. after a git pull or editing config.yaml by hand
sudo systemctl enable camdash    # start on boot
sudo systemctl disable camdash
```

Settings changes made through the Settings tab or `PUT /api/settings` take effect immediately — no restart needed. A restart is only required after pulling new code or editing `config.yaml` directly on disk.

Logs are available three ways:
- `journalctl -u camdash -f` — systemd journal (process lifecycle, stdout/stderr).
- `~/.camdash/logs/camdash.log` — rotating application log (5 files x 5 MB); path follows the configured `data_dir` (default `~/.camdash`).
- The in-app **Logs** tab — recent entries from the same log, viewable from the browser without shell access.

## API reference

Everything is under `/api` and unauthenticated except the Surveillance Station webhook (shared-secret query parameter) — there is no login, by design (see Deployment). All responses are JSON. Examples below use the placeholder cameras from `config.example.yaml` and a placeholder host/IP.

### Status and live updates

`GET /api/status` — health, webhook activity, and in-progress captures.
```shell
curl http://camdash-host:8081/api/status
```
```json
{
  "ok": true,
  "version": "0.1.0",
  "webhook": {"last_received_at": "2026-01-15T20:14:03+00:00", "last_camera_id": "patio-camera", "error": ""},
  "captures": {},
  "camera_count": 2
}
```

`GET /api/updates` — Server-Sent Events stream the dashboard UI consumes for live updates (`event_created`, `capture_progress`, `analysis_update`, `alert_update`, `event_complete`, `event_deleted`, `settings_update`, `live`, `retention`). Intended for the browser client, not general scripting.

### Cameras and live view

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/cameras` | List configured cameras, secrets masked. |
| POST | `/api/cameras/discover` | ONVIF WS-Discovery scan of the local network. |
| GET | `/api/cameras/{id}/probe` | ONVIF service/profile discovery for one camera. |
| GET | `/api/cameras/{id}/sd` | SD-card redundancy status (Thingino only). |
| POST | `/api/cameras/{id}/ptz` | Body `{"command": "left\|right\|up\|down\|up-left\|up-right\|down-left\|down-right\|center\|home", "coarse": false}`. |
| GET | `/api/cameras/{id}/ptz/status` | Motor health (Thingino only). |
| GET | `/api/cameras/{id}/live/info?hd=false` | Resolved RTSP URL and whether MJPEG/PTZ apply. |
| GET | `/api/cameras/{id}/live.mjpg?hd=false` | MJPEG relay stream (multipart, for an `<img>` tag). |
| POST | `/api/cameras/{id}/hls/start?hd=false` | Start HLS live view (cameras without an MJPEG endpoint). |
| POST | `/api/cameras/{id}/hls/stop` | Stop it explicitly (also happens automatically after the idle timeout). |

```shell
curl http://camdash-host:8081/api/cameras
```
```json
[
  {
    "id": "patio-camera", "name": "Patio Camera", "host": "192.0.2.10", "adapter": "thingino",
    "enabled": true, "needs_credentials": false, "ptz": true, "record_stream": "sub",
    "has_password": true, "has_token": true
  }
]
```

### Manual capture and events

`POST /api/cameras/{id}/capture/snapshot` — trigger an immediate snapshot through the same pipeline as an automated event.
```shell
curl -X POST http://camdash-host:8081/api/cameras/patio-camera/capture/snapshot
```
```json
{
  "id": "e2b1c9a4-6f21-4e9d-8f7a-0c9d2b6a1234", "camera_id": "patio-camera", "camera_name": "Patio Camera",
  "source": "manual", "status": "capturing", "profile": "day", "trigger_count": 1,
  "triggered_at": "2026-01-15T20:14:03+00:00", "received_at": "2026-01-15T20:14:03+00:00",
  "created_at": "2026-01-15T20:14:03+00:00"
}
```
`POST /api/cameras/{id}/capture/clip?seconds=30` — same, plus a recorded clip.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/events?camera_id=&status=&q=&limit=50&offset=0` | Paginated event list. Response: `{"events": [...]}`. |
| GET | `/api/events/{id}` | Single event, including its `media` list (snapshots/clip, each with `path`/`thumb_path`/`analysis`). |
| DELETE | `/api/events/{id}` | Delete an event and its media files. |
| GET | `/api/media/{id}/thumb` / `.../file` | Serve a thumbnail or original media file. |
| POST | `/api/media/{id}/save` | Copy media into the Local tab (survives retention). |
| POST | `/api/media/{id}/analyze` | Re-run image analysis for one media item. |
| GET / POST | `/api/media/{id}/chat` | Ask a follow-up question about an already-analyzed image. |
| GET | `/api/saved`, `/api/saved/{id}/thumb\|file` | Local tab's saved items. |
| DELETE | `/api/saved/{id}` | Remove a saved item. |
| GET / PUT | `/api/settings` | Full config; secrets masked on read, preserved on write when left blank. |
| GET | `/api/logs` | Recent log lines backing the in-app Logs tab. |
| POST | `/api/alerts/test` | Send a test alert email using the configured SMTP settings. |

### Surveillance Station webhook

`POST /api/webhooks/surveillance/{camera_id}?secret=<shared_secret>`

The inbound trigger from a Synology Surveillance Station Action Rule (see Deployment). `{camera_id}` must match a camera's `id` in CAM Dashboard's config — **the camera is identified from the URL path, not the request body**, since Surveillance Station's payload schema differs across DSM versions and isn't reliably documented anywhere. The body can be anything, including empty; it's logged for diagnostics but never parsed as a requirement.

```shell
curl -X POST "http://camdash-host:8081/api/webhooks/surveillance/patio-camera?secret=changeme"
```
Response (`202`), same shape as manual capture, with `"source": "webhook"`. `401` if the secret is missing or wrong; `409` if the camera doesn't exist, is disabled, or still has `needs_credentials` set.

A real Surveillance Station Action Rule call looks roughly like this (exact fields vary by DSM version and event type):
```json
{
  "time": "2026-01-15T20:14:03",
  "camera": "Patio Camera",
  "event": "Motion detected",
  "thumbnail": "https://nas.example.lan:5001/webapi/SurveillanceStation/Webhook/GetThumbnail/v1/<token>/thumbnail.jpg"
}
```
`camera` is Surveillance Station's own display name for the camera, not necessarily CAM Dashboard's camera `id` — it's logged for diagnostics only, never used for routing. `thumbnail` is a pre-authenticated URL (the access token is embedded in the path itself, no separate API key needed); CAM Dashboard doesn't fetch it today, since it already records its own clip and snapshots independently once triggered.
