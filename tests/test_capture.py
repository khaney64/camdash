from datetime import datetime, timezone
from pathlib import Path

import pytest

from camdash.cameras import CameraError
from camdash.capture import CaptureManager, _parse_timestamp, _select_detection_media, capture_profile, safe_unlink
from camdash.config import AppConfig, CameraConfig, CaptureConfig


def test_select_detection_media_prefers_media_with_a_detection():
    media = [
        {"id": "m0", "thumb_path": "snap0.jpg", "analysis": {"detections": []}},
        {"id": "m1", "thumb_path": "video-frame-1.jpg", "analysis": {"detections": [{"label": "raccoon"}]}},
        {"id": "m2", "thumb_path": "video-frame-2.jpg", "analysis": {"detections": [{"label": "raccoon"}]}},
    ]
    assert _select_detection_media(media)["id"] == "m1"


def test_select_detection_media_falls_back_to_first_thumb_when_none_have_detections():
    media = [
        {"id": "m0", "thumb_path": "snap0.jpg", "analysis": {"detections": []}},
        {"id": "m1", "thumb_path": "snap1.jpg", "analysis": None},
    ]
    assert _select_detection_media(media)["id"] == "m0"


def test_select_detection_media_skips_media_without_a_thumb():
    media = [
        {"id": "m0", "thumb_path": None, "analysis": {"detections": [{"label": "raccoon"}]}},
        {"id": "m1", "thumb_path": "snap1.jpg", "analysis": {"detections": [{"label": "raccoon"}]}},
    ]
    assert _select_detection_media(media)["id"] == "m1"


def test_select_detection_media_returns_none_when_no_media_has_a_thumb():
    assert _select_detection_media([{"id": "m0", "thumb_path": None, "analysis": None}]) is None


def test_day_night_and_camera_override(tmp_path: Path):
    cfg = AppConfig(data_dir=str(tmp_path), cameras=[])
    camera = CameraConfig(id="cam-1", name="Cam", host="192.0.2.1", capture={"snapshots": 5})
    profile, effective = capture_profile(cfg, camera, datetime(2026, 1, 1, 15, tzinfo=timezone.utc))
    assert profile == "day"
    assert effective.snapshots == 5
    profile, _ = capture_profile(cfg, camera, datetime(2026, 1, 1, 2, tzinfo=timezone.utc))
    assert profile == "night"


def test_timestamp_rejects_large_clock_skew():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _parse_timestamp("0", now) == now


def test_safe_unlink_rejects_outside_root(tmp_path: Path):
    outside = tmp_path.parent / "outside.jpg"
    with pytest.raises(ValueError):
        safe_unlink(str(outside), tmp_path)


@pytest.mark.asyncio
async def test_person_only_analysis_requests_event_removal(tmp_path: Path, monkeypatch):
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.analysis.enabled = True
    cfg.analysis.remove_person_only_images = True
    cfg.analysis.alert_rules_enabled = {"Person": False}
    camera = CameraConfig(id="cam-1", name="Cam", host="192.0.2.1")

    class FakeDatabase:
        analysis = None

        def update_media(self, *args, **kwargs):
            pass

        def update_event(self, event_id, **values):
            self.analysis = values.get("analysis_json", self.analysis)

        def get_event(self, event_id):
            return {"id": event_id, "camera_name": "Cam", "triggered_at": "now", "analysis": self.analysis}

    async def broadcast(_message):
        pass

    monkeypatch.setattr("camdash.capture.analyzer.analyze_image", lambda *args: {
        "description": "A person is visible", "detections": [{"label": "person", "confidence": 9}]
    })
    manager = CaptureManager(FakeDatabase(), lambda: cfg, broadcast)
    remove = await manager._analyze(
        {"id": "event-1", "camera_name": "Cam", "triggered_at": "now"},
        camera,
        [{"id": "media-1", "path": str(tmp_path / "capture.jpg"), "thumb_path": None}],
    )
    assert remove == "person_only"


