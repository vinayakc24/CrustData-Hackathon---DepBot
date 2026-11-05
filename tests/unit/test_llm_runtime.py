"""LLM runtime scoping, fixture configuration, production provider (HTTP mocked), CLI wiring."""

import ast
import json
import threading
from pathlib import Path

import pytest
from pydantic import BaseModel

from depbot.cli import _llm_factory, build_parser
from depbot.llm.base import FixtureLLMClient, LLMError
from depbot.llm.provider import API_URL, AnthropicLLMClient
from depbot.llm.runtime import (
    LLMNotConfigured,
    current_llm_client,
    fixture_factory,
    get_llm_client,
    use_llm_client,
)
from depbot.orchestrator import run_repository_job
from depbot.state import JobStatus
from tests.unit.test_orchestrator import _stages, job  # noqa: F401  (fixture)


class Answer(BaseModel):
    value: int


# ---------------------------------------------------------------- runtime


def test_unconfigured_raises_clear_error():
    assert current_llm_client() is None
    with pytest.raises(LLMNotConfigured, match="no LLM client is configured"):
        get_llm_client()
    assert issubclass(LLMNotConfigured, LLMError)


def test_scoped_binding_nests_and_restores():
    outer, inner = FixtureLLMClient({}), FixtureLLMClient({})
    with use_llm_client(outer):
        assert get_llm_client() is outer
        with use_llm_client(inner):
            assert get_llm_client() is inner
        with use_llm_client(None):
            assert current_llm_client() is None
        assert get_llm_client() is outer
    assert current_llm_client() is None
    with pytest.raises(TypeError):
        with use_llm_client("not a client"):
            pass


def test_binding_does_not_leak_across_threads():
    seen = {}
    with use_llm_client(FixtureLLMClient({})):
        t = threading.Thread(target=lambda: seen.setdefault("other", current_llm_client()))
        t.start()
        t.join()
    assert seen["other"] is None


def test_fixture_factory_gives_independent_clients(tmp_path):
    path = tmp_path / "llm.json"
    path.write_text(json.dumps({"answer": {"value": 42}}))
    factory = fixture_factory(path)
    a, b = factory(), factory()
    assert a is not b
    assert a.complete_structured(task="answer", system="", user="", schema=Answer).value == 42
    assert a.calls == ["answer"] and b.calls == []
    for bad in ("[1, 2]", "{not json"):
        path.write_text(bad)
        with pytest.raises(LLMError):
            fixture_factory(path)
    with pytest.raises(LLMError, match="not found"):
        fixture_factory(tmp_path / "missing.json")


def test_runtime_module_has_no_stage_imports():
    tree = ast.parse(Path("src/depbot/llm/runtime.py").read_text())
    modules = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
    modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    banned = ("depbot.research", "depbot.code_intelligence", "depbot.orchestrator", "depbot.migration")
    assert not [m for m in modules if m.startswith(banned)]


# ---------------------------------------------------------------- engine scoping


def test_each_job_gets_its_own_client_and_nothing_leaks(job, tmp_path):
    created, seen = [], []

    def factory():
        created.append(FixtureLLMClient({}))
        return created[-1]

    def research(**kw):
        seen.append(get_llm_client())
        return _stages().research(**kw)

    stages = _stages(research=research)
    run_repository_job(job, stages, artifacts_root=tmp_path / "a", llm_factory=factory)
    run_repository_job(job, stages, artifacts_root=tmp_path / "b", llm_factory=factory)
    assert len(created) == 2 and seen == created
    assert current_llm_client() is None  # nothing left bound after the jobs


def test_stage_binding_cannot_leak_out_of_its_job(job, tmp_path):
    def research(**kw):
        use_llm_client(FixtureLLMClient({})).__enter__()  # a stage misbehaving
        return _stages().research(**kw)

    run_repository_job(job, _stages(research=research), artifacts_root=tmp_path / "a")
    assert current_llm_client() is None


