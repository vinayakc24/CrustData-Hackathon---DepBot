"""Google Gemini Developer API provider using bounded stdlib HTTPS requests."""

from __future__ import annotations

import json
import os
import re
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
DEFAULT_MODEL = "gemini-2.5-flash"
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
        # Provider error bodies are not needed and may echo sensitive request data.
        exc.close()
        return exc.code, b""


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
            "generationConfig": {
                "responseFormat": {
                    "text": {
                        "mimeType": "application/json",
                        "schema": schema.model_json_schema(),
                    }
                },
            },
        }
        url = API_URL_TEMPLATE.format(model=self.model)
        headers = {"content-type": "application/json", "x-goog-api-key": self._api_key}
        try:
            status, raw = self._transport(
                url, headers, json.dumps(payload).encode("utf-8"), self.timeout_seconds
            )
        except _ResponseTooLarge:
            raise LLMError(f"[{task}] Gemini response exceeded the configured size limit") from None
        except Exception as exc:
            # Never include exception text: custom transports can include request headers.
            raise LLMError(f"[{task}] Gemini request failed: {type(exc).__name__}") from None

        if len(raw) > self.max_response_bytes:
            raise LLMError(f"[{task}] Gemini response exceeded the configured size limit")
        if status in (401, 403):
            raise LLMError(f"[{task}] Gemini authentication failed (HTTP {status})")
        if status == 429:
            raise LLMError(f"[{task}] Gemini rate limit exceeded (HTTP 429)")
        if status != 200:
            raise LLMError(f"[{task}] Gemini API request failed (HTTP {status})")
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise LLMError(f"[{task}] Gemini returned a malformed response") from None
        if not isinstance(body, dict):
            raise LLMError(f"[{task}] Gemini returned a malformed response")
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