@pytest.mark.asyncio
async def test_analyze_promotes_media_with_detection_to_primary(tmp_path: Path, monkeypatch):
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.analysis.enabled = True
    camera = CameraConfig(id="cam-1", name="Cam", host="192.0.2.1")

    class FakeDatabase:
        def __init__(self, media):
            self.media = {m["id"]: dict(m) for m in media}
            self.primary_media_id = None
            self.event_analysis = None

        def update_media(self, media_id, **values):
            self.media[media_id].update(values)

        def update_event(self, event_id, **values):
            if "primary_media_id" in values:
                self.primary_media_id = values["primary_media_id"]
            if "analysis_json" in values:
                self.event_analysis = values["analysis_json"]

        def get_event(self, event_id):
            media = [
                {"id": m["id"], "thumb_path": m.get("thumb_path"), "analysis": m.get("analysis_json")}
                for m in self.media.values()
            ]
            return {
                "id": event_id, "camera_name": "Cam", "triggered_at": "now", "analysis": self.event_analysis,
                "primary_media_id": self.primary_media_id, "media": media,
            }

    async def broadcast(_message):
        pass

    results = iter([
        {"description": "nothing here", "detections": []},
        {"description": "raccoon spotted", "detections": [{"label": "raccoon", "confidence": 9}]},
    ])
    monkeypatch.setattr("camdash.capture.analyzer.analyze_image", lambda *args: next(results))

    database = FakeDatabase([
        {"id": "media-0", "thumb_path": "snap0-thumb.jpg"},
        {"id": "media-1", "thumb_path": "snap1-thumb.jpg"},
    ])
    manager = CaptureManager(database, lambda: cfg, broadcast)
    remove = await manager._analyze(
        {"id": "event-1", "camera_name": "Cam", "triggered_at": "now"},
        camera,
        [
            {"id": "media-0", "path": "snap0.jpg", "thumb_path": "snap0-thumb.jpg"},
            {"id": "media-1", "path": "snap1.jpg", "thumb_path": "snap1-thumb.jpg"},
        ],
    )
    assert remove is None
    assert database.primary_media_id == "media-1"


@pytest.mark.asyncio
async def test_short_video_uses_actual_duration_and_fails(tmp_path: Path, monkeypatch):
    cfg = AppConfig(data_dir=str(tmp_path))

    class FakeDatabase:
        row = None

        def add_media(self, row):
            self.row = row

    async def broadcast(_message):
        pass

    commands = []

    async def fake_run(command, timeout):
        commands.append(command)
        Path(command[-1]).write_bytes(b"video")

    async def fake_codec(_path):
        return "h264"

    async def fake_duration(_path):
        return 2.0

    monkeypatch.setattr("camdash.capture._run_process", fake_run)
    monkeypatch.setattr("camdash.capture._probe_codec", fake_codec)
    monkeypatch.setattr("camdash.capture._probe_duration", fake_duration)
    database = FakeDatabase()
    manager = CaptureManager(database, lambda: cfg, broadcast)

    with pytest.raises(CameraError, match="ended early"):
        await manager._record_video("rtsp://camera/ch1", {"id": "event-1"}, tmp_path, 30, "sub")

    assert database.row["duration_seconds"] == 2.0
    assert database.row["path"].endswith("clip.mp4")
    assert "requested 30s, actual 2.00s" in database.row["error"]
    assert commands[0][commands[0].index("-use_wallclock_as_timestamps") + 1] == "1"


@pytest.mark.asyncio
async def test_snapshot_is_derived_from_video_at_requested_offset(tmp_path: Path, monkeypatch):
    cfg = AppConfig(data_dir=str(tmp_path))
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")

    class FakeDatabase:
        row = None

        def add_media(self, row):
            self.row = row

    commands = []

    async def fake_run(command, timeout):
        commands.append((command, timeout))
        Path(command[-1]).write_bytes(b"jpeg")

    def fake_thumbnail(_source, target):
        target.write_bytes(b"thumbnail")

    async def broadcast(_message):
        pass

    monkeypatch.setattr("camdash.capture._run_process", fake_run)
    monkeypatch.setattr("camdash.capture.make_thumbnail", fake_thumbnail)
    database = FakeDatabase()
    manager = CaptureManager(database, lambda: cfg, broadcast)

    row = await manager._snapshot_from_video(
        video, {"id": "event-1"}, tmp_path, 1, 1.5, "2026-08-03T12:00:00+00:00",
    )

    command, timeout = commands[0]
    assert command[0] == "ffmpeg"
    assert command[command.index("-i") + 1] == str(video)
    assert command[command.index("-ss") + 1] == "1.500"
    assert "-frames:v" in command
    assert timeout == 30
    assert row["captured_at"] == "2026-08-03T12:00:01.500000+00:00"
    assert row["path"].endswith("snapshot-02.jpg")
    assert database.row == row


