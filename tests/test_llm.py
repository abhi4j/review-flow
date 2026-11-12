"""Client tests against the real openai SDK with only the HTTP transport mocked.

The SDK uses its own `httpx2` client, so the mock is injected as a transport rather than
patched with respx.
"""

import json

import httpx2
import pytest
from pydantic import SecretStr

from reviewflow.config import JsonMode
from reviewflow.errors import NarrativeFailed, NarrativeUnavailable
from reviewflow.narrative.contract import OUTPUT_SCHEMA
from reviewflow.narrative.llm import OpenAICompatibleClient

ANSWER = '{"headline": "ok"}'


class Recorder:
    def __init__(self, status: int = 200, body: dict | None = None,
                 error: Exception | None = None) -> None:
        self.status, self.body, self.error = status, body or {}, error
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.error:
            raise self.error
        return httpx2.Response(self.status, json=self.body)

    def sent(self) -> dict:
        return json.loads(self.requests[-1].content)


def _client(api: Recorder, json_mode: JsonMode = "json_object") -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url="https://llm.example.com/v1", api_key=SecretStr("test-key"),
        model="test-model", json_mode=json_mode, timeout_seconds=10, max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(api)),
    )


def _completion(text: str | None, finish_reason: str = "stop") -> dict:
    return {
        "id": "c1", "object": "chat.completion", "created": 0, "model": "test-model",
        "choices": [{"index": 0, "finish_reason": finish_reason,
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }


async def _complete(client: OpenAICompatibleClient):
    return await client.complete(system="sys", user="facts", schema=OUTPUT_SCHEMA)


async def test_json_object_mode_puts_the_schema_in_the_system_prompt():
    api = Recorder(body=_completion(ANSWER))
    completion = await _complete(_client(api))
    assert completion.text == ANSWER
    assert (completion.input_tokens, completion.output_tokens) == (120, 30)
    assert str(api.requests[-1].url) == "https://llm.example.com/v1/chat/completions"
    assert api.requests[-1].headers["authorization"] == "Bearer test-key"
    sent = api.sent()
    assert sent["model"] == "test-model"
    assert sent["response_format"] == {"type": "json_object"}
    system, user = sent["messages"]
    assert system["content"].startswith("sys") and json.dumps(OUTPUT_SCHEMA) in system["content"]
    assert user == {"role": "user", "content": "facts"}


async def test_json_schema_mode_sends_a_strict_schema():
    api = Recorder(body=_completion(ANSWER))
    await _complete(_client(api, "json_schema"))
    sent = api.sent()
    assert sent["response_format"]["json_schema"]["schema"] == OUTPUT_SCHEMA
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["messages"][0]["content"] == "sys"


@pytest.mark.parametrize("status", [429, 503])
async def test_rate_limit_and_overload_map_to_503(status):
    with pytest.raises(NarrativeUnavailable):
        await _complete(_client(Recorder(status, {"error": {"message": "busy"}})))


async def test_server_error_maps_to_502():
    with pytest.raises(NarrativeFailed):
        await _complete(_client(Recorder(500, {"error": {"message": "oops"}})))


async def test_connection_error_maps_to_502():
    with pytest.raises(NarrativeFailed):
        await _complete(_client(Recorder(error=httpx2.ConnectError("down"))))


async def test_filtered_answer_maps_to_502():
    with pytest.raises(NarrativeFailed):
        await _complete(_client(Recorder(body=_completion(None, "content_filter"))))


async def test_empty_answer_is_left_to_the_validator():
    completion = await _complete(_client(Recorder(body=_completion("   "))))
    assert completion.text == "   "


def test_schema_stays_inside_the_strict_subset():
    """No numeric or length constraints, and every object closed and fully required, as
    strict json_schema mode requires."""
    def walk(node):
        if isinstance(node, dict):
            assert not {"minimum", "maximum", "minLength", "maxLength", "maxItems"} & node.keys()
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(OUTPUT_SCHEMA)
