from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import paho.mqtt.client as mqtt

from .config import AppConfig, CameraConfig


LOG = logging.getLogger(__name__)
Trigger = Callable[..., Awaitable[dict[str, Any]]]


class MqttSource:
    def __init__(self, config_getter: Callable[[], AppConfig], trigger: Trigger):
        self.config_getter = config_getter
        self.trigger = trigger
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: mqtt.Client | None = None
        self.connected = False
        self.last_error = ""

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        cfg = self.config_getter().mqtt
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="camdash-service", clean_session=True)
        if cfg.username:
            client.username_pw_set(cfg.username, cfg.password)
        if cfg.tls:
            client.tls_set()
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self.client = client
        try:
            client.connect_async(cfg.host, cfg.port, keepalive=60)
            client.loop_start()
        except Exception as exc:
            self.last_error = str(exc)
            LOG.exception("MQTT startup failed")

    def stop(self) -> None:
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected = reason_code == 0
        if self.connected:
            client.subscribe(self.config_getter().mqtt.topic, qos=1)
            self.last_error = ""
            LOG.info("MQTT connected and subscribed")
        else:
            self.last_error = f"connect reason {reason_code}"
            LOG.error("MQTT connection failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        self.connected = False
        if reason_code:
            self.last_error = f"disconnect reason {reason_code}"
            LOG.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, message) -> None:
        if not self.loop:
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            camera_id = str(payload.get("camera_id") or _camera_from_topic(message.topic))
            event = str(payload.get("event", "motion")).lower()
            if event != "motion":
                return
            configured = self.config_getter().camera(camera_id)
            if configured.mqtt_topic and configured.mqtt_topic != message.topic:
                raise ValueError("topic does not match camera configuration")
            timestamp = str(payload.get("timestamp", ""))
            source_key = f"{message.topic}:{timestamp}:{event}"
            asyncio.run_coroutine_threadsafe(
                self.trigger(camera_id, "mqtt", source_key=source_key, triggered_at=timestamp), self.loop,
            )
        except Exception as exc:
            self.last_error = str(exc)
            LOG.warning("rejected MQTT message topic=%s: %s", message.topic, exc)


class OnvifEventSource:
    def __init__(self, config_getter: Callable[[], AppConfig], trigger: Trigger):
        self.config_getter = config_getter
        self.trigger = trigger
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.health: dict[str, dict[str, Any]] = {}

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.stop_event.clear()
        for camera in self.config_getter().cameras:
            if camera.adapter == "onvif" and camera.enabled and not camera.needs_credentials:
                thread = threading.Thread(target=self._worker, args=(camera,), daemon=True, name=f"onvif-{camera.id}")
                self.threads.append(thread)
                thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self.threads.clear()

    def _worker(self, camera: CameraConfig) -> None:
        while not self.stop_event.is_set():
            try:
                from onvif import ONVIFCamera
                client = ONVIFCamera(camera.host, camera.onvif_port, camera.username, camera.password)
                pullpoint = client.create_pullpoint_service()
                self.health[camera.id] = {"connected": True, "error": ""}
                while not self.stop_event.is_set():
                    response = pullpoint.PullMessages({"Timeout": "PT20S", "MessageLimit": 32})
                    for notification in getattr(response, "NotificationMessage", []) or []:
                        if _notification_is_motion(notification):
                            source_key = _notification_key(camera.id, notification)
                            if self.loop:
                                asyncio.run_coroutine_threadsafe(
                                    self.trigger(camera.id, "onvif", source_key=source_key, triggered_at=None), self.loop,
                                )
            except Exception as exc:
                self.health[camera.id] = {"connected": False, "error": str(exc)}
                LOG.warning("ONVIF event worker %s: %s", camera.id, exc)
                self.stop_event.wait(10)


def _notification_is_motion(notification: Any) -> bool:
    text = str(notification).lower()
    if "motion" not in text and "videoanalytics" not in text:
        return False
    return any(marker in text for marker in ("value': 'true", "value=true", ">true<", "isMotion=true".lower()))


def _notification_key(camera_id: str, notification: Any) -> str:
    value = str(notification)
    return f"{camera_id}:{hashlib.sha256(value.encode()).hexdigest()}"


def _camera_from_topic(topic: str) -> str:
    parts = topic.split("/")
    if len(parts) == 4 and parts[:2] == ["camdash", "cameras"] and parts[3] == "motion":
        return parts[2]
    raise ValueError("invalid motion topic")
