"""Production LLM provider: Anthropic Messages API over stdlib HTTP (owner: Member A).

* The API key is read from ``LLM_API_KEY`` (plan.md §7) or ``ANTHROPIC_API_KEY``;
  it is sent only in the request header and never logged, echoed or stored.
* The model must return one JSON document matching the requested Pydantic
  schema. Anything else raises ``LLMError``; callers never get unvalidated text.
* No tools are offered to the model; it cannot run commands or reach the repo.
"""

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

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"
KEY_ENV_VARS = ("LLM_API_KEY", "ANTHROPIC_API_KEY")
MODEL_ENV_VAR = "DEPBOT_LLM_MODEL"

# transport(url, headers, body_bytes, timeout) -> (status, response_bytes)
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]

_JSON_INSTRUCTIONS = (
    "Respond with exactly one JSON document and nothing else (no prose, no code fences). "
    "It must validate against this JSON Schema:\n"
)


def _urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"LLM request failed: {type(exc).__name__}") from None


def _extract_json(text: str) -> object:
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"model did not return valid JSON: {exc.msg}") from None


class AnthropicLLMClient(LLMClient):
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
        timeout_seconds: float = 120.0,
        transport: Transport | None = None,
    ) -> None:
        if not api_key:
            raise LLMError("an API key is required for the anthropic provider")
        self._api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self._transport = transport or _urllib_transport
        self.calls: list[str] = []

    def __repr__(self) -> str:  # never expose the key
        return f"AnthropicLLMClient(model={self.model!r})"

    @classmethod
    def from_env(cls, *, model: str | None = None, transport: Transport | None = None) -> AnthropicLLMClient:
        key = next((os.environ[v] for v in KEY_ENV_VARS if os.environ.get(v)), "")
        if not key:
            raise LLMError(f"set one of {', '.join(KEY_ENV_VARS)} to use the anthropic provider")
        return cls(api_key=key, model=model or os.environ.get(MODEL_ENV_VAR) or DEFAULT_MODEL,
                   transport=transport)

    def complete_structured(self, *, task: str, system: str, user: str, schema: type[T]) -> T:
        self.calls.append(task)
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system + "\n\n" + _JSON_INSTRUCTIONS + json.dumps(schema.model_json_schema()),
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "content-type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
        }
        status, raw = self._transport(API_URL, headers, json.dumps(payload).encode(), self.timeout_seconds)
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise LLMError(f"[{task}] provider returned non-JSON HTTP {status}") from None
        if status != 200:
            message = body.get("error", {}).get("message", "") if isinstance(body, dict) else ""
            raise LLMError(f"[{task}] provider HTTP {status}: {message[:200]}")
        if body.get("stop_reason") == "max_tokens":
            raise LLMError(f"[{task}] response truncated at max_tokens={self.max_tokens}")
        text = "".join(
            part.get("text", "") for part in body.get("content", []) if part.get("type") == "text"
        )
        data = _extract_json(text)
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            raise LLMError(f"[{task}] response failed schema validation: {exc.error_count()} error(s)") from None
