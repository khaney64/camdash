from __future__ import annotations

import asyncio
import collections
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .alerter import AlertEngine
from .cameras import CameraError, adapter_for, discover_onvif
from .capture import CaptureManager, safe_unlink
from . import analyzer
from .config import AppConfig, CameraConfig, load_config, merge_public_update, public_config, save_config
from .db import Database
from .event_sources import MqttSource, OnvifEventSource


class RingHandler(logging.Handler):
    def __init__(self, maximum: int = 500):
        super().__init__()
        self.entries: collections.deque[dict[str, Any]] = collections.deque(maxlen=maximum)

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        message = _redact(message)
        self.entries.append({
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname, "logger": record.name, "message": message,
        })


RING = RingHandler()
RING.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(RING)
logging.getLogger().setLevel(os.environ.get("CAMDASH_LOG_LEVEL", "INFO"))
LOG = logging.getLogger("camdash")


class PtzRequest(BaseModel):
    command: str
    coarse: bool = False


class AppState:
    def __init__(self):
        self.config, self.config_path = load_config()
        self._prepare_dirs()
        log_path = self.config.root / "logs" / "camdash.log"
        if not any(isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == log_path for h in logging.getLogger().handlers):
            file_handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            logging.getLogger().addHandler(file_handler)
        self.db = Database(self.config.root / "camdash.db")
        self.listeners: set[asyncio.Queue] = set()
        self.capture = CaptureManager(self.db, lambda: self.config, self.broadcast)
        self.mqtt = MqttSource(lambda: self.config, self.capture.trigger)
        self.onvif = OnvifEventSource(lambda: self.config, self.capture.trigger)
        self.retention_task: asyncio.Task | None = None
        self.hls: dict[str, tuple[asyncio.subprocess.Process, Path]] = {}

    def _prepare_dirs(self) -> None:
        for name in ("media", "saved", "logs", "hls"):
            (self.config.root / name).mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.mqtt.start(loop)
        self.onvif.start(loop)
        self.retention_task = asyncio.create_task(self._retention_loop())
        LOG.info("CAM Dashboard started")

    async def stop(self) -> None:
        self.mqtt.stop()
        self.onvif.stop()
        if self.retention_task:
            self.retention_task.cancel()
        for camera_id in list(self.hls):
            await self.stop_hls(camera_id)
        self.db.close()

    async def restart_sources(self) -> None:
        self.mqtt.stop()
        self.onvif.stop()
        self.mqtt = MqttSource(lambda: self.config, self.capture.trigger)
        self.onvif = OnvifEventSource(lambda: self.config, self.capture.trigger)
        loop = asyncio.get_running_loop()
        self.mqtt.start(loop)
        self.onvif.start(loop)

    async def broadcast(self, event: dict[str, Any]) -> None:
        for queue in list(self.listeners):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def _retention_loop(self) -> None:
        while True:
            try:
                await self.capture.apply_retention()
            except Exception:
                LOG.exception("retention pass failed")
            await asyncio.sleep(max(60, self.config.retention.interval_minutes * 60))

    async def start_hls(self, camera_id: str, main: bool = False) -> dict[str, Any]:
        if camera_id in self.hls and self.hls[camera_id][0].returncode is None:
            return {"active": True, "playlist": f"/api/cameras/{camera_id}/hls/index.m3u8"}
        camera = self.config.camera(camera_id)
        rtsp = adapter_for(camera).rtsp_url(main)
        directory = self.config.root / "hls" / camera_id
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        codec = await asyncio.to_thread(_rtsp_codec, rtsp)
        video_args = ["-c:v", "copy"] if codec == "h264" else ["-c:v", "h264_v4l2m2m", "-b:v", "1800k"]
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", rtsp,
                   "-an", *video_args, "-f", "hls", "-hls_time", "1", "-hls_list_size", "5",
                   "-hls_flags", "delete_segments+append_list", str(directory / "index.m3u8")]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        self.hls[camera_id] = (process, directory)
        await self.broadcast({"type": "live", "camera_id": camera_id, "active": True})
        return {"active": True, "playlist": f"/api/cameras/{camera_id}/hls/index.m3u8"}

    async def stop_hls(self, camera_id: str) -> None:
        current = self.hls.pop(camera_id, None)
        if current:
            process, directory = current
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
            shutil.rmtree(directory, ignore_errors=True)
            await self.broadcast({"type": "live", "camera_id": camera_id, "active": False})


STATE: AppState | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global STATE
    STATE = AppState()
    await STATE.start()
    try:
        yield
    finally:
        await STATE.stop()
        STATE = None


app = FastAPI(title="CAM Dashboard", version="0.1.0", lifespan=lifespan)


def state() -> AppState:
    if STATE is None:
        raise HTTPException(503, "application is starting")
    return STATE


@app.get("/api/status")
async def status():
    s = state()
    return {
        "ok": True, "version": "0.1.0", "mqtt": {"connected": s.mqtt.connected, "error": s.mqtt.last_error},
        "onvif": s.onvif.health, "captures": {key: value[0] for key, value in s.capture.inflight.items()},
        "camera_count": len(s.config.cameras),
    }


