"""LLM client for the narrative, for any OpenAI-compatible Chat Completions API.

Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL; evaluated on DeepSeek V4.1 Flash. Providers
differ in how they enforce JSON, so LLM_JSON_MODE picks one:

  json_schema  the provider constrains decoding to the schema
  json_object  the provider guarantees JSON and the schema travels in the system prompt;
               the widest support (DeepSeek's own Chat Completions API offers only this)

Either way the service validates the answer, because no schema can express "every number
must be a fact reference". The `LLM` protocol exists so tests can swap in a fake.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2
import openai
from openai.types.chat import ChatCompletionMessageParam
from openai.types.shared_params import ResponseFormatJSONObject, ResponseFormatJSONSchema
from pydantic import SecretStr

from reviewflow.config import JsonMode, Settings
from reviewflow.errors import NarrativeFailed, NarrativeUnavailable

log = logging.getLogger(__name__)

# Room for the private `analysis` field ahead of the answer; at 8,000 tokens some answers
# were cut off mid-JSON.
_MAX_OUTPUT_TOKENS = 16_384
# Rate limits and overload clear up; the caller should retry later.
_UNAVAILABLE = frozenset({429, 503})


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    input_tokens: int | None
    output_tokens: int | None


class LLM(Protocol):
    model: str

    async def complete(self, *, system: str, user: str, schema: dict[str, Any]) -> Completion:
        ...


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model: str,
        json_mode: JsonMode,
        timeout_seconds: float,
        max_retries: int = 2,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._json_mode = json_mode
        self._client = openai.AsyncOpenAI(
            base_url=base_url, api_key=api_key.get_secret_value(), timeout=timeout_seconds,
            max_retries=max_retries, http_client=http_client,
        )

    async def complete(self, *, system: str, user: str, schema: dict[str, Any]) -> Completion:
        if self._json_mode == "json_object":
            system = (f"{system}\nAnswer with one JSON object that matches this JSON Schema:\n"
                      f"{json.dumps(schema)}")
            response_format: ResponseFormatJSONObject | ResponseFormatJSONSchema = {
                "type": "json_object"}
        else:
            response_format = {"type": "json_schema", "json_schema": {
                "name": "narrative", "schema": schema, "strict": True}}
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            response = await self._client.chat.completions.create(
                model=self.model, messages=messages, max_tokens=_MAX_OUTPUT_TOKENS,
                response_format=response_format,
            )
        except openai.APIStatusError as exc:
            if exc.status_code in _UNAVAILABLE:
                raise NarrativeUnavailable("The LLM provider is rate limited or overloaded; "
                                           "retry later.", retry_after=30) from exc
            log.error("llm request failed", extra={"fields": {"status": exc.status_code}})
            raise NarrativeFailed("The LLM provider returned an error.") from exc
        except openai.APIConnectionError as exc:
            log.error("llm unreachable", extra={"fields": {"error": type(exc).__name__}})
            raise NarrativeFailed("The LLM provider could not be reached.") from exc
        if not response.choices:
            raise NarrativeFailed("The LLM provider returned no answer.")
        choice = response.choices[0]
        if choice.finish_reason == "content_filter":
            raise NarrativeFailed("The LLM provider filtered the answer.")
        content = choice.message.content or ""
        if choice.finish_reason == "length" or not content.strip():
            # Left to the validator, which asks for a repair; logged so it can be diagnosed.
            log.warning("llm answer truncated or empty",
                        extra={"fields": {"finish_reason": choice.finish_reason}})
        usage = response.usage
        return Completion(
            content,
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
        )


def create_llm(settings: Settings) -> LLM | None:
    if settings.llm_api_key is None:
        return None
    return OpenAICompatibleClient(
        base_url=settings.llm_base_url, api_key=settings.llm_api_key, model=settings.llm_model,
        json_mode=settings.llm_json_mode, timeout_seconds=settings.llm_timeout_seconds,
    )