@pytest.mark.asyncio
async def test_video_snapshot_records_failure_for_invalid_start_timestamp(tmp_path: Path):
    cfg = AppConfig(data_dir=str(tmp_path))

    class FakeDatabase:
        row = None

        def add_media(self, row):
            self.row = row

    async def broadcast(_message):
        pass

    database = FakeDatabase()
    manager = CaptureManager(database, lambda: cfg, broadcast)
    row = await manager._snapshot_from_video(
        tmp_path / "clip.mp4", {"id": "event-1"}, tmp_path, 0, 0, "not-an-iso-timestamp",
    )

    assert row == database.row
    assert row["captured_at"] == "not-an-iso-timestamp"
    assert "Invalid isoformat string" in row["error"]


@pytest.mark.asyncio
async def test_video_recording_failure_falls_back_to_camera_snapshots(tmp_path: Path, monkeypatch):
    cfg = AppConfig(data_dir=str(tmp_path))
    camera = CameraConfig(id="cam-1", name="Cam", host="192.0.2.1")
    capture = CaptureConfig(snapshots=2, snapshot_interval_seconds=0, day_video_seconds=30,
                            night_video_seconds=30, cooldown_seconds=0)
    calls = []

    class FakeDatabase:
        def __init__(self):
            self.media = []

        def update_event(self, _event_id, **_values):
            pass

        def get_event(self, _event_id):
            return {"id": "event-1", "media": self.media}

    async def broadcast(_message):
        pass

    class FakeAdapter:
        def rtsp_url(self, _main):
            return "rtsp://camera/ch1"

    database = FakeDatabase()
    manager = CaptureManager(database, lambda: cfg, broadcast)

    async def failed_record(*_args):
        calls.append("record")
        raise CameraError("ffmpeg failed")

    async def camera_snapshot(_adapter, _event, _event_dir, index):
        calls.append(f"snapshot-{index}")
        row = {"id": f"snapshot-{index}", "kind": "snapshot", "path": str(tmp_path / f"{index}.jpg")}
        database.media.append(row)
        return row

    async def video_snapshot(*_args):
        raise AssertionError("missing recording must use camera snapshots")

    async def fake_analyze(*_args, **_kwargs):
        calls.append("analyze")
        return None

    monkeypatch.setattr("camdash.capture.adapter_for", lambda _camera: FakeAdapter())
    monkeypatch.setattr(manager, "_record_video", failed_record)
    monkeypatch.setattr(manager, "_snapshot", camera_snapshot)
    monkeypatch.setattr(manager, "_snapshot_from_video", video_snapshot)
    monkeypatch.setattr(manager, "_analyze", fake_analyze)

    await manager._run({
        "id": "event-1", "camera_name": "Cam", "triggered_at": "2026-08-03T12:00:00+00:00",
        "received_at": "2026-08-03T12:00:00+00:00", "profile": "day",
    }, camera, capture)

    assert calls == ["record", "snapshot-0", "snapshot-1", "analyze"]


@pytest.mark.asyncio
async def test_scan_video_interval_generates_expected_offsets(tmp_path: Path, monkeypatch):
    cfg = AppConfig(data_dir=str(tmp_path))
    calls = []

    async def fake_snapshot_from_video(_video_path, _event, _event_dir, index, offset, _started_at):
        calls.append((index, round(offset, 3)))
        return {"id": f"scan-{index}", "kind": "snapshot", "path": str(tmp_path / f"scan-{index}.jpg")}

    async def broadcast(_message):
        pass

    manager = CaptureManager(object(), lambda: cfg, broadcast)
    monkeypatch.setattr(manager, "_snapshot_from_video", fake_snapshot_from_video)

    media = await manager._scan_video_interval(
        tmp_path / "clip.mp4", {"id": "event-1"}, tmp_path, video_duration=17.0,
        video_started_at="2026-08-06T00:00:00+00:00", interval_seconds=5, start_index=2,
    )

    assert calls == [(2, 5.0), (3, 10.0), (4, 15.0)]
    assert len(media) == 3


