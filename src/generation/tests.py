import os
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from courses.services import create_draft_course

from .adapters import (
    AnthropicAdapter,
    GenerationRequest,
    GoogleGenAIAdapter,
    OpenAIAdapter,
    OpenAICompatibleAdapter,
    ProviderConfigurationError,
    get_adapter,
)
from .models import GenerationAttempt, GenerationJob, GenerationSettings, LLMModel, LLMProvider


class GenerationConfigurationTests(TestCase):
    def setUp(self):
        self.provider = LLMProvider.objects.create(
            name='OpenAI',
            adapter_type=LLMProvider.AdapterType.OPENAI,
            api_key_environment_variable='OPENAI_API_KEY',
        )
        self.model = LLMModel.objects.create(provider=self.provider, identifier='test-model')

    def test_provider_reads_key_only_from_process_environment(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}, clear=False):
            self.assertEqual(self.provider.get_api_key(), 'test-key')
            self.assertTrue(self.provider.is_api_key_configured)

    def test_provider_requires_safe_environment_variable_name(self):
        self.provider.api_key_environment_variable = 'not-valid'
        with self.assertRaises(ValidationError):
            self.provider.full_clean()

    def test_compatible_provider_requires_base_url(self):
        provider = LLMProvider(
            name='Local endpoint',
            adapter_type=LLMProvider.AdapterType.OPENAI_COMPATIBLE,
            api_key_environment_variable='LOCAL_API_KEY',
        )
        with self.assertRaises(ValidationError):
            provider.full_clean()

    def test_singleton_settings_validates_model_provider_pair(self):
        other_provider = LLMProvider.objects.create(
            name='Anthropic',
            adapter_type=LLMProvider.AdapterType.ANTHROPIC,
            api_key_environment_variable='ANTHROPIC_API_KEY',
        )
        settings = GenerationSettings(default_provider=other_provider, default_model=self.model)
        with self.assertRaises(ValidationError):
            settings.full_clean()

    def test_generation_job_validates_lesson_target_and_model_provider(self):
        owner = get_user_model().objects.create_user('author')
        course = create_draft_course(owner, title='Course', topic='Topic')
        job = GenerationJob(
            course=course,
            provider=self.provider,
            llm_model=self.model,
            job_type=GenerationJob.JobType.LESSON,
        )
        with self.assertRaises(ValidationError):
            job.full_clean()


class AdapterTests(TestCase):
    def setUp(self):
        self.provider = LLMProvider.objects.create(
            name='OpenAI',
            adapter_type=LLMProvider.AdapterType.OPENAI,
            api_key_environment_variable='OPENAI_API_KEY',
        )
        self.model = LLMModel.objects.create(provider=self.provider, identifier='attempt-model')
        self.request = GenerationRequest(model='test-model', prompt='Write a lesson.')

    def test_adapter_factory_selects_each_provider_adapter(self):
        self.assertIsInstance(get_adapter(self.provider, client=object()), OpenAIAdapter)
        expected = {
            LLMProvider.AdapterType.ANTHROPIC: AnthropicAdapter,
            LLMProvider.AdapterType.GOOGLE_GENAI: GoogleGenAIAdapter,
            LLMProvider.AdapterType.OPENAI_COMPATIBLE: OpenAICompatibleAdapter,
        }
        for adapter_type, adapter_class in expected.items():
            provider = LLMProvider.objects.create(
                name=adapter_type,
                adapter_type=adapter_type,
                api_key_environment_variable=f'{adapter_type.upper()}_API_KEY',
                base_url='https://example.test/v1' if adapter_type == LLMProvider.AdapterType.OPENAI_COMPATIBLE else '',
            )
            self.assertIsInstance(get_adapter(provider, client=object()), adapter_class)

    def test_openai_adapter_normalizes_mocked_response(self):
        mocked_client = SimpleNamespace(
            responses=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(
                    output_text='Generated content',
                    _request_id='req_123',
                    usage=SimpleNamespace(input_tokens=12, output_tokens=34),
                )
            )
        )
        response = OpenAIAdapter(self.provider, client=mocked_client).generate(self.request)

        self.assertEqual(response.text, 'Generated content')
        self.assertEqual(response.provider_request_id, 'req_123')
        self.assertEqual(response.output_tokens, 34)

    def test_anthropic_adapter_normalizes_mocked_response(self):
        provider = LLMProvider.objects.create(
            name='Anthropic',
            adapter_type=LLMProvider.AdapterType.ANTHROPIC,
            api_key_environment_variable='ANTHROPIC_API_KEY',
        )
        mocked_client = SimpleNamespace(
            messages=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(
                    id='msg_123',
                    content=[SimpleNamespace(text='Generated content')],
                    usage=SimpleNamespace(input_tokens=12, output_tokens=34),
                )
            )
        )

        response = AnthropicAdapter(provider, client=mocked_client).generate(self.request)

        self.assertEqual(response.text, 'Generated content')
        self.assertEqual(response.provider_request_id, 'msg_123')

    def test_google_adapter_normalizes_mocked_response(self):
        provider = LLMProvider.objects.create(
            name='Google',
            adapter_type=LLMProvider.AdapterType.GOOGLE_GENAI,
            api_key_environment_variable='GOOGLE_API_KEY',
        )
        mocked_client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kwargs: SimpleNamespace(
                    text='Generated content',
                    response_id='google_123',
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=12,
                        candidates_token_count=34,
                    ),
                )
            )
        )

        response = GoogleGenAIAdapter(provider, client=mocked_client).generate(self.request)

        self.assertEqual(response.text, 'Generated content')
        self.assertEqual(response.provider_request_id, 'google_123')

    def test_missing_key_fails_before_provider_client_is_created(self):
        with patch.dict(os.environ, {}, clear=True):
            adapter = OpenAIAdapter(self.provider)
            with self.assertRaises(ProviderConfigurationError):
                adapter.api_key()

    def test_attempt_requires_the_job_provider_and_model(self):
        owner = get_user_model().objects.create_user('author')
        course = create_draft_course(owner, title='Course', topic='Topic')
        job = GenerationJob.objects.create(
            course=course,
            provider=self.provider,
            llm_model=LLMModel.objects.create(provider=self.provider, identifier='job-model'),
            job_type=GenerationJob.JobType.CURRICULUM,
        )
        attempt = GenerationAttempt(
            job=job,
            provider=self.provider,
            llm_model=self.model,
            attempt_number=1,
            prompt_template_version='v1',
        )
        with self.assertRaises(ValidationError):
            attempt.full_clean()
