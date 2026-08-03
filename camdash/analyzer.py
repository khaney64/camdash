from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import requests

from .config import AnalysisConfig


LOG = logging.getLogger(__name__)


def with_reasoning(prompt: str) -> str:
    return prompt.rstrip() + "\nInclude your reasoning."


def local_chat_url(base_url: str) -> str:
    value = base_url.rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1"):
        return value + "/chat/completions"
    return value + "/v1/chat/completions"


def analyze_image(path: Path, cfg: AnalysisConfig, prompt: str | None = None) -> dict[str, Any]:
    if not cfg.enabled:
        return {"description": "", "detections": [], "engine": "disabled"}
    effective_prompt = prompt or cfg.prompt
    encoded = base64.b64encode(path.read_bytes()).decode()
    try:
        result = _call(encoded, cfg, effective_prompt)
        if not result.get("description") and cfg.thinking_budget > 0:
            budget = cfg.thinking_budget * 2
            LOG.warning("analysis empty response; retrying with thinking_budget=%d", budget)
            result = _call(encoded, replace(cfg, thinking_budget=budget), effective_prompt)
            if result.get("description"):
                LOG.info("analysis retry succeeded thinking_budget=%d", budget)
            else:
                LOG.warning("analysis retry also returned empty thinking_budget=%d", budget)
        detections = result.get("detections") or []
        if (
            not result.get("error") and detections
            and all(float(item.get("confidence", 0)) <= 3 for item in detections)
            and cfg.thinking_budget > 0
        ):
            budget = cfg.thinking_budget * 2
            LOG.warning("analysis low-confidence response; retrying with thinking_budget=%d", budget)
            result = _call(encoded, replace(cfg, thinking_budget=budget), effective_prompt)
            retry_confidences = [float(item.get("confidence", 0)) for item in result.get("detections") or []]
            if any(value > 3 for value in retry_confidences):
                LOG.info("analysis confidence retry succeeded thinking_budget=%d", budget)
            else:
                LOG.warning("analysis confidence retry still low thinking_budget=%d", budget)
        return result
    except Exception as exc:
        LOG.exception("analysis failed")
        return {"description": "", "detections": [], "error": str(exc), "engine": cfg.backend}


def _call(image: str, cfg: AnalysisConfig, prompt: str) -> dict[str, Any]:
    if cfg.backend == "anthropic":
        return _anthropic(image, cfg, prompt)
    return _local(image, cfg, prompt)


def _local(image: str, cfg: AnalysisConfig, prompt: str) -> dict[str, Any]:
    if not cfg.llm_url or not cfg.llm_model:
        raise RuntimeError("local LLM URL or model is not configured")
    payload: dict[str, Any] = {
        "model": cfg.llm_model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}},
        ]}],
        "max_tokens": cfg.max_tokens + max(0, cfg.thinking_budget),
        "temperature": cfg.temperature,
    }
    if cfg.thinking_budget:
        payload["chat_template_kwargs"] = {"enable_thinking": True, "thinking_budget": cfg.thinking_budget}
    response = requests.post(local_chat_url(cfg.llm_url), json=payload, timeout=180)
    response.raise_for_status()
    text = response.json()["choices"][0]["message"]["content"]
    return {**parse_result(text), "engine": f"Local ({cfg.llm_model})"}


def _anthropic(image: str, cfg: AnalysisConfig, prompt: str) -> dict[str, Any]:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")
    payload: dict[str, Any] = {
        "model": cfg.anthropic_model,
        "max_tokens": cfg.max_tokens + max(0, cfg.thinking_budget),
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}},
            {"type": "text", "text": prompt},
        ]}],
    }
    if cfg.thinking_budget:
        payload["thinking"] = {"type": "enabled", "budget_tokens": cfg.thinking_budget}
    response = requests.post("https://api.anthropic.com/v1/messages", json=payload, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    }, timeout=180)
    response.raise_for_status()
    text = "\n".join(item.get("text", "") for item in response.json().get("content", []) if item.get("type") == "text")
    return {**parse_result(text), "engine": f"Anthropic ({cfg.anthropic_model})"}


def parse_result(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    try:
        parsed = json.loads(cleaned)
        detections = parsed.get("detections", [])
        if not isinstance(detections, list):
            detections = []
        normalized = []
        for item in detections:
            if isinstance(item, str):
                normalized.append({"label": item.lower(), "confidence": 5})
            elif isinstance(item, dict) and item.get("label"):
                detection = {"label": str(item["label"]).lower(), "confidence": float(item.get("confidence", 5))}
                if item.get("name"):
                    detection["name"] = str(item["name"])
                if item.get("reasoning"):
                    detection["reasoning"] = str(item["reasoning"])
                normalized.append(detection)
        return {"description": str(parsed.get("description", "")), "detections": normalized, "raw": text}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"description": text.strip(), "detections": [], "raw": text}


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    best: dict[str, dict[str, Any]] = {}
    descriptions = []
    engines = []
    errors = []
    for result in results:
        if result.get("description") and result["description"] not in descriptions:
            descriptions.append(result["description"])
        if result.get("engine") and result["engine"] not in engines:
            engines.append(result["engine"])
        if result.get("error"):
            errors.append(result["error"])
        for detection in result.get("detections", []):
            label = str(detection.get("label", "")).lower()
            if not label:
                continue
            current = best.get(label)
            if current is None:
                best[label] = dict(detection)
                continue
            if float(detection.get("confidence", 0)) > float(current.get("confidence", -1)):
                replacement = dict(detection)
                for field in ("name", "reasoning"):
                    if not replacement.get(field) and current.get(field):
                        replacement[field] = current[field]
                best[label] = replacement
            else:
                for field in ("name", "reasoning"):
                    if not current.get(field) and detection.get(field):
                        current[field] = detection[field]
    return {
        "description": " ".join(descriptions), "detections": list(best.values()),
        "engines": engines, "errors": errors, "images_analyzed": len(results),
    }
