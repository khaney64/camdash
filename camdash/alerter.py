from __future__ import annotations

import logging
import os
import smtplib
import time
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape
from pathlib import Path
from typing import Any

import yaml


LOG = logging.getLogger(__name__)


class AlertEngine:
    def __init__(self, rules_path: Path):
        self.rules_path = rules_path
        self.last_fired: dict[str, float] = {}

    def rules(self) -> list[dict[str, Any]]:
        try:
            value = yaml.safe_load(self.rules_path.read_text(encoding="utf-8")) or []
            return value.get("rules", value.get("alerts", [])) if isinstance(value, dict) else value
        except FileNotFoundError:
            return []
        except Exception:
            LOG.exception("failed to load alert rules")
            return []

    def evaluate(
        self,
        event: dict[str, Any],
        thumb: Path | None,
        cooldown_seconds: int,
        enabled_rules: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        analysis = event.get("analysis") or {}
        detections = {str(d.get("label", "")).lower() for d in analysis.get("detections", [])}
        description = str(analysis.get("description", "")).lower()
        triggered, errors = [], []
        matching, catch_all = [], []
        enabled = {str(name).lower(): bool(value) for name, value in (enabled_rules or {}).items()}
        for rule in self.rules():
            name = str(rule.get("name", "unnamed"))
            if enabled.get(name.lower(), True) is False:
                continue
            if rule.get("catch_all"):
                catch_all.append(rule)
                continue
            keywords = {str(k).lower() for k in rule.get("keywords", [])}
            if keywords.intersection(detections) or any(k in description for k in keywords):
                matching.append(rule)
        now = time.time()
        eligible = []
        for rule in matching or catch_all:
            name = str(rule.get("name", "unnamed"))
            if now - self.last_fired.get(name, 0) < cooldown_seconds:
                continue
            eligible.append(rule)

        email_rules = [rule for rule in eligible if rule.get("action", "log") == "email"]
        if email_rules:
            try:
                names = [str(rule.get("name", "unnamed")) for rule in email_rules]
                self._email(event, names, thumb)
                for name in names:
                    self.last_fired[name] = now
                    triggered.append(name)
            except Exception as exc:
                errors.append(f"{', '.join(names)}: {exc}")
                LOG.exception("alert failed")

        for rule in eligible:
            if rule.get("action", "log") == "email":
                continue
            name = str(rule.get("name", "unnamed"))
            LOG.warning("alert [%s] camera=%s event=%s", name, event["camera_name"], event["id"])
            self.last_fired[name] = now
            triggered.append(name)
        return {"triggered": triggered, "errors": errors}

    def test_email(self) -> None:
        self._send("CAM Dashboard test", "Email alerts are configured correctly.", None)

    def _email(self, event: dict[str, Any], rules: list[str], thumb: Path | None) -> None:
        detections = ", ".join(d.get("label", "") for d in (event.get("analysis") or {}).get("detections", []))
        rule_text = ", ".join(rules)
        self._send(
            f"CAM Dashboard: {detections or rule_text} at {event['camera_name']}",
            f"Rules: {rule_text}\nCamera: {event['camera_name']}\nTime: {event['triggered_at']}\n\n{(event.get('analysis') or {}).get('description', '')}",
            thumb,
        )

    @staticmethod
    def _send(subject: str, body: str, thumb: Path | None) -> None:
        recipient = os.environ.get("CAMDASH_ALERT_EMAIL", "")
        password = os.environ.get("CAMDASH_ALERT_SMTP_PASSWORD", "")
        sender = os.environ.get("CAMDASH_ALERT_FROM_EMAIL", recipient)
        if not recipient or not password:
            raise RuntimeError("email recipient or SMTP password is not configured")
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = sender, recipient, subject
        message.set_content(body)
        if thumb and thumb.exists():
            cid = make_msgid(domain="camdash.local")
            message.add_alternative(
                f"<html><body><pre>{escape(body)}</pre><img src=\"cid:{cid[1:-1]}\" alt=\"Captured image\"></body></html>",
                subtype="html",
            )
            message.get_payload()[-1].add_related(
                thumb.read_bytes(), maintype="image", subtype="jpeg", cid=cid, filename="capture.jpg"
            )
        host = os.environ.get("CAMDASH_ALERT_SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("CAMDASH_ALERT_SMTP_PORT", "587"))
        username = os.environ.get("CAMDASH_ALERT_SMTP_USER", sender)
        implicit_ssl = os.environ.get("CAMDASH_ALERT_SMTP_SSL", "").lower() in {"1", "true", "yes"} or port == 465
        smtp_class = smtplib.SMTP_SSL if implicit_ssl else smtplib.SMTP
        with smtp_class(host, port, timeout=15) as client:
            if not implicit_ssl:
                client.starttls()
            client.login(username, password)
            client.send_message(message)
