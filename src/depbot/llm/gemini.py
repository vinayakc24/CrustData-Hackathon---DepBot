"""Google Gemini Developer API provider using bounded stdlib HTTPS requests."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from depbot.llm.base import LLMClient, LLMError

T = TypeVar("T", bound=BaseModel)

API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
API_KEY_ENV_VAR = "GEMINI_API_KEY"
MODEL_ENV_VAR = "DEPBOT_LLM_MODEL"
DEFAULT_MODEL = "gemini-flash-latest"  # alias; gemini-2.5-flash is closed to new API users
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_RESPONSE_BYTES = 2_000_000
_READ_CHUNK_BYTES = 64 * 1024

Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


class _ResponseTooLarge(Exception):
    pass


def _read_bounded(stream, max_bytes: int) -> bytes:
    """Read at most max_bytes plus one detection byte from an HTTP stream."""
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        chunk = stream.read(min(_READ_CHUNK_BYTES, max_bytes - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise _ResponseTooLarge
    return b"".join(chunks)


def _urllib_transport(
    url: str, headers: dict[str, str], body: bytes, timeout: float, max_response_bytes: int
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _read_bounded(response, max_response_bytes)
    except urllib.error.HTTPError as exc:
        # Keep a small, bounded error body: only Google's `error.message` is ever surfaced,
        # truncated and with the API key redacted (see _error_detail).
        try:
            return exc.code, exc.read(_ERROR_BODY_LIMIT)
        except Exception:
            return exc.code, b""
        finally:
            exc.close()


_ERROR_BODY_LIMIT = 4096
# Temporary overload / rate limit: bounded retries with a short backoff, then a clean LLMError.
_RETRY_STATUSES = (429, 500, 503)
RETRY_DELAYS_SECONDS = (2.0, 5.0)


def _error_detail(raw: bytes, api_key: str) -> str:
    """Google's `error.message` (e.g. 'Invalid JSON payload received. Unknown name ...'),
    truncated, with the key redacted. Empty when the body is not the documented error shape."""
    try:
        message = json.loads(raw[:_ERROR_BODY_LIMIT])["error"]["message"]
    except Exception:
        return ""
    if not isinstance(message, str):
        return ""
    if len(api_key) >= 8:  # real keys only; a short test key would mangle ordinary words
        message = message.replace(api_key, "[redacted]")
    return ": " + " ".join(message.split())[:300]


def _inline_refs(schema: dict) -> dict:
    """Pydantic emits `$defs` + `$ref`; inline them so the response schema is self-contained."""
    defs = schema.get("$defs", {})

    def resolve(node, depth=0):
        if depth > 20:
            raise LLMError("response schema is too deeply nested for Gemini")
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                merged = {**defs[ref.split("/")[-1]], **{k: v for k, v in node.items() if k != "$ref"}}
                return resolve(merged, depth + 1)
            return {k: resolve(v, depth + 1) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v, depth + 1) for v in node]
        return node

    return resolve(schema)


def _extract_json(text: str) -> object:
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Gemini returned invalid JSON: {exc.msg}") from None


class GeminiLLMClient(LLMClient):
    """Structured-response client for Google's Gemini Developer API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: Transport | None = None,
    ) -> None:
        if not api_key:
            raise LLMError(f"set {API_KEY_ENV_VAR} to use the gemini provider")
        if not model or re.fullmatch(r"[A-Za-z0-9._-]+", model) is None:
            raise LLMError("Gemini model name is invalid")
        if timeout_seconds <= 0:
            raise LLMError("Gemini timeout_seconds must be positive")
        if max_response_bytes <= 0:
            raise LLMError("Gemini max_response_bytes must be positive")
        self._api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._transport = transport or (
            lambda url, headers, body, timeout: _urllib_transport(
                url, headers, body, timeout, self.max_response_bytes
            )
        )
        self.calls: list[str] = []
        self.usage: dict[str, int] = {}  # token totals as reported by the provider (telemetry)
        self.retries = 0

    def __repr__(self) -> str:
        return f"GeminiLLMClient(model={self.model!r})"

    @classmethod
    def from_env(
        cls,
        *,
        model: str | None = None,
        transport: Transport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> GeminiLLMClient:
        key = os.environ.get(API_KEY_ENV_VAR, "")
        if not key:
            raise LLMError(f"set {API_KEY_ENV_VAR} to use the gemini provider")
        return cls(
            api_key=key,
            model=model or os.environ.get(MODEL_ENV_VAR) or DEFAULT_MODEL,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            transport=transport,
        )

    def complete_structured(self, *, task: str, system: str, user: str, schema: type[T]) -> T:
        self.calls.append(task)
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            # Documented structured-output fields: JSON MIME type + a JSON Schema for the response.
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": _inline_refs(schema.model_json_schema()),
            },
        }
        url = API_URL_TEMPLATE.format(model=self.model)
        headers = {"content-type": "application/json", "x-goog-api-key": self._api_key}
        body_bytes = json.dumps(payload).encode("utf-8")
        for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
            try:
                status, raw = self._transport(url, headers, body_bytes, self.timeout_seconds)
            except _ResponseTooLarge:
                raise LLMError(f"[{task}] Gemini response exceeded the configured size limit") from None
            except Exception as exc:
                # Never include exception text: custom transports can include request headers.
                raise LLMError(f"[{task}] Gemini request failed: {type(exc).__name__}") from None
            if status not in _RETRY_STATUSES or attempt == len(RETRY_DELAYS_SECONDS):
                break
            self.retries += 1
            time.sleep(RETRY_DELAYS_SECONDS[attempt])

        if len(raw) > self.max_response_bytes:
            raise LLMError(f"[{task}] Gemini response exceeded the configured size limit")
        if status != 200:
            detail = _error_detail(raw, self._api_key)
            if status in (401, 403) or "API key not valid" in detail:
                raise LLMError(f"[{task}] Gemini authentication failed (HTTP {status}){detail}")
            if status == 429:
                raise LLMError(f"[{task}] Gemini rate limit exceeded (HTTP 429){detail}")
            raise LLMError(f"[{task}] Gemini API request failed (HTTP {status}){detail}")
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise LLMError(f"[{task}] Gemini returned a malformed response") from None
        if not isinstance(body, dict):
            raise LLMError(f"[{task}] Gemini returned a malformed response")
        reported = body.get("usageMetadata")
        if isinstance(reported, dict):
            for ours, theirs in (("input_tokens", "promptTokenCount"), ("output_tokens", "candidatesTokenCount")):
                if isinstance(reported.get(theirs), int):
                    self.usage[ours] = self.usage.get(ours, 0) + reported[theirs]
        try:
            candidate = body["candidates"][0]
            finish_reason = candidate.get("finishReason")
            if finish_reason == "MAX_TOKENS":
                raise LLMError(f"[{task}] Gemini response was truncated")
            parts = candidate["content"]["parts"]
            text = "".join(part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str))
            if not text:
                raise LLMError(f"[{task}] Gemini returned no response text")
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"[{task}] Gemini returned a malformed response") from None
        data = _extract_json(text)
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            raise LLMError(
                f"[{task}] Gemini response failed schema validation: {exc.error_count()} error(s)"
            ) from None