def test_unconfigured_llm_in_stage_is_explained_failure(job, tmp_path):
    def research(**kw):
        get_llm_client()

    state, _ = run_repository_job(job, _stages(research=research), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.FAILED_RESEARCH
    assert state.status_reason.startswith("LLM: no LLM client is configured")


def test_factory_failure_blocks_job(job, tmp_path):
    def factory():
        raise LLMError("no key")

    state, outcome = run_repository_job(job, _stages(), artifacts_root=tmp_path / "a", llm_factory=factory)
    assert state.status == JobStatus.BLOCKED and "LLM client could not be created" in state.status_reason


# ---------------------------------------------------------------- provider


class FakeTransport:
    def __init__(self, status=200, body=None):
        self.status, self.requests = status, []
        self.body = body if body is not None else {
            "content": [{"type": "text", "text": '{"value": 7}'}], "stop_reason": "end_turn"}

    def __call__(self, url, headers, body, timeout):
        self.requests.append((url, headers, json.loads(body), timeout))
        return self.status, json.dumps(self.body).encode()


def test_provider_sends_schema_and_validates_response():
    transport = FakeTransport()
    client = AnthropicLLMClient(api_key="sk-test-secret", model="m1", transport=transport)
    assert client.complete_structured(task="t", system="SYS", user="USER", schema=Answer).value == 7
    url, headers, payload, timeout = transport.requests[0]
    assert url == API_URL and headers["x-api-key"] == "sk-test-secret" and timeout > 0
    assert payload["model"] == "m1" and payload["messages"] == [{"role": "user", "content": "USER"}]
    assert payload["system"].startswith("SYS") and '"value"' in payload["system"]
    assert "tools" not in payload
    assert "sk-test" not in repr(client)


@pytest.mark.parametrize("transport,match", [
    (FakeTransport(status=401, body={"error": {"message": "bad key"}}), "HTTP 401: bad key"),
    (FakeTransport(body={"content": [{"type": "text", "text": "Sure! here"}]}), "valid JSON"),
    (FakeTransport(body={"content": [{"type": "text", "text": '{"value": "x"}'}]}), "schema validation"),
    (FakeTransport(body={"content": [], "stop_reason": "max_tokens"}), "truncated"),
])
def test_provider_errors_are_llm_errors(transport, match):
    client = AnthropicLLMClient(api_key="k", transport=transport)
    with pytest.raises(LLMError, match=match):
        client.complete_structured(task="t", system="", user="", schema=Answer)


def test_provider_accepts_fenced_json():
    t = FakeTransport(body={"content": [{"type": "text", "text": '```json\n{"value": 3}\n```'}]})
    assert AnthropicLLMClient(api_key="k", transport=t).complete_structured(
        task="t", system="", user="", schema=Answer).value == 3


def test_provider_from_env(monkeypatch):
    for v in ("LLM_API_KEY", "ANTHROPIC_API_KEY", "DEPBOT_LLM_MODEL"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        AnthropicLLMClient.from_env()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k2")
    monkeypatch.setenv("DEPBOT_LLM_MODEL", "env-model")
    assert AnthropicLLMClient.from_env().model == "env-model"
    assert AnthropicLLMClient.from_env(model="cli-model").model == "cli-model"


# ---------------------------------------------------------------- CLI


def _args(*extra):
    return build_parser().parse_args(["upgrade", "--repo", ".", "--dependency", "d", "--to", "1", *extra])


def test_cli_llm_modes(tmp_path, monkeypatch):
    for v in ("DEPBOT_LLM", "LLM_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    assert _llm_factory(_args()) is None
    assert _llm_factory(_args("--llm", "none")) is None
    with pytest.raises(LLMError, match="requires --llm-fixtures"):
        _llm_factory(_args("--llm", "fixture"))
    (tmp_path / "f.json").write_text('{"answer": {"value": 1}}')
    factory = _llm_factory(_args("--llm", "fixture", "--llm-fixtures", str(tmp_path / "f.json")))
    assert isinstance(factory(), FixtureLLMClient)
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        _llm_factory(_args("--llm", "anthropic"))
    monkeypatch.setenv("LLM_API_KEY", "k")
    assert isinstance(_llm_factory(_args("--llm", "anthropic", "--llm-model", "m"))(), AnthropicLLMClient)
    monkeypatch.setenv("DEPBOT_LLM", "bogus")
    with pytest.raises(LLMError, match="unknown LLM mode"):
        _llm_factory(_args())
