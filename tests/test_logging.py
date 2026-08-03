import logging
from pathlib import Path

from camdash.main import RedactingUtcFormatter, _read_persisted_logs


def test_persisted_logs_load_rotations_in_order_and_redact(tmp_path: Path):
    log_path = tmp_path / "camdash.log"
    rotated = Path(f"{log_path}.1")
    rotated.write_text(
        "2026-08-03 03:25:29,267 WARNING camdash.analyzer old password=secret\n"
        "trace token=also-secret\n",
        encoding="utf-8",
    )
    log_path.write_text(
        "2026-08-03T03:26:00Z INFO camdash.capture Analysis: done detections=cat/Cupcake\n",
        encoding="utf-8",
    )

    entries = _read_persisted_logs(log_path)

    assert [entry["logger"] for entry in entries] == ["camdash.analyzer", "camdash.capture"]
    assert entries[0]["time"] == "2026-08-03T03:25:29.267000+00:00"
    assert "secret" not in entries[0]["message"]
    assert "password=<redacted>" in entries[0]["message"]
    assert "token=<redacted>" in entries[0]["message"]
    assert entries[1]["message"].endswith("cat/Cupcake")


def test_file_formatter_uses_utc_and_redacts_urls():
    formatter = RedactingUtcFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"
    )
    record = logging.LogRecord(
        "camdash.test", logging.INFO, __file__, 1,
        "stream=rtsp://thingino:secret@camera/ch0 snapshot=http://camera/x.jpg?token=abc", (), None,
    )
    record.created = 1785727560

    output = formatter.format(record)

    assert output.startswith("2026-")
    assert "secret" not in output
    assert "token=<redacted>" in output
    assert "rtsp://<redacted>@camera/ch0" in output