@pytest.mark.asyncio
async def test_remove_empty_images_scans_video_and_deletes_when_still_empty(tmp_path: Path, monkeypatch):
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.analysis.enabled = True
    cfg.analysis.remove_empty_images = True
    cfg.analysis.empty_scan_interval_seconds = 5
    camera = CameraConfig(id="cam-1", name="Cam", host="192.0.2.1")

    class FakeDatabase:
        analysis = None

        def update_media(self, *args, **kwargs):
            pass

        def update_event(self, event_id, **values):
            self.analysis = values.get("analysis_json", self.analysis)

        def get_event(self, event_id):
            return {"id": event_id, "camera_name": "Cam", "triggered_at": "now", "analysis": self.analysis}

    async def broadcast(_message):
        pass

    monkeypatch.setattr("camdash.capture.analyzer.analyze_image", lambda *args: {"description": "", "detections": []})

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")
    scan_calls = []

    async def fake_snapshot_from_video(_video_path, _event, _event_dir, index, offset, _started_at):
        scan_calls.append((index, offset))
        path = tmp_path / f"scan-{index}.jpg"
        path.write_bytes(b"jpeg")
        return {"id": f"scan-{index}", "kind": "snapshot", "path": str(path), "thumb_path": None}

    manager = CaptureManager(FakeDatabase(), lambda: cfg, broadcast)
    monkeypatch.setattr(manager, "_snapshot_from_video", fake_snapshot_from_video)
    initial_snapshot_path = tmp_path / "initial.jpg"
    initial_snapshot_path.write_bytes(b"jpeg")

    remove = await manager._analyze(
        {"id": "event-1", "camera_name": "Cam", "triggered_at": "now"},
        camera,
        [{"id": "media-1", "path": str(initial_snapshot_path), "thumb_path": None}],
        video_path=video_path, video_duration=17.0,
        video_started_at="2026-08-06T00:00:00+00:00", event_dir=tmp_path,
    )

    assert remove == "empty"
    assert scan_calls == [(1, 5.0), (2, 10.0), (3, 15.0)]


@pytest.mark.asyncio
async def test_remove_empty_images_keeps_event_when_scan_finds_detection(tmp_path: Path, monkeypatch):
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.analysis.enabled = True
    cfg.analysis.remove_empty_images = True
    cfg.analysis.empty_scan_interval_seconds = 5
    camera = CameraConfig(id="cam-1", name="Cam", host="192.0.2.1")

    class FakeDatabase:
        analysis = None

        def update_media(self, *args, **kwargs):
            pass

        def update_event(self, event_id, **values):
            self.analysis = values.get("analysis_json", self.analysis)

        def get_event(self, event_id):
            return {"id": event_id, "camera_name": "Cam", "triggered_at": "now", "analysis": self.analysis}

    async def broadcast(_message):
        pass

    def fake_analyze_image(path, *_args):
        if "scan" in str(path):
            return {"description": "A raccoon", "detections": [{"label": "raccoon", "confidence": 8}]}
        return {"description": "", "detections": []}

    monkeypatch.setattr("camdash.capture.analyzer.analyze_image", fake_analyze_image)

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    async def fake_snapshot_from_video(_video_path, _event, _event_dir, index, offset, _started_at):
        path = tmp_path / f"scan-{index}.jpg"
        path.write_bytes(b"jpeg")
        return {"id": f"scan-{index}", "kind": "snapshot", "path": str(path), "thumb_path": None}

    manager = CaptureManager(FakeDatabase(), lambda: cfg, broadcast)
    monkeypatch.setattr(manager, "_snapshot_from_video", fake_snapshot_from_video)
    initial_snapshot_path = tmp_path / "initial.jpg"
    initial_snapshot_path.write_bytes(b"jpeg")

    remove = await manager._analyze(
        {"id": "event-1", "camera_name": "Cam", "triggered_at": "now"},
        camera,
        [{"id": "media-1", "path": str(initial_snapshot_path), "thumb_path": None}],
        video_path=video_path, video_duration=17.0,
        video_started_at="2026-08-06T00:00:00+00:00", event_dir=tmp_path,
    )

    assert remove is None


