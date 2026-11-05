"""Run-scoped LLM client access (owner: Member A).

Stage functions keep their frozen signatures and obtain the client for the
current repository job with ``get_llm_client()``. The engine binds one client
per job inside a copied ``contextvars`` context, so a client (and its call log,
cache, or fixture state) never leaks between jobs or concurrent runs.

    from depbot.llm.runtime import LLMNotConfigured, get_llm_client

    try:
        client = get_llm_client()
    except LLMNotConfigured:
        ...  # deterministic fallback, or raise StageError(..., blocked=True)

Tests and the deterministic demo bind a fixture client explicitly::

    with use_llm_client(FixtureLLMClient({"breaking_changes": {...}})):
        research_release(event=..., dependency=..., artifacts_dir=...)

This module imports only ``depbot.llm.base`` (no research, code-intelligence or
orchestrator imports).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from depbot.llm.base import FixtureLLMClient, LLMClient, LLMError

LLMFactory = Callable[[], LLMClient]

_current: ContextVar[LLMClient | None] = ContextVar("depbot_llm_client", default=None)


class LLMNotConfigured(LLMError):
    """No LLM client is bound for the current run (e.g. `--llm none`)."""


def get_llm_client() -> LLMClient:
    """The client bound to the current job. Raises ``LLMNotConfigured`` if none."""
    client = _current.get()
    if client is None:
        raise LLMNotConfigured(
            "no LLM client is configured for this run (use --llm fixture|anthropic, "
            "or bind one with depbot.llm.runtime.use_llm_client)"
        )
    return client


def current_llm_client() -> LLMClient | None:
    """The bound client, or None. For optional LLM use without exception handling."""
    return _current.get()


@contextmanager
def use_llm_client(client: LLMClient | None) -> Iterator[LLMClient | None]:
    """Bind ``client`` for the duration of the block (``None`` explicitly unbinds)."""
    if client is not None and not isinstance(client, LLMClient):
        raise TypeError(f"expected an LLMClient, got {type(client).__name__}")
    token = _current.set(client)
    try:
        yield client
    finally:
        _current.reset(token)


def load_fixture_responses(path: Path) -> dict[str, dict | list]:
    """Read a ``{task_label: response_json}`` mapping for ``FixtureLLMClient``."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LLMError(f"LLM fixture file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM fixture file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
        raise LLMError(f"LLM fixture file must map task labels to responses: {path}")
    return data


def fixture_factory(path: Path) -> LLMFactory:
    """Factory returning a fresh ``FixtureLLMClient`` per job (independent call logs)."""
    responses = load_fixture_responses(path)  # validate eagerly, at CLI start
    return lambda: FixtureLLMClient(json.loads(json.dumps(responses)))
