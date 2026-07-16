"""Provider-neutral interfaces for synchronous text generation.

Tasks will call these adapters in a later implementation phase. The adapters do
not persist records and deliberately receive an API key instead of loading one.
"""

from abc import ABC, abstractmethod
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
    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Generate plain text from a provider-neutral request."""


class OpenAIAdapter(LLMAdapter):
    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - depends on deployment extras.
                raise ProviderConfigurationError('The OpenAI SDK is not installed.') from exc
            kwargs = {'api_key': self.api_key()}
            if self.provider.base_url:
                kwargs['base_url'] = self.provider.base_url
            self._client = OpenAI(**kwargs)
        return self._client

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        input_messages = []
        if request.system_instruction:
            input_messages.append({'role': 'system', 'content': request.system_instruction})
        input_messages.append({'role': 'user', 'content': request.prompt})
        parameters = {'model': request.model, 'input': input_messages}
        if request.max_output_tokens is not None:
            parameters['max_output_tokens'] = request.max_output_tokens
        if request.temperature is not None:
            parameters['temperature'] = request.temperature
        response = self._get_client().responses.create(**parameters)
        usage = getattr(response, 'usage', None)
        return GenerationResponse(
            text=getattr(response, 'output_text', ''),
            provider_request_id=getattr(response, '_request_id', None),
            input_tokens=getattr(usage, 'input_tokens', None),
            output_tokens=getattr(usage, 'output_tokens', None),
        )


class OpenAICompatibleAdapter(OpenAIAdapter):
    """Uses the OpenAI client against a provider-specific compatible base URL."""


class AnthropicAdapter(LLMAdapter):
    def _get_client(self):
        if self._client is None:
            try:
                from anthropic import Anthropic
            except ImportError as exc:  # pragma: no cover - depends on deployment extras.
                raise ProviderConfigurationError('The Anthropic SDK is not installed.') from exc
            self._client = Anthropic(api_key=self.api_key(), base_url=self.provider.base_url or None)
        return self._client

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        parameters = {
            'model': request.model,
            'max_tokens': request.max_output_tokens or 4000,
            'messages': [{'role': 'user', 'content': request.prompt}],
        }
        if request.system_instruction:
            parameters['system'] = request.system_instruction
        if request.temperature is not None:
            parameters['temperature'] = request.temperature
        response = self._get_client().messages.create(**parameters)
        text = ''.join(getattr(block, 'text', '') for block in response.content)
        usage = getattr(response, 'usage', None)
        return GenerationResponse(
            text=text,
            provider_request_id=getattr(response, 'id', None),
            input_tokens=getattr(usage, 'input_tokens', None),
            output_tokens=getattr(usage, 'output_tokens', None),
        )


class GoogleGenAIAdapter(LLMAdapter):
    def _get_client(self):
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - depends on deployment extras.
                raise ProviderConfigurationError('The Google GenAI SDK is not installed.') from exc
            self._client = genai.Client(api_key=self.api_key())
        return self._client

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        contents = request.prompt
        if request.system_instruction:
            contents = f'{request.system_instruction}\n\n{request.prompt}'
        response = self._get_client().models.generate_content(model=request.model, contents=contents)
        usage = getattr(response, 'usage_metadata', None)
        return GenerationResponse(
            text=getattr(response, 'text', ''),
            provider_request_id=getattr(response, 'response_id', None),
            input_tokens=getattr(usage, 'prompt_token_count', None),
            output_tokens=getattr(usage, 'candidates_token_count', None),
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
