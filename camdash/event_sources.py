from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .cameras import CameraDisabledError
from .config import AppConfig


LOG = logging.getLogger(__name__)
Trigger = Callable[..., Awaitable[dict[str, Any]]]


class MotionSuppressor:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def suppress(self, camera_id: str, seconds: float) -> None:
        until = self._clock() + max(0.0, seconds)
        with self._lock:
            self._until[camera_id] = max(until, self._until.get(camera_id, 0.0))

    def clear(self, camera_id: str) -> None:
        with self._lock:
            self._until.pop(camera_id, None)

    def remaining(self, camera_id: str) -> float:
        now = self._clock()
        with self._lock:
            remaining = self._until.get(camera_id, 0.0) - now
            if remaining <= 0:
                self._until.pop(camera_id, None)
                return 0.0
            return remaining


class WebhookSource:
    def __init__(self, config_getter: Callable[[], AppConfig], trigger: Trigger,
                 motion_suppressor: MotionSuppressor | None = None):
        self.config_getter = config_getter
        self.trigger = trigger
        self.motion_suppressor = motion_suppressor
        self.last_received_at: str | None = None
        self.last_camera_id: str | None = None
        self.last_error: str = ""

    async def handle(self, camera_id: str, raw_body: bytes, content_type: str) -> dict[str, Any]:
        received_at = datetime.now(timezone.utc).isoformat()
        LOG.info(
            "Webhook: notification received camera=%s content_type=%s bytes=%d %s",
            camera_id, content_type, len(raw_body), _describe_payload(raw_body),
        )
        self.last_received_at = received_at
        self.last_camera_id = camera_id
        remaining = self.motion_suppressor.remaining(camera_id) if self.motion_suppressor else 0.0
        if remaining > 0:
            LOG.info(
                "Webhook: motion suppressed after PTZ camera=%s remaining_seconds=%.2f",
                camera_id, remaining,
            )
            return {"duplicate": True, "suppressed": True}
        try:
            result = await self.trigger(camera_id, "webhook", source_key=str(uuid.uuid4()), triggered_at=received_at)
        except CameraDisabledError:
            LOG.info("Webhook: ignored, camera disabled camera=%s", camera_id)
            self.last_error = ""
            return {"ignored": True, "reason": "disabled", "camera_id": camera_id}
        except Exception as exc:
            self.last_error = str(exc)
            raise
        self.last_error = ""
        if result.get("duplicate"):
            LOG.info("Webhook: duplicate trigger ignored camera=%s", camera_id)
        else:
            LOG.info("Webhook: trigger routed camera=%s event=%s", camera_id, result.get("id", "unknown"))
        return result


def _describe_payload(raw_body: bytes) -> str:
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"body={raw_body[:2000]!r}"
    if not isinstance(payload, dict):
        return f"body={raw_body[:2000]!r}"
    reported_camera = payload.get("camera") or payload.get("camera_name") or payload.get("deviceName")
    return (
        f"reported_camera={reported_camera!r} time={payload.get('time')!r} "
        f"event={payload.get('event')!r} thumbnail={payload.get('thumbnail')!r}"
    )
