import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
    _model_kwargs,
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
            name='Test Anthropic',
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

    def test_langchain_adapter_normalizes_a_chat_message(self):
        runnable = Mock()
        runnable.invoke.return_value = SimpleNamespace(
            content='Generated content', id='run_123', usage_metadata={'input_tokens': 12, 'output_tokens': 34}, response_metadata={}
        )
        response = OpenAIAdapter(self.provider, client=runnable).generate(self.request)

        runnable.invoke.assert_called_once_with([('human', 'Write a lesson.')])
        self.assertEqual(response.text, 'Generated content')
        self.assertEqual(response.provider_request_id, 'run_123')
        self.assertTrue(response.metadata['langchain'])

    def test_langchain_openai_uses_native_structured_output(self):
        runnable, structured = Mock(), Mock()
        runnable.with_structured_output.return_value = structured
        structured.invoke.return_value = {'title': 'Outline'}
        response = OpenAIAdapter(self.provider, client=runnable).generate(
            GenerationRequest(model='test-model', prompt='Write JSON.', json_schema={'type': 'object'})
        )

        runnable.with_structured_output.assert_called_once_with({'type': 'object'}, method='json_schema')
        self.assertEqual(response.text, '{"title": "Outline"}')

    def test_langchain_adapter_sends_preview_to_the_callback(self):
        callback, runnable = Mock(), Mock()
        runnable.invoke.return_value = SimpleNamespace(content='Generated content', id='run_123', usage_metadata={}, response_metadata={})

        OpenAIAdapter(self.provider, client=runnable).generate(
            GenerationRequest(model='test-model', prompt='Write.', on_text_delta=callback)
        )

        callback.assert_called_once_with('Generated content')

    def test_all_provider_adapters_use_a_langchain_runnable(self):
        for name, adapter_type in (
            ('Test Anthropic', LLMProvider.AdapterType.ANTHROPIC),
            ('Test Google', LLMProvider.AdapterType.GOOGLE_GENAI),
            ('Test DeepSeek', LLMProvider.AdapterType.OPENAI_COMPATIBLE),
        ):
            provider = LLMProvider.objects.create(
                name=name, adapter_type=adapter_type, api_key_environment_variable=f'{adapter_type.upper()}_API_KEY',
                base_url='https://example.test/v1' if adapter_type == LLMProvider.AdapterType.OPENAI_COMPATIBLE else '',
            )
            runnable = Mock()
            runnable.invoke.return_value = SimpleNamespace(content='Generated content', id='run_123', usage_metadata={}, response_metadata={})
            response = get_adapter(provider, client=runnable).generate(self.request)
            self.assertEqual(response.text, 'Generated content')

    def test_missing_key_fails_before_provider_client_is_created(self):
        with patch.dict(os.environ, {}, clear=True):
            adapter = OpenAIAdapter(self.provider)
            with self.assertRaises(ProviderConfigurationError):
                adapter.api_key()

    def test_model_kwargs_passes_the_configured_request_timeout(self):
        kwargs = _model_kwargs(
            GenerationRequest(model='test-model', prompt='Write.', timeout_seconds=45),
            api_key='test-key',
        )

        self.assertEqual(kwargs['timeout'], 45)

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