@app.get("/api/updates")
async def updates(request: Request):
    s = state()
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    s.listeners.add(queue)

    async def generate():
        try:
            yield "data: " + json.dumps({"type": "ready"}) + "\n\n"
            while not await request.is_disconnected():
                try:
                    item = await asyncio.wait_for(queue.get(), 15)
                    yield "data: " + json.dumps(item, default=str) + "\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            s.listeners.discard(queue)
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/settings")
async def get_settings():
    return public_config(state().config)


@app.put("/api/settings")
async def put_settings(payload: dict[str, Any] = Body(...)):
    s = state()
    try:
        updated = merge_public_update(s.config, payload)
        save_config(updated, s.config_path)
        s.config = updated
        s.capture.alerts = AlertEngine(updated.root / "alerts.yaml")
        await s.restart_sources()
        await s.broadcast({"type": "settings_update"})
        return public_config(updated)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/cameras")
async def cameras():
    return public_config(state().config)["cameras"]


@app.post("/api/cameras/discover")
async def discover():
    return {"devices": await asyncio.to_thread(discover_onvif)}


@app.get("/api/cameras/{camera_id}/probe")
async def probe(camera_id: str):
    try:
        result = await asyncio.to_thread(adapter_for(state().config.camera(camera_id)).probe)
        return {name: getattr(result, name) for name in result.__slots__}
    except KeyError as exc:
        raise HTTPException(404, "camera not found") from exc


@app.get("/api/cameras/{camera_id}/sd")
async def sd_status(camera_id: str):
    try:
        return await asyncio.to_thread(adapter_for(state().config.camera(camera_id)).sd_status)
    except KeyError as exc:
        raise HTTPException(404, "camera not found") from exc
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/cameras/{camera_id}/ptz")
async def ptz(camera_id: str, payload: PtzRequest):
    try:
        camera = state().config.camera(camera_id)
        if not camera.ptz:
            raise HTTPException(409, "PTZ is disabled for this camera")
        adapter = adapter_for(camera)
        try:
            await asyncio.to_thread(adapter.ptz, payload.command, payload.coarse)
        except CameraError:
            if camera.adapter == "thingino":
                raise
            raise
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(404, "camera not found") from exc
    except CameraError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/cameras/{camera_id}/live/info")
async def live_info(camera_id: str):
    try:
        camera = state().config.camera(camera_id)
        return {"camera_id": camera_id, "mjpeg": camera.adapter == "thingino", "ptz": camera.ptz,
                "mjpeg_url": f"/api/cameras/{camera_id}/live.mjpg", "hls": f"/api/cameras/{camera_id}/hls/index.m3u8"}
    except KeyError as exc:
        raise HTTPException(404, "camera not found") from exc


@app.get("/api/cameras/{camera_id}/live.mjpg")
async def live_mjpeg(camera_id: str, hd: bool = False):
    try:
        response = await asyncio.to_thread(adapter_for(state().config.camera(camera_id)).open_mjpeg, hd)
    except KeyError as exc:
        raise HTTPException(404, "camera not found") from exc
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc

    def chunks():
        try:
            yield from response.iter_content(64 * 1024)
        finally:
            response.close()
    content_type = response.headers.get("content-type", "multipart/x-mixed-replace; boundary=frame")
    return StreamingResponse(chunks(), media_type=content_type)


@app.post("/api/cameras/{camera_id}/hls/start")
async def hls_start(camera_id: str, hd: bool = False):
    try:
        return await state().start_hls(camera_id, hd)
    except KeyError as exc:
        raise HTTPException(404, "camera not found") from exc
    except Exception as exc:
        raise HTTPException(502, _redact(str(exc))) from exc


@app.post("/api/cameras/{camera_id}/hls/stop")
async def hls_stop(camera_id: str):
    await state().stop_hls(camera_id)
    return {"ok": True}


@app.get("/api/cameras/{camera_id}/hls/{filename}")
async def hls_file(camera_id: str, filename: str):
    if Path(filename).name != filename or not filename.endswith((".m3u8", ".ts")):
        raise HTTPException(400, "invalid HLS filename")
    path = state().config.root / "hls" / camera_id / filename
    if not path.exists():
        raise HTTPException(404, "HLS segment not ready")
    return FileResponse(path, media_type="application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t",
                        headers={"Cache-Control": "no-store"})


