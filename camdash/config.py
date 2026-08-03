from __future__ import annotations

import copy
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


CAMERA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
SECRET_FIELDS = {"password", "token", "mqtt_password"}


@dataclass(slots=True)
class CaptureConfig:
    snapshots: int = 3
    snapshot_interval_seconds: float = 1.0
    day_video_seconds: int = 30
    night_video_seconds: int = 30
    day_starts: str = "07:00"
    night_starts: str = "19:00"
    cooldown_seconds: int = 5


@dataclass(slots=True)
class RetentionConfig:
    days: int = 30
    max_gb: int = 50
    interval_minutes: int = 60


@dataclass(slots=True)
class MqttConfig:
    host: str = "mqtt.example.internal"
    port: int = 1884
    username: str = ""
    password: str = ""
    topic: str = "camdash/cameras/+/motion"
    tls: bool = False


@dataclass(slots=True)
class AnalysisConfig:
    enabled: bool = False
    backend: str = "llm"
    llm_url: str = ""
    llm_model: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"
    prompt: str = (
        "Analyze this security-camera image. Return JSON with keys description and "
        "detections, where detections is a list of objects containing label and confidence 0-10."
    )
    max_tokens: int = 1024
    thinking_budget: int = 2048
    temperature: float = 0.1
    chat_enabled: bool = False
    alerts_enabled: bool = False
    alert_cooldown_minutes: int = 30
    alert_rules_enabled: dict[str, bool] = field(default_factory=dict)
    remove_person_only_images: bool = False


@dataclass(slots=True)
class CameraConfig:
    id: str
    name: str
    host: str
    adapter: str = "onvif"
    enabled: bool = True
    needs_credentials: bool = False
    onvif_port: int = 80
    username: str = ""
    password: str = ""
    token: str = ""
    rtsp_main: str = ""
    rtsp_sub: str = ""
    snapshot_main: str = ""
    snapshot_sub: str = ""
    mjpeg_main: str = ""
    mjpeg_sub: str = ""
    record_stream: str = "main"
    mqtt_topic: str = ""
    prompt_override: str = ""
    ptz: bool = False
    sd_redundancy: bool = False
    capture: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not CAMERA_ID_RE.fullmatch(self.id):
            raise ValueError("camera id must be a lowercase slug")
        if not self.name.strip() or not self.host.strip():
            raise ValueError("camera name and host are required")
        if self.adapter not in {"thingino", "onvif"}:
            raise ValueError("adapter must be thingino or onvif")
        if self.record_stream not in {"main", "sub"}:
            raise ValueError("record stream must be main or sub")


@dataclass(slots=True)
class AppConfig:
    timezone: str = "America/New_York"
    data_dir: str = "~/.camdash"
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    cameras: list[CameraConfig] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return Path(self.data_dir).expanduser()

    def camera(self, camera_id: str) -> CameraConfig:
        for camera in self.cameras:
            if camera.id == camera_id:
                return camera
        raise KeyError(camera_id)


def _dataclass_from(cls, value: dict[str, Any] | None):
    value = value or {}
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in value.items() if k in allowed})


def load_config(path: Path | None = None) -> tuple[AppConfig, Path]:
    target = path or Path(os.environ.get("CAMDASH_CONFIG", "~/.camdash/config.yaml")).expanduser()
    raw: dict[str, Any] = {}
    if target.exists():
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    cfg = AppConfig(
        timezone=str(raw.get("timezone", "America/New_York")),
        data_dir=str(raw.get("data_dir", "~/.camdash")),
        capture=_dataclass_from(CaptureConfig, raw.get("capture")),
        retention=_dataclass_from(RetentionConfig, raw.get("retention")),
        mqtt=_dataclass_from(MqttConfig, raw.get("mqtt")),
        analysis=_dataclass_from(AnalysisConfig, raw.get("analysis")),
        cameras=[_dataclass_from(CameraConfig, item) for item in raw.get("cameras", [])],
    )
    seen: set[str] = set()
    for camera in cfg.cameras:
        camera.validate()
        if camera.id in seen:
            raise ValueError(f"duplicate camera id: {camera.id}")
        seen.add(camera.id)
    _validate(cfg)
    return cfg, target


