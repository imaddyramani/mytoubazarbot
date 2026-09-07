"""Small, fail-safe OpenAI-compatible client for xKiro and GROQ.

The document workflows are deliberately local-first.  This module is only a
remote enhancement/recovery layer: callers must keep their local result when
``complete_json`` returns ``None``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import base64
from io import BytesIO
from copy import deepcopy
from pathlib import Path

import httpx

LOGGER = logging.getLogger("mytourbazar.ai_provider")

XKIRO_BASE_URL = "https://api.xkiro.com/v1"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_MODEL_CACHE = {}
_MODEL_LOCK = threading.Lock()

_TEXT_MODEL_PREFERENCE = (
    "qwen/qwen3.5-plus:free",
    "qwen/qwen3.5-flash:free",
    "deepseek/deepseek-v4-flash",
    "minimax/minimax-m2.7:free",
    "openai/gpt-5.3-codex-spark",
)
_VISION_MODEL_PREFERENCE = (
    "qwen/qwen3.5-plus:free",
    "qwen/qwen3.5-omni-plus:free",
    "qwen/qwen3-vl-plus:free",
    "minimax/minimax-m3:free",
)


class AIProviderError(RuntimeError):
    """A recoverable remote-provider failure."""


def enabled():
    provider = os.getenv("AI_PROVIDER", "local").strip().lower()
    if provider in {"", "local", "off", "disabled", "none"}:
        return False
    return bool(os.getenv("XKIRO_API_KEY", "").strip() or os.getenv("GROQ_API_KEY", "").strip())


def configuration_summary():
    """Safe startup summary containing no credential values."""
    provider=os.getenv("AI_PROVIDER","local").strip().lower() or "local"
    if provider in {"local","off","disabled","none"}:
        return "AI provider: local-only"
    text_model=os.getenv("XKIRO_TEXT_MODEL","").strip() or "qwen/qwen3.5-plus:free (auto-preferred)"
    vision_model=os.getenv("XKIRO_VISION_MODEL","").strip() or "qwen/qwen3.5-plus:free (auto-preferred)"
    fallback=os.getenv("AI_FALLBACK_PROVIDER","none").strip().lower() or "none"
    key_state="configured" if os.getenv("XKIRO_API_KEY","").strip() else "missing key"
    return f"AI provider: {provider} ({key_state}); text={text_model}; vision={vision_model}; fallback={fallback}"


def _timeout():
    try:
        return max(10.0, min(float(os.getenv("AI_TIMEOUT_SECONDS", "80")), 90.0))
    except ValueError:
        return 80.0


def _max_input_chars():
    try:
        return max(4000, min(int(os.getenv("AI_MAX_INPUT_CHARS", "60000")), 120000))
    except ValueError:
        return 60000


def _provider_order():
    selected = os.getenv("AI_PROVIDER", "local").strip().lower()
    fallback = os.getenv("AI_FALLBACK_PROVIDER", "none").strip().lower()
    if selected in {"auto", "xkiro"}:
        order = ["xkiro"]
        if fallback == "groq":
            order.append("groq")
        return order
    if selected == "groq":
        return ["groq"]
    return []


def _discover_xkiro_model(vision=False):
    configured = os.getenv("XKIRO_VISION_MODEL" if vision else "XKIRO_TEXT_MODEL", "").strip()
    if configured:
        return configured
    cache_key = "vision" if vision else "text"
    with _MODEL_LOCK:
        if _MODEL_CACHE.get(cache_key):
            return _MODEL_CACHE[cache_key]
        with httpx.Client(timeout=min(_timeout(), 20.0)) as client:
            response = client.get(f"{XKIRO_BASE_URL}/models")
            response.raise_for_status()
            catalog = response.json().get("data") or []
        choices = []
        for item in catalog:
            capabilities = item.get("capabilities") or {}
            if item.get("access_tier") != "free":
                continue
            if vision and not capabilities.get("vision"):
                continue
            choices.append(str(item.get("id") or ""))
        preferred = _VISION_MODEL_PREFERENCE if vision else _TEXT_MODEL_PREFERENCE
        model = next((item for item in preferred if item in choices), "")
        if not model and choices:
            model = choices[0]
        if not model:
            raise AIProviderError("xKiro currently exposes no suitable free model")
        _MODEL_CACHE[cache_key] = model
        LOGGER.info("Selected xKiro %s model: %s", cache_key, model)
        return model


def _provider_config(provider, vision=False):
    if provider == "xkiro":
        key = os.getenv("XKIRO_API_KEY", "").strip()
        if not key:
            raise AIProviderError("XKIRO_API_KEY is not configured")
        return XKIRO_BASE_URL, key, _discover_xkiro_model(vision)
    if provider == "groq":
        key = os.getenv("GROQ_API_KEY", "").strip()
        model = os.getenv("GROQ_VISION_MODEL" if vision else "GROQ_MODEL", "").strip()
        if not key or not model:
            raise AIProviderError("GROQ_API_KEY/GROQ_MODEL is not configured")
        return GROQ_BASE_URL, key, model
    raise AIProviderError(f"Unsupported provider: {provider}")


def _vision_parts(image_paths):
    """Create bounded JPEG data URLs; render only a few PDF pages at low memory."""
    try:
        limit=max(1,min(int(os.getenv("AI_MAX_VISION_PAGES","3")),6))
    except ValueError:
        limit=3
    parts=[]
    for raw_path in image_paths or []:
        path=Path(raw_path)
        if not path.is_file() or len(parts)>=limit:
            continue
        images=[]
        try:
            if path.suffix.lower()=='.pdf':
                import fitz
                from PIL import Image
                with fitz.open(str(path)) as document:
                    for page in document:
                        if len(parts)+len(images)>=limit: break
                        zoom=min(1.6,1600/max(page.rect.width,page.rect.height))
                        pix=page.get_pixmap(matrix=fitz.Matrix(zoom,zoom),colorspace=fitz.csRGB,alpha=False)
                        image=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
                        del pix
                        images.append(image)
            else:
                from PIL import Image, ImageOps
                with Image.open(path) as original:
                    images.append(ImageOps.exif_transpose(original).convert('RGB').copy())
            for image in images:
                try:
                    image.thumbnail((1600,1600))
                    buffer=BytesIO()
                    image.save(buffer,format='JPEG',quality=82,optimize=True)
                    encoded=base64.b64encode(buffer.getvalue()).decode('ascii')
                    parts.append({"type":"image_url","image_url":{"url":"data:image/jpeg;base64,"+encoded}})
                finally:
                    image.close()
                if len(parts)>=limit: break
        except Exception as exc:
            LOGGER.warning("Could not prepare one supplier attachment for vision: %s", type(exc).__name__)
    return parts


def _json_from_content(content):
    if isinstance(content, list):
        content = "".join(str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content)
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise AIProviderError("model did not return a JSON object")
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise AIProviderError("model returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise AIProviderError("model JSON response was not an object")
    return value


def _validate(value, schema):
    if not isinstance(value, dict):
        raise AIProviderError("response is not an object")
    missing = [key for key in (schema or {}).get("required", []) if key not in value]
    if missing:
        raise AIProviderError("response is missing required fields: " + ", ".join(missing[:8]))
    for key, rule in ((schema or {}).get("properties") or {}).items():
        if key not in value:
            continue
        expected = rule.get("type")
        if expected == "array" and not isinstance(value[key], list):
            raise AIProviderError(f"{key} must be an array")
        if expected == "object" and not isinstance(value[key], dict):
            raise AIProviderError(f"{key} must be an object")
        if expected == "string" and not isinstance(value[key], str):
            value[key] = "" if value[key] is None else str(value[key])
        if expected == "integer":
            try:
                value[key] = int(value[key] or 0)
            except (TypeError, ValueError) as exc:
                raise AIProviderError(f"{key} must be an integer") from exc
        if expected == "number":
            try:
                value[key] = float(value[key] or 0)
            except (TypeError, ValueError) as exc:
                raise AIProviderError(f"{key} must be a number") from exc
    return value


def _request(provider, system_prompt, user_text, schema, max_tokens, vision=False, correction="", image_paths=None):
    base_url, key, model = _provider_config(provider, vision)
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    user_content=str(user_text or "")[:_max_input_chars()]
    if vision:
        visual=_vision_parts(image_paths)
        if visual:
            user_content=visual+[{"type":"text","text":user_content}]
    messages = [
        {"role": "system", "content": system_prompt.strip() + "\nReturn JSON matching this schema exactly:\n" + schema_text},
        {"role": "user", "content": user_content},
    ]
    if correction:
        messages.append({"role": "system", "content": "Your previous answer failed validation: " + correction + ". Return corrected JSON only."})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max(512, min(int(max_tokens), 12000)),
        "response_format": {"type": "json_object"},
    }
    with httpx.Client(timeout=_timeout()) as client:
        response = client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
    if response.status_code >= 400:
        raise AIProviderError(f"{provider} returned HTTP {response.status_code}")
    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        raise AIProviderError(f"{provider} returned no completion")
    content = (choices[0].get("message") or {}).get("content")
    return _validate(_json_from_content(content), schema), model


def complete_json(system_prompt, user_text, schema, *, purpose="extraction", max_tokens=5000, image_paths=None):
    """Return validated JSON, or ``None`` so the caller can keep local output."""
    if not enabled():
        return None
    errors = []
    vision=bool(image_paths)
    for provider in _provider_order():
        correction = ""
        for attempt in range(2):
            try:
                value, model = _request(
                    provider, system_prompt, user_text, deepcopy(schema), max_tokens,
                    vision=vision, correction=correction, image_paths=image_paths,
                )
                LOGGER.info("AI %s completed via %s/%s", purpose, provider, model)
                return value
            except (AIProviderError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                correction = str(exc)[:300]
                errors.append(f"{provider}: {correction}")
                LOGGER.warning("AI %s attempt %s failed via %s: %s", purpose, attempt + 1, provider, correction)
                if attempt == 0 and "validation" not in correction and "JSON" not in correction and "required" not in correction:
                    break
    LOGGER.warning("AI %s unavailable; local result retained (%s)", purpose, "; ".join(errors[-4:]))
    return None
