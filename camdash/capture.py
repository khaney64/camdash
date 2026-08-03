from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image

from . import analyzer
from .alerter import AlertEngine
from .cameras import CameraError, adapter_for
from .config import AppConfig, CameraConfig, CaptureConfig
from .db import Database, utcnow


LOG = logging.getLogger(__name__)
Broadcast = Callable[[dict[str, Any]], Awaitable[None]]


class CaptureManager:
    def __init__(self, db: Database, config_getter: Callable[[], AppConfig], broadcast: Broadcast,
                 alerts_path: Path | None = None):
        self.db = db
        self.config_getter = config_getter
        self.broadcast = broadcast
        self.inflight: dict[str, tuple[str, asyncio.Task]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.alerts = AlertEngine(alerts_path or self.config_getter().root / "alerts.yaml")

    async def trigger(self, camera_id: str, source: str, source_key: str | None = None,
                      triggered_at: str | None = None, snapshots: int | None = None,
                      video_seconds: int | None = None) -> dict[str, Any]:
        cfg = self.config_getter()
        camera = cfg.camera(camera_id)
        if not camera.enabled or camera.needs_credentials:
            raise CameraError("camera is disabled or needs credentials")
        if camera_id in self.inflight:
            event_id, _ = self.inflight[camera_id]
            self.db.increment_trigger(event_id)
            await self.broadcast({"type": "event_trigger", "event_id": event_id, "camera_id": camera_id, "coalesced": True})
            return self.db.get_event(event_id) or {"id": event_id}

        received = datetime.now(timezone.utc)
        triggered = _parse_timestamp(triggered_at, received)
        profile, effective = capture_profile(cfg, camera, triggered)
        if snapshots is not None:
            effective.snapshots = snapshots
        if video_seconds is not None:
            effective.day_video_seconds = effective.night_video_seconds = video_seconds
        event_id = str(uuid.uuid4())
        row = {
            "id": event_id, "camera_id": camera.id, "camera_name": camera.name, "source": source,
            "source_key": source_key, "triggered_at": triggered.isoformat(), "received_at": received.isoformat(),
            "profile": profile, "status": "capturing", "created_at": received.isoformat(),
        }
        if not self.db.create_event(row):
            return {"duplicate": True, "camera_id": camera_id, "source_key": source_key}
        task = asyncio.create_task(self._run(row, camera, effective), name=f"capture-{camera_id}-{event_id}")
        self.inflight[camera_id] = (event_id, task)
        task.add_done_callback(lambda _: self.inflight.pop(camera_id, None))
        await self.broadcast({"type": "event_created", "event": self.db.get_event(event_id)})
        return self.db.get_event(event_id) or row

    async def wait_for_event(self, event_id: str, timeout: float = 120) -> dict[str, Any] | None:
        for _, (current_id, task) in list(self.inflight.items()):
            if current_id == event_id:
                await asyncio.wait_for(asyncio.shield(task), timeout)
                break
        return self.db.get_event(event_id)

    async def _run(self, event: dict[str, Any], camera: CameraConfig, capture: CaptureConfig) -> None:
        event_id = event["id"]
        root = self.config_getter().root
        date = datetime.fromisoformat(event["triggered_at"]).astimezone().strftime("%Y/%m/%d")
        event_dir = root / "media" / date / event_id
        event_dir.mkdir(parents=True, exist_ok=True)
        adapter = adapter_for(camera)
        duration = capture.day_video_seconds if event["profile"] == "day" else capture.night_video_seconds
        errors: list[str] = []
        snapshots: list[dict[str, Any]] = []
        video_task = asyncio.create_task(self._record_video(adapter.rtsp_url(True), event, event_dir, duration)) if duration > 0 else None
        try:
            for index in range(capture.snapshots):
                if index:
                    await asyncio.sleep(capture.snapshot_interval_seconds)
                media = await self._snapshot(adapter, event, event_dir, index)
                snapshots.append(media)
                if index == 0 and media.get("path"):
                    self.db.update_event(event_id, primary_media_id=media["id"])
                await self.broadcast({"type": "capture_progress", "event_id": event_id, "snapshot": index + 1,
                                      "snapshot_total": capture.snapshots})
        except Exception as exc:
            errors.append(f"snapshots: {exc}")
            LOG.exception("snapshot capture failed camera=%s event=%s", camera.id, event_id)

        remove_person_only = False
        if snapshots:
            try:
                remove_person_only = await self._analyze(event, camera, snapshots)
            except Exception as exc:
                errors.append(f"analysis: {exc}")
                LOG.exception("analysis failed event=%s", event_id)

        if video_task:
            try:
                await video_task
            except Exception as exc:
                errors.append(f"video: {exc}")
                LOG.exception("video capture failed camera=%s event=%s", camera.id, event_id)

        if remove_person_only:
            current = self.db.get_event(event_id)
            if current:
                self.delete_event_files(current)
                self.db.delete_event(event_id)
            await self.broadcast({"type": "event_deleted", "event_id": event_id, "reason": "person_only"})
            await asyncio.sleep(max(0, capture.cooldown_seconds))
            return

        current = self.db.get_event(event_id)
        has_media = bool(current and current.get("media"))
        status = "complete" if not errors else ("partial" if has_media else "failed")
        self.db.update_event(event_id, status=status, error="; ".join(errors) or None)
        await self.broadcast({"type": "event_complete", "event": self.db.get_event(event_id)})
        await asyncio.sleep(max(0, capture.cooldown_seconds))

    async def _snapshot(self, adapter, event: dict[str, Any], event_dir: Path, index: int) -> dict[str, Any]:
        media_id = str(uuid.uuid4())
        captured_at = utcnow()
        path = event_dir / f"snapshot-{index + 1:02d}.jpg"
        thumb = event_dir / f"snapshot-{index + 1:02d}-thumb.jpg"
        try:
            content, mime = await asyncio.to_thread(adapter.fetch_snapshot, True)
            await asyncio.to_thread(path.write_bytes, content)
            await asyncio.to_thread(make_thumbnail, path, thumb)
            row = {"id": media_id, "event_id": event["id"], "kind": "snapshot", "sequence": index,
                   "captured_at": captured_at, "path": str(path), "thumb_path": str(thumb), "mime_type": mime,
                   "size_bytes": path.stat().st_size}
        except Exception as exc:
            row = {"id": media_id, "event_id": event["id"], "kind": "snapshot", "sequence": index,
                   "captured_at": captured_at, "mime_type": "image/jpeg", "error": str(exc)}
        self.db.add_media(row)
        return row

    async def _record_video(self, rtsp_url: str, event: dict[str, Any], event_dir: Path, duration: int) -> dict[str, Any]:
        media_id = str(uuid.uuid4())
        path = event_dir / "clip.mp4"
        temporary = event_dir / "clip.tmp.mp4"
        captured_at = utcnow()
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", rtsp_url,
                   "-t", str(duration), "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart",
                   "-y", str(temporary)]
        error = ""
        try:
            await _run_process(command, timeout=duration + 35)
            codec = await _probe_codec(temporary)
            if codec != "h264":
                converted = event_dir / "clip.h264.mp4"
                try:
                    await _run_process(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(temporary), "-c:v",
                                        "h264_v4l2m2m", "-c:a", "aac", "-movflags", "+faststart", "-y", str(converted)], timeout=duration * 4 + 60)
                except Exception:
                    await _run_process(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(temporary), "-c:v",
                                        "libx264", "-preset", "veryfast", "-c:a", "aac", "-movflags", "+faststart", "-y", str(converted)], timeout=duration * 6 + 60)
                temporary.unlink(missing_ok=True)
                temporary = converted
            os.replace(temporary, path)
        except Exception as exc:
            error = str(exc)
            temporary.unlink(missing_ok=True)
        row = {"id": media_id, "event_id": event["id"], "kind": "video", "sequence": 100,
               "captured_at": captured_at, "path": str(path) if path.exists() else None, "mime_type": "video/mp4",
               "size_bytes": path.stat().st_size if path.exists() else 0, "duration_seconds": duration,
               "error": error or None}
        self.db.add_media(row)
        await self.broadcast({"type": "video_complete", "event_id": event["id"], "media_id": media_id, "error": error})
        if error:
            raise CameraError(error)
        return row

    async def _analyze(self, event: dict[str, Any], camera: CameraConfig, snapshots: list[dict[str, Any]]) -> bool:
        cfg = self.config_getter()
        prompt = camera.prompt_override.strip() or cfg.analysis.prompt
        results = []
        for media in snapshots:
            if not media.get("path"):
                continue
            result = await asyncio.to_thread(analyzer.analyze_image, Path(media["path"]), cfg.analysis, prompt)
            results.append(result)
            self.db.update_media(media["id"], analyzed=True, analysis_json=result)
            await self.broadcast({"type": "analysis_update", "event_id": event["id"], "media_id": media["id"], "analysis": result})
        aggregate = analyzer.aggregate(results)
        self.db.update_event(event["id"], analysis_json=aggregate)
        failures = [str(result["error"]) for result in results if result.get("error")]
        if results and len(failures) == len(results):
            raise RuntimeError(f"all {len(results)} image analyses failed: {failures[0]}")
        labels = {
            str(detection.get("label", "")).lower()
            for detection in aggregate.get("detections", [])
            if detection.get("label")
        }
        if cfg.analysis.remove_person_only_images and labels and labels <= {"person", "human", "legs"}:
            LOG.info("removing person-only event camera=%s event=%s", camera.id, event["id"])
            return True
        event_with_analysis = self.db.get_event(event["id"]) or event
        if cfg.analysis.alerts_enabled:
            first = next((Path(m["thumb_path"]) for m in snapshots if m.get("thumb_path")), None)
            alert = await asyncio.to_thread(self.alerts.evaluate, event_with_analysis, first,
                                            cfg.analysis.alert_cooldown_minutes * 60,
                                            cfg.analysis.alert_rules_enabled)
            self.db.update_event(event["id"], alert_json=alert)
            await self.broadcast({"type": "alert_update", "event_id": event["id"], "alert": alert})
        return False

    def delete_event_files(self, event: dict[str, Any]) -> None:
        for media in event.get("media", []):
            for field in ("path", "thumb_path"):
                safe_unlink(media.get(field), self.config_getter().root)

    def save(self, media: dict[str, Any]) -> dict[str, Any]:
        if not media.get("path"):
            raise FileNotFoundError("media file is unavailable")
        saved_id = str(uuid.uuid4())
        target_dir = self.config_getter().root / "saved" / saved_id
        target_dir.mkdir(parents=True, exist_ok=True)
        source = Path(media["path"])
        target = target_dir / source.name
        shutil.copy2(source, target)
        thumb_target = None
        if media.get("thumb_path") and Path(media["thumb_path"]).exists():
            thumb_target = target_dir / "thumb.jpg"
            shutil.copy2(media["thumb_path"], thumb_target)
        row = {"id": saved_id, "source_media_id": media["id"], "source_event_id": media["event_id"],
               "camera_id": media["camera_id"], "camera_name": media["camera_name"], "kind": media["kind"],
               "saved_at": utcnow(), "path": str(target), "thumb_path": str(thumb_target) if thumb_target else None,
               "mime_type": media.get("mime_type"), "size_bytes": target.stat().st_size, "analysis": media.get("analysis")}
        self.db.save_media(row)
        return self.db.get_saved(saved_id) or row

    async def apply_retention(self) -> int:
        cfg = self.config_getter()
        cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.retention.days)
        candidates = self.db.retention_candidates(cutoff, cfg.retention.max_gb * 1024**3)
        for item in candidates:
            event = self.db.delete_event(item["id"])
            if event:
                self.delete_event_files(event)
        if candidates:
            await self.broadcast({"type": "retention", "deleted": len(candidates)})
        return len(candidates)


