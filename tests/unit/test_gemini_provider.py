import json

import pytest
from pydantic import BaseModel

from depbot.cli import _llm_factory, build_parser
from depbot.llm.base import LLMError
from depbot.llm.gemini import API_KEY_ENV_VAR, API_URL_TEMPLATE, GeminiLLMClient


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    import depbot.llm.gemini as gemini
    monkeypatch.setattr(gemini.time, "sleep", lambda _seconds: None)


class Answer(BaseModel):
    value: int


class FakeTransport:
    def __init__(self, status=200, body=None, exception=None):
        self.status = status
        self.body = body if body is not None else {
            "candidates": [{"content": {"parts": [{"text": '{"value": 7}'}]}, "finishReason": "STOP"}]
        }
        self.exception = exception
        self.requests = []

    def __call__(self, url, headers, raw_body, timeout):
        if self.exception:
            raise self.exception
        self.requests.append((url, headers, json.loads(raw_body), timeout))
        return self.status, (self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode())


def test_gemini_sends_schema_and_validates_structured_response():
    transport = FakeTransport()
    client = GeminiLLMClient(api_key="gemini-secret", model="gemini-test", transport=transport)
    assert client.complete_structured(task="t", system="SYS", user="USER", schema=Answer).value == 7
    url, headers, payload, timeout = transport.requests[0]
    assert url == API_URL_TEMPLATE.format(model="gemini-test")
    assert headers["x-goog-api-key"] == "gemini-secret"
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "USER"}]}]
    assert payload["systemInstruction"]["parts"][0]["text"] == "SYS"
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert payload["generationConfig"]["responseJsonSchema"] == Answer.model_json_schema()
    assert timeout > 0
    assert "gemini-secret" not in repr(client)


def test_gemini_rejects_schema_invalid_response():
    transport = FakeTransport(body={
        "candidates": [{"content": {"parts": [{"text": '{"value":"wrong"}'}]}}]
    })
    with pytest.raises(LLMError, match="schema validation"):
        GeminiLLMClient(api_key="k", transport=transport).complete_structured(
            task="answer", system="", user="", schema=Answer
        )


@pytest.mark.parametrize(("status", "message"), [
    (401, "authentication failed"),
    (403, "authentication failed"),
    (429, "rate limit"),
    (500, "HTTP 500"),
])
def test_gemini_http_errors_are_useful_and_do_not_expose_key(status, message):
    client = GeminiLLMClient(api_key="top-secret-key", transport=FakeTransport(status=status))
    with pytest.raises(LLMError, match=message) as error:
        client.complete_structured(task="t", system="", user="", schema=Answer)
    assert "top-secret-key" not in str(error.value)


def test_gemini_timeout_is_reported_without_transport_details():
    secret = "key-must-not-leak"
    transport = FakeTransport(exception=TimeoutError(secret))
    client = GeminiLLMClient(api_key=secret, transport=transport)
    with pytest.raises(LLMError, match="TimeoutError") as error:
        client.complete_structured(task="t", system="", user="", schema=Answer)
    assert secret not in str(error.value)