@pytest.mark.asyncio
async def test_remove_empty_images_disabled_skips_video_scan(tmp_path: Path, monkeypatch):
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.analysis.enabled = True
    camera = CameraConfig(id="cam-1", name="Cam", host="192.0.2.1")

    class FakeDatabase:
        def update_media(self, *args, **kwargs):
            pass

        def update_event(self, *args, **kwargs):
            pass

        def get_event(self, event_id):
            return {"id": event_id, "camera_name": "Cam", "triggered_at": "now"}

    async def broadcast(_message):
        pass

    monkeypatch.setattr("camdash.capture.analyzer.analyze_image", lambda *args: {"description": "", "detections": []})

    async def fake_snapshot_from_video(*_args):
        raise AssertionError("should not scan when remove_empty_images is disabled")

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    manager = CaptureManager(FakeDatabase(), lambda: cfg, broadcast)
    monkeypatch.setattr(manager, "_snapshot_from_video", fake_snapshot_from_video)
    initial_snapshot_path = tmp_path / "initial.jpg"
    initial_snapshot_path.write_bytes(b"jpeg")

    remove = await manager._analyze(
        {"id": "event-1", "camera_name": "Cam", "triggered_at": "now"},
        camera,
        [{"id": "media-1", "path": str(initial_snapshot_path), "thumb_path": None}],
        video_path=video_path, video_duration=17.0,
        video_started_at="2026-08-06T00:00:00+00:00", event_dir=tmp_path,
    )

    assert remove is None


@pytest.mark.asyncio
async def test_event_records_video_before_deriving_snapshots(tmp_path: Path, monkeypatch):
    cfg = AppConfig(data_dir=str(tmp_path))
    camera = CameraConfig(id="cam-1", name="Cam", host="192.0.2.1", record_stream="sub")
    capture = CaptureConfig(snapshots=2, snapshot_interval_seconds=1, day_video_seconds=30,
                            night_video_seconds=30, cooldown_seconds=0)
    calls = []

    class FakeDatabase:
        def __init__(self):
            self.media = []
            self.updates = []

        def update_event(self, event_id, **values):
            self.updates.append((event_id, values))

        def get_event(self, event_id):
            return {"id": event_id, "media": self.media}

    class FakeAdapter:
        def rtsp_url(self, main):
            assert main is False
            return "rtsp://camera/ch1"

        def fetch_snapshot(self, main):
            raise AssertionError("event capture must not call the camera snapshot endpoint")

    async def broadcast(_message):
        pass

    database = FakeDatabase()
    manager = CaptureManager(database, lambda: cfg, broadcast)

    async def fake_record(*_args):
        calls.append("video")
        path = tmp_path / "media" / "2026" / "08" / "03" / "event-1" / "clip.mp4"
        path.write_bytes(b"video")
        row = {"id": "video-1", "kind": "video", "captured_at": "2026-08-03T12:00:00+00:00",
               "duration_seconds": 30.0, "path": str(path)}
        database.media.append(row)
        return row

    async def fake_snapshot(_video_path, _event, _event_dir, index, offset, _started_at):
        calls.append(f"snapshot-{index}@{offset}")
        row = {"id": f"snapshot-{index}", "kind": "snapshot", "path": str(tmp_path / f"{index}.jpg")}
        database.media.append(row)
        return row

    async def fake_analyze(_event, _camera, _snapshots, **_kwargs):
        calls.append("analyze")
        return None

    monkeypatch.setattr("camdash.capture.adapter_for", lambda _camera: FakeAdapter())
    monkeypatch.setattr(manager, "_record_video", fake_record)
    monkeypatch.setattr(manager, "_snapshot_from_video", fake_snapshot)
    monkeypatch.setattr(manager, "_analyze", fake_analyze)

    await manager._run({
        "id": "event-1", "camera_name": "Cam", "triggered_at": "2026-08-03T12:00:00+00:00",
        "received_at": "2026-08-03T12:00:00+00:00", "profile": "day",
    }, camera, capture)

    assert calls == ["video", "snapshot-0@0", "snapshot-1@1", "analyze"]
    assert any(values.get("primary_media_id") == "snapshot-0" for _, values in database.updates)