def _validate(cfg: AppConfig) -> None:
    if not 1 <= cfg.capture.snapshots <= 20:
        raise ValueError("snapshots must be between 1 and 20")
    if not 0 <= cfg.capture.snapshot_interval_seconds <= 60:
        raise ValueError("snapshot interval must be between 0 and 60 seconds")
    for value in (cfg.capture.day_video_seconds, cfg.capture.night_video_seconds):
        if not 1 <= value <= 3600:
            raise ValueError("video duration must be between 1 and 3600 seconds")
    if cfg.mqtt.port not in range(1, 65536):
        raise ValueError("invalid MQTT port")
    if not 0 <= cfg.analysis.alert_cooldown_minutes <= 1440:
        raise ValueError("alert cooldown must be between 0 and 1440 minutes")
    if cfg.analysis.backend not in {"llm", "anthropic"}:
        raise ValueError("analysis backend must be llm or anthropic")
    if not 100 <= cfg.analysis.max_tokens <= 4096:
        raise ValueError("analysis max tokens must be between 100 and 4096")
    if not 0 <= cfg.analysis.thinking_budget <= 32768:
        raise ValueError("analysis thinking budget must be between 0 and 32768")
    if not 0 <= cfg.analysis.temperature <= 1:
        raise ValueError("analysis temperature must be between 0 and 1")
    person_enabled = next(
        (enabled for name, enabled in cfg.analysis.alert_rules_enabled.items() if name.lower() == "person"),
        True,
    )
    if cfg.analysis.remove_person_only_images and person_enabled:
        raise ValueError("person alerts and person-only image removal cannot both be enabled")


def save_config(cfg: AppConfig, path: Path) -> None:
    _validate(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(asdict(cfg), sort_keys=False, allow_unicode=True)
    fd, temporary = tempfile.mkstemp(prefix="config-", suffix=".yaml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def public_config(cfg: AppConfig) -> dict[str, Any]:
    result = asdict(cfg)
    result["mqtt"]["has_password"] = bool(result["mqtt"].pop("password", ""))
    for camera in result["cameras"]:
        for key in ("password", "token"):
            camera[f"has_{key}"] = bool(camera.pop(key, ""))
        for key in ("rtsp_main", "rtsp_sub", "snapshot_main", "snapshot_sub", "mjpeg_main", "mjpeg_sub"):
            if camera.get(key):
                camera[key] = _redact_url(camera[key])
    return result


def merge_public_update(current: AppConfig, data: dict[str, Any]) -> AppConfig:
    raw = asdict(current)
    for section in ("capture", "retention", "analysis"):
        if isinstance(data.get(section), dict):
            raw[section].update(data[section])
    if isinstance(data.get("mqtt"), dict):
        password = raw["mqtt"].get("password", "")
        raw["mqtt"].update({k: v for k, v in data["mqtt"].items() if k != "has_password"})
        if not data["mqtt"].get("password"):
            raw["mqtt"]["password"] = password
    if "timezone" in data:
        raw["timezone"] = data["timezone"]
    if isinstance(data.get("cameras"), list):
        existing = {c["id"]: c for c in raw["cameras"]}
        merged = []
        for item in data["cameras"]:
            base = copy.deepcopy(existing.get(item.get("id"), {}))
            for key, value in item.items():
                if key.startswith("has_"):
                    continue
                if key in SECRET_FIELDS and value == "":
                    continue
                base[key] = value
            merged.append(base)
        raw["cameras"] = merged
    cfg = AppConfig(
        timezone=raw["timezone"], data_dir=raw["data_dir"],
        capture=_dataclass_from(CaptureConfig, raw["capture"]),
        retention=_dataclass_from(RetentionConfig, raw["retention"]),
        mqtt=_dataclass_from(MqttConfig, raw["mqtt"]),
        analysis=_dataclass_from(AnalysisConfig, raw["analysis"]),
        cameras=[_dataclass_from(CameraConfig, c) for c in raw["cameras"]],
    )
    ids = [camera.id for camera in cfg.cameras]
    if len(ids) != len(set(ids)):
        raise ValueError("camera ids must be unique")
    for camera in cfg.cameras:
        camera.validate()
    _validate(cfg)
    return cfg


def _redact_url(value: str) -> str:
    value = re.sub(r"(://)[^/@]+@", r"\1<redacted>@", value)
    return re.sub(r"([?&]token=)[^&]+", r"\1<redacted>", value, flags=re.I)