def test_gemini_reads_only_its_dedicated_api_key_and_model_convention(monkeypatch):
    for name in (API_KEY_ENV_VAR, "LLM_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "anthropic-secret")
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiLLMClient.from_env()
    monkeypatch.setenv(API_KEY_ENV_VAR, "gemini-key")
    monkeypatch.setenv("DEPBOT_LLM_MODEL", "env-model")
    assert GeminiLLMClient.from_env().model == "env-model"
    assert GeminiLLMClient.from_env(model="cli-model").model == "cli-model"


def test_gemini_response_read_is_bounded_during_streaming(monkeypatch):
    import depbot.llm.gemini as gemini

    class Response:
        status = 200

        def __init__(self):
            self.read_sizes = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            self.read_sizes.append(size)
            return b"x" * size

    response = Response()
    monkeypatch.setattr(gemini.urllib.request, "urlopen", lambda request, timeout: response)
    client = GeminiLLMClient(api_key="k", max_response_bytes=20)
    with pytest.raises(LLMError, match="size limit"):
        client.complete_structured(task="t", system="", user="", schema=Answer)
    assert sum(response.read_sizes) == 21


def test_cli_accepts_gemini_and_requires_key(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    args = build_parser().parse_args([
        "upgrade", "--repo", ".", "--dependency", "d", "--to", "1", "--llm", "gemini"
    ])
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        _llm_factory(args)
    monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
    args = build_parser().parse_args([
        "upgrade", "--repo", ".", "--dependency", "d", "--to", "1",
        "--llm", "gemini", "--llm-model", "cli-model",
    ])
    factory = _llm_factory(args)
    assert isinstance(factory(), GeminiLLMClient)
    assert factory().model == "cli-model"



class Nested(BaseModel):
    items: list[Answer]


def test_nested_schema_refs_are_inlined_for_gemini():
    transport = FakeTransport(body={"candidates": [{"content": {"parts": [{"text": '{"items": [{"value": 1}]}'}]}}]})
    GeminiLLMClient(api_key="k", transport=transport).complete_structured(task="t", system="", user="", schema=Nested)
    schema = transport.requests[0][2]["generationConfig"]["responseJsonSchema"]
    assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)
    assert schema["properties"]["items"]["items"]["properties"]["value"]["type"] == "integer"


def test_400_surfaces_googles_message_with_key_redacted():
    body = json.dumps({"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                 "message": "Invalid JSON payload received. Unknown name \"foo\" (key top-secret-key)"}}).encode()
    client = GeminiLLMClient(api_key="top-secret-key", transport=FakeTransport(status=400, body=body))
    with pytest.raises(LLMError, match=r"HTTP 400\): Invalid JSON payload received\. Unknown name") as error:
        client.complete_structured(task="t", system="", user="", schema=Answer)
    assert "top-secret-key" not in str(error.value) and "[redacted]" in str(error.value)


def test_invalid_key_400_is_reported_as_authentication():
    body = json.dumps({"error": {"code": 400, "message": "API key not valid. Please pass a valid API key."}}).encode()
    with pytest.raises(LLMError, match="authentication failed .*API key not valid"):
        GeminiLLMClient(api_key="k", transport=FakeTransport(status=400, body=body)).complete_structured(
            task="t", system="", user="", schema=Answer)



class SequenceTransport(FakeTransport):
    def __init__(self, statuses):
        super().__init__()
        self.statuses = list(statuses)

    def __call__(self, url, headers, raw_body, timeout):
        self.requests.append(json.loads(raw_body))
        status = self.statuses.pop(0)
        if status != 200:
            return status, json.dumps({"error": {"message": "This model is currently experiencing high demand."}}).encode()
        return 200, json.dumps(self.body).encode()


def test_temporary_overload_is_retried_then_succeeds(monkeypatch):
    import depbot.llm.gemini as gemini
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    transport = SequenceTransport([503, 429, 200])
    client = GeminiLLMClient(api_key="k", transport=transport)
    assert client.complete_structured(task="t", system="", user="", schema=Answer).value == 7
    assert len(transport.requests) == 3 and client.retries == 2


def test_persistent_overload_fails_cleanly_after_bounded_retries(monkeypatch):
    import depbot.llm.gemini as gemini
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    transport = SequenceTransport([503, 503, 503, 200])
    with pytest.raises(LLMError, match="HTTP 503\\): This model is currently experiencing high demand"):
        GeminiLLMClient(api_key="k", transport=transport).complete_structured(task="t", system="", user="", schema=Answer)
    assert len(transport.requests) == 3  # 1 try + 2 retries, never unbounded


def test_client_errors_are_not_retried(monkeypatch):
    import depbot.llm.gemini as gemini
    monkeypatch.setattr(gemini.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("no sleep")))
    transport = SequenceTransport([404])
    with pytest.raises(LLMError, match="HTTP 404"):
        GeminiLLMClient(api_key="k", transport=transport).complete_structured(task="t", system="", user="", schema=Answer)
    assert len(transport.requests) == 1
