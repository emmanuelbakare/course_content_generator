"""LangChain-backed, provider-neutral generation adapters.

Provider SDKs are intentionally accessed only through LangChain integration
packages.  The application owns job persistence, authorization, retries, and
Pydantic validation; LangChain owns provider-specific model invocation.
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .models import LLMProvider


class ProviderConfigurationError(RuntimeError):
    """Raised when a provider cannot be used with the saved configuration."""


@dataclass(frozen=True)
class GenerationRequest:
    model: str
    prompt: str
    system_instruction: str = ''
    temperature: float | None = None
    max_output_tokens: int | None = None
    timeout_seconds: int | None = None
    json_schema: dict[str, Any] | None = None
    json_schema_name: str | None = None
    on_text_delta: Callable[[str], None] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class GenerationResponse:
    text: str
    provider_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class LLMAdapter(ABC):
    def __init__(self, provider: LLMProvider, *, client=None):
        self.provider = provider
        self._client = client

    def api_key(self) -> str:
        api_key = self.provider.get_api_key()
        if not api_key:
            raise ProviderConfigurationError(
                f'Environment variable {self.provider.api_key_environment_variable} is not configured.'
            )
        return api_key

    @abstractmethod
    def _build_model(self, request: GenerationRequest):
        """Return the appropriate LangChain chat model."""

    def _model(self, request: GenerationRequest):
        return self._client if self._client is not None else self._build_model(request)

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        model = self._model(request)
        messages = []
        if request.system_instruction:
            messages.append(('system', request.system_instruction))
        messages.append(('human', request.prompt))

        if request.json_schema:
            model = self._structured_model(model, request)
        result = model.invoke(messages)
        text = _result_to_text(result)
        if request.on_text_delta:
            # LangChain integrations can stream ordinary chat tokens, but a
            # structured model returns its validated object after completion.
            # Send that owner-only preview through the same callback.
            request.on_text_delta(text)
        return _normalize_response(result, text, structured=bool(request.json_schema))

    def _structured_model(self, model, request: GenerationRequest):
        return model.with_structured_output(request.json_schema)


class OpenAIAdapter(LLMAdapter):
    def _build_model(self, request: GenerationRequest):
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover - deployment dependency.
            raise ProviderConfigurationError('The langchain-openai package is not installed.') from exc
        kwargs = _model_kwargs(request, api_key=self.api_key())
        if self.provider.base_url:
            kwargs['base_url'] = self.provider.base_url
        return ChatOpenAI(**kwargs)

    def _structured_model(self, model, request: GenerationRequest):
        # Native OpenAI Structured Outputs are used for schema adherence.
        return model.with_structured_output(request.json_schema, method='json_schema')


class AnthropicAdapter(LLMAdapter):
    def _build_model(self, request: GenerationRequest):
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:  # pragma: no cover - deployment dependency.
            raise ProviderConfigurationError('The langchain-anthropic package is not installed.') from exc
        kwargs = _model_kwargs(request, api_key=self.api_key())
        if self.provider.base_url:
            kwargs['base_url'] = self.provider.base_url
        return ChatAnthropic(**kwargs)


class GoogleGenAIAdapter(LLMAdapter):
    def _build_model(self, request: GenerationRequest):
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as exc:  # pragma: no cover - deployment dependency.
            raise ProviderConfigurationError('The langchain-google-genai package is not installed.') from exc
        return ChatGoogleGenerativeAI(**_model_kwargs(request, api_key=self.api_key()))


class OpenAICompatibleAdapter(LLMAdapter):
    """Route the supplied DeepSeek and Groq catalogs through native integrations."""

    def _build_model(self, request: GenerationRequest):
        name = self.provider.name.casefold()
        if 'deepseek' in name:
            try:
                from langchain_deepseek import ChatDeepSeek
            except ImportError as exc:  # pragma: no cover - deployment dependency.
                raise ProviderConfigurationError('The langchain-deepseek package is not installed.') from exc
            return ChatDeepSeek(**_model_kwargs(request, api_key=self.api_key()))
        if 'groq' in name:
            try:
                from langchain_groq import ChatGroq
            except ImportError as exc:  # pragma: no cover - deployment dependency.
                raise ProviderConfigurationError('The langchain-groq package is not installed.') from exc
            return ChatGroq(**_model_kwargs(request, api_key=self.api_key()))
        # A user-defined compatible endpoint remains available through
        # LangChain's OpenAI integration; application code never imports the
        # OpenAI SDK directly.
        return OpenAIAdapter(self.provider)._build_model(request)


def _model_kwargs(request: GenerationRequest, *, api_key: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {'model': request.model, 'api_key': api_key}
    if request.temperature is not None:
        kwargs['temperature'] = request.temperature
    if request.max_output_tokens is not None:
        kwargs['max_tokens'] = request.max_output_tokens
    if request.timeout_seconds is not None:
        kwargs['timeout'] = request.timeout_seconds
    return kwargs


def _result_to_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return json.dumps(result)
    if hasattr(result, 'model_dump'):
        return json.dumps(result.model_dump(mode='json'))
    content = getattr(result, 'content', result)
    if isinstance(content, str):
        return content
    return json.dumps(content)


def _normalize_response(result: Any, text: str, *, structured: bool) -> GenerationResponse:
    usage = getattr(result, 'usage_metadata', {}) or {}
    metadata = dict(getattr(result, 'response_metadata', {}) or {})
    metadata['langchain'] = True
    metadata['structured_output_requested'] = structured
    return GenerationResponse(
        text=text,
        provider_request_id=getattr(result, 'id', None),
        input_tokens=usage.get('input_tokens'),
        output_tokens=usage.get('output_tokens'),
        metadata=metadata,
    )


ADAPTERS = {
    LLMProvider.AdapterType.OPENAI: OpenAIAdapter,
    LLMProvider.AdapterType.ANTHROPIC: AnthropicAdapter,
    LLMProvider.AdapterType.GOOGLE_GENAI: GoogleGenAIAdapter,
    LLMProvider.AdapterType.OPENAI_COMPATIBLE: OpenAICompatibleAdapter,
}


def get_adapter(provider: LLMProvider, *, client=None) -> LLMAdapter:
    if not provider.enabled:
        raise ProviderConfigurationError(f'Provider {provider.name} is disabled.')
    try:
        adapter_class = ADAPTERS[provider.adapter_type]
    except KeyError as exc:
        raise ProviderConfigurationError(f'Unsupported provider adapter: {provider.adapter_type}') from exc
    return adapter_class(provider, client=client)
