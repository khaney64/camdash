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
            LOG.info("Capture: trigger coalesced camera=%s event=%s source=%s", camera_id, event_id, source)
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
            LOG.info("Capture: duplicate trigger ignored camera=%s source=%s", camera_id, source)
            return {"duplicate": True, "camera_id": camera_id, "source_key": source_key}
        duration = effective.day_video_seconds if profile == "day" else effective.night_video_seconds
        LOG.info(
            "Capture: trigger accepted camera=%s event=%s source=%s profile=%s snapshots=%d video_seconds=%d",
            camera_id, event_id, source, profile, effective.snapshots, duration,
        )
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
        LOG.info(
            "Capture: started camera=%s event=%s snapshots=%d interval_seconds=%s video_seconds=%d",
            camera.id, event_id, capture.snapshots, capture.snapshot_interval_seconds, duration,
        )
        record_main = camera.record_stream == "main"
        video_path = event_dir / "clip.mp4"
        video_started_at = utcnow()
        video_duration = 0.0
        if duration > 0:
            try:
                video = await self._record_video(
                    adapter.rtsp_url(record_main), event, event_dir, duration, camera.record_stream,
                )
                video_started_at = video["captured_at"]
                video_duration = float(video.get("duration_seconds") or 0)
            except Exception as exc:
                errors.append(f"video: {exc}")
                LOG.exception("video capture failed camera=%s event=%s", camera.id, event_id)
                if video_path.exists():
                    try:
                        video_duration = await _probe_duration(video_path)
                    except Exception:
                        LOG.exception("recorded video duration probe failed camera=%s event=%s", camera.id, event_id)

        use_video_snapshots = duration > 0 and video_path.exists()
        try:
            for index in range(capture.snapshots):
                if use_video_snapshots:
                    requested_offset = index * capture.snapshot_interval_seconds
                    offset = min(requested_offset, max(0.0, video_duration - 0.1))
                    media = await self._snapshot_from_video(
                        video_path, event, event_dir, index, offset, video_started_at,
                    )
                else:
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

        remove_reason: str | None = None
        if snapshots:
            try:
                remove_reason = await self._analyze(
                    event, camera, snapshots, video_path=video_path, video_duration=video_duration,
                    video_started_at=video_started_at, event_dir=event_dir,
                )
            except Exception as exc:
                errors.append(f"analysis: {exc}")
                LOG.exception("analysis failed event=%s", event_id)

        if remove_reason:
            current = self.db.get_event(event_id)
            if current:
                self.delete_event_files(current)
                self.db.delete_event(event_id)
            await self.broadcast({"type": "event_deleted", "event_id": event_id, "reason": remove_reason})
            LOG.info("Capture: event removed camera=%s event=%s reason=%s", camera.id, event_id, remove_reason)
            await asyncio.sleep(max(0, capture.cooldown_seconds))
            return

        current = self.db.get_event(event_id)
        has_media = bool(current and current.get("media"))
        status = "complete" if not errors else ("partial" if has_media else "failed")
        self.db.update_event(event_id, status=status, error="; ".join(errors) or None)
        await self.broadcast({"type": "event_complete", "event": self.db.get_event(event_id)})
        LOG.info(
            "Capture: finished camera=%s event=%s status=%s media=%d errors=%d",
            camera.id, event_id, status, len(current.get("media", [])) if current else 0, len(errors),
        )
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
        if row.get("path"):
            LOG.info(
                "Capture: snapshot %d saved event=%s media=%s bytes=%d",
                index + 1, event["id"], media_id, row.get("size_bytes", 0),
            )
        else:
            LOG.warning("Capture: snapshot %d failed event=%s error=%s", index + 1, event["id"], row.get("error"))
        return row

    async def _snapshot_from_video(self, video_path: Path, event: dict[str, Any], event_dir: Path, index: int,
                                   offset_seconds: float, video_started_at: str) -> dict[str, Any]:
        media_id = str(uuid.uuid4())
        captured_at = video_started_at
        path = event_dir / f"snapshot-{index + 1:02d}.jpg"
        thumb = event_dir / f"snapshot-{index + 1:02d}-thumb.jpg"
        try:
            captured_at = (
                datetime.fromisoformat(video_started_at) + timedelta(seconds=offset_seconds)
            ).isoformat()
            if not video_path.exists():
                raise CameraError("recorded video is unavailable")
            await _run_process([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
                "-ss", f"{offset_seconds:.3f}", "-frames:v", "1", "-q:v", "2", "-y", str(path),
            ], timeout=30)
            if not path.exists() or not path.stat().st_size:
                raise CameraError("video snapshot extraction produced no image")
            await asyncio.to_thread(make_thumbnail, path, thumb)
            row = {"id": media_id, "event_id": event["id"], "kind": "snapshot", "sequence": index,
                   "captured_at": captured_at, "path": str(path), "thumb_path": str(thumb),
                   "mime_type": "image/jpeg", "size_bytes": path.stat().st_size}
        except Exception as exc:
            row = {"id": media_id, "event_id": event["id"], "kind": "snapshot", "sequence": index,
                   "captured_at": captured_at, "mime_type": "image/jpeg", "error": str(exc)}
        self.db.add_media(row)
        if row.get("path"):
            LOG.info(
                "Capture: snapshot %d derived from video event=%s media=%s offset_seconds=%.3f bytes=%d",
                index + 1, event["id"], media_id, offset_seconds, row.get("size_bytes", 0),
            )
        else:
            LOG.warning(
                "Capture: snapshot %d video extraction failed event=%s offset_seconds=%.3f error=%s",
                index + 1, event["id"], offset_seconds, row.get("error"),
            )
        return row

    async def _record_video(self, rtsp_url: str, event: dict[str, Any], event_dir: Path, duration: int,
                            stream: str = "main") -> dict[str, Any]:
        media_id = str(uuid.uuid4())
        path = event_dir / "clip.mp4"
        temporary = event_dir / "clip.tmp.mp4"
        captured_at = utcnow()
        LOG.info(
            "Capture: video recording started event=%s stream=%s duration_seconds=%d",
            event["id"], stream, duration,
        )
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
                   "-use_wallclock_as_timestamps", "1", "-i", rtsp_url,
                   "-t", str(duration), "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart",
                   "-y", str(temporary)]
        error = ""
        actual_duration = 0.0
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
            actual_duration = await _probe_duration(path)
            if actual_duration < duration * 0.8:
                error = f"recording ended early: requested {duration}s, actual {actual_duration:.2f}s"
        except Exception as exc:
            error = str(exc)
            temporary.unlink(missing_ok=True)
        row = {"id": media_id, "event_id": event["id"], "kind": "video", "sequence": 100,
               "captured_at": captured_at, "path": str(path) if path.exists() else None, "mime_type": "video/mp4",
               "size_bytes": path.stat().st_size if path.exists() else 0, "duration_seconds": actual_duration,
               "error": error or None}
        self.db.add_media(row)
        await self.broadcast({"type": "video_complete", "event_id": event["id"], "media_id": media_id, "error": error})
        if error:
            LOG.warning("Capture: video recording failed event=%s error=%s", event["id"], error)
            raise CameraError(error)
        LOG.info(
            "Capture: video saved event=%s media=%s stream=%s duration_seconds=%.2f bytes=%d",
            event["id"], media_id, stream, actual_duration, row["size_bytes"],
        )
        return row

    async def _analyze(self, event: dict[str, Any], camera: CameraConfig, snapshots: list[dict[str, Any]],
                       video_path: Path | None = None, video_duration: float = 0.0,
                       video_started_at: str | None = None, event_dir: Path | None = None) -> str | None:
        cfg = self.config_getter()
        prompt = camera.prompt_override.strip() or cfg.analysis.prompt
        all_media = list(snapshots)
        analyzable = [media for media in all_media if media.get("path")]
        LOG.info("Analysis: processing %d image(s) camera=%s event=%s", len(analyzable), camera.id, event["id"])
        results = await self._analyze_images(event, camera, analyzable, prompt, cfg)

        if (
            cfg.analysis.remove_empty_images
            and video_path is not None and event_dir is not None and video_started_at is not None
            and video_duration > 0 and video_path.exists()
            and not analyzer.aggregate(results).get("detections")
        ):
            LOG.info(
                "Analysis: initial images empty, scanning video every %ds camera=%s event=%s",
                cfg.analysis.empty_scan_interval_seconds, camera.id, event["id"],
            )
            scanned_media = await self._scan_video_interval(
                video_path, event, event_dir, video_duration, video_started_at,
                cfg.analysis.empty_scan_interval_seconds, start_index=len(all_media),
            )
            if scanned_media:
                all_media.extend(scanned_media)
                scan_analyzable = [media for media in scanned_media if media.get("path")]
                LOG.info(
                    "Analysis: processing %d additional scanned image(s) camera=%s event=%s",
                    len(scan_analyzable), camera.id, event["id"],
                )
                results.extend(await self._analyze_images(event, camera, scan_analyzable, prompt, cfg))

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
        if cfg.analysis.remove_empty_images and results and not labels:
            LOG.info("Analysis: empty event marked for removal camera=%s event=%s", camera.id, event["id"])
            return "empty"
        if cfg.analysis.remove_person_only_images and labels and labels <= {"person", "human", "legs"}:
            LOG.info("Analysis: person-only event marked for removal camera=%s event=%s", camera.id, event["id"])
            return "person_only"
        event_with_analysis = self.db.get_event(event["id"]) or event
        media_list = event_with_analysis.get("media", [])
        primary = _select_detection_media(media_list)
        if primary and primary.get("id") and primary["id"] != event_with_analysis.get("primary_media_id"):
            self.db.update_event(event["id"], primary_media_id=primary["id"])
            event_with_analysis["primary_media_id"] = primary["id"]
        if cfg.analysis.alerts_enabled:
            thumb = Path(primary["thumb_path"]) if primary and primary.get("thumb_path") else None
            alert = await asyncio.to_thread(self.alerts.evaluate, event_with_analysis, thumb,
                                            cfg.analysis.alert_cooldown_minutes * 60,
                                            cfg.analysis.alert_rules_enabled)
            self.db.update_event(event["id"], alert_json=alert)
            await self.broadcast({"type": "alert_update", "event_id": event["id"], "alert": alert})
            LOG.info(
                "Alert: evaluated camera=%s event=%s sent=%s matched=%s",
                camera.id, event["id"], bool(alert.get("sent")), bool(alert.get("matched")),
            )
        LOG.info(
            "Analysis: done camera=%s event=%s images=%d detections=%s errors=%d",
            camera.id, event["id"], len(results), _detection_summary(aggregate), len(failures),
        )
        return None

    async def _analyze_images(self, event: dict[str, Any], camera: CameraConfig, media_list: list[dict[str, Any]],
                              prompt: str, cfg: AppConfig) -> list[dict[str, Any]]:
        results = []
        total = len(media_list)
        for index, media in enumerate(media_list, 1):
            LOG.info("Analysis: image %d/%d analyzing camera=%s event=%s media=%s", index, total, camera.id, event["id"], media["id"])
            result = await asyncio.to_thread(analyzer.analyze_image, Path(media["path"]), cfg.analysis, prompt)
            results.append(result)
            self.db.update_media(media["id"], analyzed=True, analysis_json=result)
            await self.broadcast({"type": "analysis_update", "event_id": event["id"], "media_id": media["id"], "analysis": result})
            detections = _detection_summary(result)
            if result.get("error"):
                LOG.warning("Analysis: image %d/%d failed camera=%s media=%s error=%s", index, total, camera.id, media["id"], result["error"])
            else:
                LOG.info("Analysis: image %d/%d complete camera=%s media=%s detections=%s", index, total, camera.id, media["id"], detections)
        return results

    async def _scan_video_interval(self, video_path: Path, event: dict[str, Any], event_dir: Path,
                                   video_duration: float, video_started_at: str,
                                   interval_seconds: int, start_index: int) -> list[dict[str, Any]]:
        media: list[dict[str, Any]] = []
        offset = float(interval_seconds)
        index = start_index
        while offset < video_duration - 0.1:
            media.append(await self._snapshot_from_video(video_path, event, event_dir, index, offset, video_started_at))
            index += 1
            offset += interval_seconds
        return media

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


def _detection_summary(result: dict[str, Any]) -> str:
    values = []
    for detection in result.get("detections", []) or []:
        label = str(detection.get("label", "")).strip()
        name = str(detection.get("name", "")).strip()
        if label:
            values.append(f"{label}/{name}" if name else label)
    return ", ".join(values) or "none"


def _select_detection_media(media_list: list[dict[str, Any]]) -> dict[str, Any] | None:
    with_detection = next(
        (m for m in media_list if m.get("thumb_path") and (m.get("analysis") or {}).get("detections")), None,
    )
    return with_detection or next((m for m in media_list if m.get("thumb_path")), None)


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


async def _probe_duration(path: Path) -> float:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, stderr = await asyncio.wait_for(process.communicate(), 20)
    if process.returncode:
        raise CameraError(stderr.decode(errors="replace")[-500:] or "unable to read recorded video duration")
    try:
        return float(out.decode().strip())
    except ValueError as exc:
        raise CameraError("unable to read recorded video duration") from exc


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
