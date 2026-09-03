"""Groq-backed compatibility layer for the subset of google.genai used by MyTourBazar.

Why this exists:
The existing project has several modules that import:
    from google import genai
    from google.genai import types
and call:
    genai.Client(...).models.generate_content(...)

Installing this shim before those project modules are imported keeps their existing
function signatures/workflow unchanged while routing generation to Groq.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import time
import types as _pytypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "qwen/qwen3.6-27b"
DEFAULT_AUDIO_MODEL = "whisper-large-v3-turbo"
MAX_IMAGES = 2
MAX_TEXT_CHARS = 14000


@dataclass
class Part:
    data: bytes | None = None
    mime_type: str | None = None
    text: str | None = None

    @classmethod
    def from_bytes(cls, data: bytes, mime_type: str):
        return cls(data=bytes(data), mime_type=str(mime_type or "application/octet-stream"))

    @classmethod
    def from_text(cls, text: str):
        return cls(text=str(text or ""), mime_type="text/plain")


class GenerateContentConfig:
    def __init__(
        self,
        response_mime_type: str | None = None,
        response_schema: Any = None,
        temperature: float | None = None,
        system_instruction: Any = None,
        max_output_tokens: int | None = None,
        **kwargs,
    ):
        self.response_mime_type = response_mime_type
        self.response_schema = response_schema
        self.temperature = temperature
        self.system_instruction = system_instruction
        self.max_output_tokens = max_output_tokens
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Response:
    def __init__(self, text: str = "", raw: Any = None):
        self.text = text or ""
        self.raw = raw


class _Models:
    def __init__(self, api_key: str):
        self.api_key = str(api_key or "").strip()

    def generate_content(self, model: str, contents: Any, config: GenerateContentConfig | None = None, **kwargs):
        return _generate_content(self.api_key, model, contents, config or GenerateContentConfig(), **kwargs)


class Client:
    def __init__(self, api_key: str | None = None, **kwargs):
        self.api_key = str(api_key or os.getenv("GROQ_API_KEY", "")).strip()
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is missing. Add GROQ_API_KEY in Northflank environment variables.")
        self.models = _Models(self.api_key)


# ---------------------------------------------------------------------------
# Source preparation
# ---------------------------------------------------------------------------

def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _part_text(part: Part) -> str:
    if part.text is not None:
        return str(part.text)
    if not part.data:
        return ""
    mime = (part.mime_type or "").lower()
    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        try:
            return part.data.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return ""


def _image_data_url(raw: bytes, mime: str) -> str | None:
    """Normalize image bytes so base64 requests stay comfortably below Groq limits."""
    if not raw:
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            # Flatten transparency over white, then resize only when needed.
            if im.mode not in ("RGB", "L"):
                bg = Image.new("RGB", im.size, "white")
                if "A" in im.getbands():
                    bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").getchannel("A"))
                    im = bg
                else:
                    im = im.convert("RGB")
            else:
                im = im.convert("RGB")

            max_side = 1900
            if max(im.size) > max_side:
                ratio = max_side / max(im.size)
                im = im.resize(
                    (max(1, int(im.width * ratio)), max(1, int(im.height * ratio))),
                    Image.Resampling.LANCZOS,
                )

            # Re-encode as JPEG; lower quality progressively if necessary.
            for quality in (84, 76, 68, 58):
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=quality, optimize=True)
                payload = buf.getvalue()
                # Keep raw image below ~2.7MB; base64 then remains below ~3.7MB.
                if len(payload) <= 2_700_000 or quality == 58:
                    encoded = base64.b64encode(payload).decode("ascii")
                    return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        # If PIL cannot read it and the original is already small, pass it through.
        if len(raw) <= 2_700_000 and mime.startswith("image/"):
            encoded = base64.b64encode(raw).decode("ascii")
            return f"data:{mime};base64,{encoded}"
    return None


def _pdf_payload(raw: bytes) -> tuple[str, list[str]]:
    """Extract all selectable text and render up to five useful pages for vision.

    The MyTourBazar flight engine already performs its own PyMuPDF geometry checks;
    these rendered images provide the multimodal model with visual layout evidence.
    """
    if not raw:
        return "", []
    text_chunks: list[str] = []
    images: list[str] = []
    try:
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        page_scores = []
        keywords = (
            "flight", "departure", "arrival", "airport", "terminal", "pnr",
            "passenger", "ticket", "baggage", "fare", "total", "hotel",
            "check-in", "check out", "itinerary", "booking", "guest", "room",
            "bus", "boarding", "dropping", "journey", "tour", "package",
        )
        for i, page in enumerate(doc):
            txt = page.get_text("text") or ""
            if txt.strip():
                text_chunks.append(f"\n--- PDF PAGE {i+1} ---\n{txt}")
            low = txt.lower()
            score = sum(1 for k in keywords if k in low)
            # Text-empty pages are often scanned documents and deserve vision priority.
            if not txt.strip():
                score += 8
            page_scores.append((score, i))

        # Preserve page order among selected relevant pages.
        if len(doc) <= MAX_IMAGES:
            selected = list(range(len(doc)))
        else:
            selected = sorted(i for _, i in sorted(page_scores, reverse=True)[:MAX_IMAGES])

        for i in selected:
            page = doc[i]
            # 1.6x is enough for ticket text while keeping payload compact.
            pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
            png = pix.tobytes("png")
            url = _image_data_url(png, "image/png")
            if url:
                images.append(url)
        doc.close()
    except Exception:
        return "", []

    text = "".join(text_chunks)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    return text, images[:MAX_IMAGES]


def _transcribe_audio(api_key: str, raw: bytes, mime: str) -> str:
    ext = {
        "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
        "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/webm": ".webm",
    }.get((mime or "").lower(), ".ogg")
    filename = "voice" + ext
    headers = {"Authorization": f"Bearer {api_key}"}
    files = {"file": (filename, raw, mime or "application/octet-stream")}
    data = {"model": os.getenv("GROQ_AUDIO_MODEL", DEFAULT_AUDIO_MODEL), "response_format": "json"}
    last_error = None
    for attempt in range(4):
        try:
            with httpx.Client(timeout=120.0) as client:
                r = client.post(f"{GROQ_BASE_URL}/audio/transcriptions", headers=headers, files=files, data=data)
            if r.status_code == 429 or r.status_code >= 500:
                last_error = RuntimeError(f"Groq audio API {r.status_code}: {r.text[:500]}")
                retry_after = float(r.headers.get("retry-after") or (2 ** attempt))
                time.sleep(min(max(retry_after, 1), 65))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Groq audio API {r.status_code}: {r.text[:1000]}")
            payload = r.json()
            return str(payload.get("text") or "").strip()
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Groq voice transcription failed: {last_error}")


def _prepare_contents(api_key: str, contents: Any) -> tuple[str, list[str]]:
    if contents is None:
        items = []
    elif isinstance(contents, (list, tuple)):
        items = list(contents)
    else:
        items = [contents]

    text_chunks: list[str] = []
    image_urls: list[str] = []
    audio_transcripts: list[str] = []

    for item in items:
        if isinstance(item, str):
            text_chunks.append(item)
            continue
        if isinstance(item, Part):
            mime = (item.mime_type or "").lower()
            if item.text is not None or mime.startswith("text/"):
                t = _part_text(item)
                if t:
                    text_chunks.append(t)
            elif mime == "application/pdf":
                pdf_text, pdf_images = _pdf_payload(item.data or b"")
                if pdf_text:
                    text_chunks.append("\nSELECTABLE PDF TEXT:\n" + pdf_text)
                for url in pdf_images:
                    if len(image_urls) < MAX_IMAGES:
                        image_urls.append(url)
            elif mime.startswith("image/"):
                if len(image_urls) < MAX_IMAGES:
                    url = _image_data_url(item.data or b"", mime)
                    if url:
                        image_urls.append(url)
            elif mime.startswith("audio/"):
                transcript = _transcribe_audio(api_key, item.data or b"", mime)
                if transcript:
                    audio_transcripts.append(transcript)
            else:
                t = _part_text(item)
                if t:
                    text_chunks.append(t)
            continue

        # Accept simple dict-like content when a future module passes one.
        if isinstance(item, dict):
            if "text" in item:
                text_chunks.append(str(item.get("text") or ""))
            else:
                text_chunks.append(json.dumps(item, ensure_ascii=False))
        else:
            text_chunks.append(str(item))

    if audio_transcripts:
        text_chunks.append("\nAUDIO TRANSCRIPT FROM GROQ WHISPER:\n" + "\n".join(audio_transcripts))

    combined = "\n\n".join(x for x in text_chunks if str(x).strip())
    if len(combined) > MAX_TEXT_CHARS:
        combined = combined[:MAX_TEXT_CHARS]

    # Groq Free has a much smaller tokens-per-minute budget than the model context
    # window. Keep vision requests lean: when the prompt/source text is already large,
    # send one best visual page rather than exhausting the free TPM allowance.
    image_cap = 1 if len(combined) > 8500 else MAX_IMAGES
    return combined, image_urls[:image_cap]


# ---------------------------------------------------------------------------
# Groq request
# ---------------------------------------------------------------------------

def _schema_prompt(schema: Any) -> str:
    if not schema:
        return ""
    try:
        s = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        s = str(schema)
    return (
        "\n\nSTRICT JSON OUTPUT REQUIREMENT:\n"
        "Return one valid JSON object only. Do not use markdown fences. "
        "Match this schema exactly; include every required field and use empty strings, "
        "empty arrays, or numeric zero when the source does not support a value.\n"
        f"JSON SCHEMA:\n{s}"
    )


def _clean_json_text(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _generate_content(api_key: str, model: str, contents: Any, config: GenerateContentConfig, **kwargs) -> _Response:
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is missing. Add GROQ_API_KEY in Northflank.")

    requested_model = str(model or "").strip()
    # Old code may still pass a Gemini model name. Never send that to Groq.
    if not requested_model or "gemini" in requested_model.lower():
        requested_model = os.getenv("GROQ_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    text, image_urls = _prepare_contents(api_key, contents)
    if config.response_mime_type == "application/json":
        text += _schema_prompt(config.response_schema)

    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    else:
        content.append({"type": "text", "text": "Read the supplied source and respond to the task."})
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    messages = []
    system_instruction = getattr(config, "system_instruction", None)
    if system_instruction:
        messages.append({"role": "system", "content": str(system_instruction)})
    messages.append({"role": "user", "content": content})

    payload: dict[str, Any] = {
        "model": requested_model,
        "messages": messages,
        "temperature": 0 if config.temperature is None else config.temperature,
        "stream": False,
        "max_completion_tokens": int(config.max_output_tokens or 12000),
    }

    # Qwen 3.6 officially supports JSON Object Mode with image inputs.
    # Qwen 3.8 additionally supports strict Structured Outputs; use it when selected.
    if config.response_mime_type == "application/json":
        if config.response_schema and "qwen3.8" in requested_model.lower():
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "mytourbazar_output",
                    "strict": False,
                    "schema": config.response_schema,
                },
            }
        else:
            payload["response_format"] = {"type": "json_object"}

    # Non-thinking mode is ideal for precise extraction and keeps free-tier token use low.
    if requested_model.startswith("qwen/"):
        payload["reasoning_effort"] = "none"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: Exception | None = None
    for attempt in range(5):
        try:
            with httpx.Client(timeout=180.0) as client:
                r = client.post(f"{GROQ_BASE_URL}/chat/completions", headers=headers, json=payload)

            if r.status_code == 429 or r.status_code >= 500:
                last_error = RuntimeError(f"Groq API {r.status_code}: {r.text[:700]}")
                retry_after = r.headers.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after else float(2 ** attempt)
                except Exception:
                    delay = float(2 ** attempt)
                time.sleep(min(max(delay, 1), 65))
                continue

            if r.status_code >= 400:
                raise RuntimeError(f"Groq API {r.status_code}: {r.text[:1400]}")

            raw = r.json()
            choices = raw.get("choices") or []
            if not choices:
                raise RuntimeError(f"Groq returned no completion choices: {str(raw)[:800]}")
            message = choices[0].get("message") or {}
            output = message.get("content") or ""
            if isinstance(output, list):
                output = "".join(str(x.get("text") or "") if isinstance(x, dict) else str(x) for x in output)
            output = _clean_json_text(str(output))
            if not output:
                raise RuntimeError("Groq returned an empty response.")
            return _Response(output, raw=raw)
        except Exception as exc:
            last_error = exc
            if attempt < 4 and not (isinstance(exc, RuntimeError) and "Groq API 4" in str(exc) and "429" not in str(exc)):
                time.sleep(min(2 ** attempt, 12))
                continue
            raise

    raise RuntimeError(f"Groq request failed after retries: {last_error}")


# ---------------------------------------------------------------------------
# Runtime module installation
# ---------------------------------------------------------------------------

def install_google_genai_shim():
    """Install `google.genai` and `google.genai.types` aliases in sys.modules.

    Must be called by bot.py before importing extractor/editor/bus/hotel/flight modules.
    """
    google_mod = sys.modules.get("google")
    if google_mod is None:
        google_mod = _pytypes.ModuleType("google")
        google_mod.__path__ = []
        sys.modules["google"] = google_mod

    genai_mod = _pytypes.ModuleType("google.genai")
    types_mod = _pytypes.ModuleType("google.genai.types")

    types_mod.Part = Part
    types_mod.GenerateContentConfig = GenerateContentConfig

    genai_mod.Client = Client
    genai_mod.types = types_mod

    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = types_mod
    setattr(google_mod, "genai", genai_mod)
    return genai_mod


__all__ = [
    "Client", "Part", "GenerateContentConfig", "install_google_genai_shim",
]