@app.post("/api/cameras/{camera_id}/capture/snapshot")
async def manual_snapshot(camera_id: str):
    try:
        event = await state().capture.trigger(camera_id, "manual", source_key=str(uuid.uuid4()), snapshots=1, video_seconds=0)
        return JSONResponse(event, status_code=202)
    except (KeyError, CameraError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/cameras/{camera_id}/capture/clip")
async def manual_clip(camera_id: str, seconds: int = Query(30, ge=1, le=3600)):
    try:
        event = await state().capture.trigger(camera_id, "manual", source_key=str(uuid.uuid4()), snapshots=1, video_seconds=seconds)
        return JSONResponse(event, status_code=202)
    except (KeyError, CameraError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/events")
async def list_events(camera_id: str | None = None, status: str | None = None, q: str | None = None,
                      limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    return {"events": state().db.list_events(camera_id=camera_id, status=status, query=q, limit=limit, offset=offset)}


@app.get("/api/events/{event_id}")
async def get_event(event_id: str):
    event = state().db.get_event(event_id)
    if not event:
        raise HTTPException(404, "event not found")
    return event


@app.delete("/api/events/{event_id}")
async def delete_event(event_id: str):
    s = state()
    event = s.db.delete_event(event_id)
    if not event:
        raise HTTPException(404, "event not found")
    s.capture.delete_event_files(event)
    await s.broadcast({"type": "event_deleted", "event_id": event_id})
    return {"ok": True}


@app.get("/api/media/{media_id}/thumb")
async def media_thumb(media_id: str):
    return _file_for_media(state().db.get_media(media_id), "thumb_path")


@app.get("/api/media/{media_id}/file")
async def media_file(media_id: str):
    return _file_for_media(state().db.get_media(media_id), "path")


@app.post("/api/media/{media_id}/save")
async def save_media(media_id: str):
    media = state().db.get_media(media_id)
    if not media:
        raise HTTPException(404, "media not found")
    try:
        item = await asyncio.to_thread(state().capture.save, media)
        await state().broadcast({"type": "saved", "item": item})
        return item
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/media/{media_id}/analyze")
async def analyze_media(media_id: str):
    s = state()
    media = s.db.get_media(media_id)
    if not media or media.get("kind") != "snapshot" or not media.get("path"):
        raise HTTPException(404, "analyzable snapshot not found")
    try:
        camera = s.config.camera(media["camera_id"])
        prompt = camera.prompt_override.strip() or s.config.analysis.prompt
        result = await asyncio.to_thread(analyzer.analyze_image, Path(media["path"]), s.config.analysis, prompt)
        s.db.update_media(media_id, analyzed=True, analysis_json=result)
        event = s.db.get_event(media["event_id"])
        aggregated = analyzer.aggregate([m["analysis"] for m in event["media"] if m.get("analysis")])
        s.db.update_event(media["event_id"], analysis_json=aggregated)
        await s.broadcast({"type": "analysis_update", "event_id": media["event_id"], "media_id": media_id, "analysis": result})
        return result
    except KeyError as exc:
        raise HTTPException(404, "camera no longer configured") from exc


@app.post("/api/media/{media_id}/chat")
async def chat_media(media_id: str, payload: dict[str, Any] = Body(...)):
    media = state().db.get_media(media_id)
    prompt = str(payload.get("prompt", "")).strip()
    if not media or media.get("kind") != "snapshot" or not media.get("path"):
        raise HTTPException(404, "snapshot not found")
    if not prompt or len(prompt) > 4000:
        raise HTTPException(422, "prompt must contain 1-4000 characters")
    result = await asyncio.to_thread(analyzer.analyze_image, Path(media["path"]), state().config.analysis, prompt)
    return result


@app.get("/api/saved")
async def list_saved():
    return {"items": state().db.list_saved()}


@app.get("/api/saved/{saved_id}/thumb")
async def saved_thumb(saved_id: str):
    return _file_for_media(state().db.get_saved(saved_id), "thumb_path")


@app.get("/api/saved/{saved_id}/file")
async def saved_file(saved_id: str):
    return _file_for_media(state().db.get_saved(saved_id), "path")


@app.delete("/api/saved/{saved_id}")
async def delete_saved(saved_id: str):
    item = state().db.delete_saved(saved_id)
    if not item:
        raise HTTPException(404, "saved item not found")
    safe_unlink(item.get("path"), state().config.root)
    safe_unlink(item.get("thumb_path"), state().config.root)
    return {"ok": True}


@app.get("/api/logs")
async def logs():
    return {"entries": list(RING.entries)}


@app.post("/api/alerts/test")
async def test_alert():
    try:
        await asyncio.to_thread(state().capture.alerts.test_email)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


STATIC = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


@app.get("/")
async def root():
    return FileResponse(STATIC / "index.html")


@app.get("/{full_path:path}")
async def spa(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(STATIC / "index.html")


def _file_for_media(item: dict[str, Any] | None, field: str):
    if not item or not item.get(field):
        raise HTTPException(404, "file not found")
    path = Path(item[field])
    if not path.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(path, media_type=item.get("mime_type") or mimetypes.guess_type(path.name)[0], filename=None)


def _rtsp_codec(url: str) -> str:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", url],
            capture_output=True, text=True, timeout=15, check=True,
        )
        return result.stdout.strip().lower()
    except Exception:
        return "h264"


def _redact(value: str) -> str:
    import re
    value = re.sub(r"(rtsp://)[^@\s]+@", r"\1<redacted>@", value)
    value = re.sub(r"([?&]token=)[^&\s]+", r"\1<redacted>", value, flags=re.I)
    value = re.sub(r"(?i)(password|token|api[_-]?key)([\"']?\s*[:=]\s*[\"']?)[^\s,}\"']+", r"\1\2<redacted>", value)
    return value