def capture_profile(cfg: AppConfig, camera: CameraConfig, timestamp: datetime) -> tuple[str, CaptureConfig]:
    base = {name: getattr(cfg.capture, name) for name in cfg.capture.__slots__}
    overrides = {key: value for key, value in camera.capture.items() if key in base}
    effective = CaptureConfig(**{**base, **overrides})
    try:
        zone = ZoneInfo(cfg.timezone)
    except ZoneInfoNotFoundError:
        LOG.warning("timezone data unavailable for %s; using UTC", cfg.timezone)
        zone = timezone.utc
    local = timestamp.astimezone(zone)
    start_h, start_m = map(int, effective.day_starts.split(":"))
    end_h, end_m = map(int, effective.night_starts.split(":"))
    minute = local.hour * 60 + local.minute
    start, end = start_h * 60 + start_m, end_h * 60 + end_m
    is_day = start <= minute < end if start <= end else minute >= start or minute < end
    return ("day" if is_day else "night"), effective


def make_thumbnail(source: Path, target: Path) -> None:
    with Image.open(source) as image:
        image = image.convert("RGB")
        image.thumbnail((640, 480))
        image.save(target, "JPEG", quality=82, optimize=True)


async def _probe_codec(path: Path) -> str:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await asyncio.wait_for(process.communicate(), 20)
    return out.decode().strip().lower()


async def _run_process(command: list[str], timeout: float) -> None:
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise CameraError("ffmpeg timed out")
    if process.returncode:
        message = stderr.decode(errors="replace")[-1000:]
        message = _redact_process_error(message)
        raise CameraError(message or f"ffmpeg exited {process.returncode}")


def _redact_process_error(value: str) -> str:
    import re
    value = re.sub(r"(rtsp://)[^@\s]+@", r"\1<redacted>@", value)
    return re.sub(r"([?&]token=)[^&\s]+", r"\1<redacted>", value, flags=re.I)


def safe_unlink(value: str | None, root: Path) -> None:
    if not value:
        return
    path = Path(value).resolve()
    resolved_root = root.resolve()
    if resolved_root not in path.parents:
        raise ValueError("refusing to delete outside data directory")
    path.unlink(missing_ok=True)
    parent = path.parent
    while parent != resolved_root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _parse_timestamp(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        if str(value).isdigit():
            parsed = datetime.fromtimestamp(int(value), timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        if abs((parsed - fallback).total_seconds()) > 86400:
            return fallback
        return parsed
    except (ValueError, OSError, OverflowError):
        return fallback
