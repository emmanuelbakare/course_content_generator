import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import GenerationSettings, LLMModel, LLMProvider


class GenerationSettingsViewTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            'staff', password='safe-password', is_staff=True
        )
        self.author = get_user_model().objects.create_user('author', password='safe-password')
        self.settings_url = reverse('generation:settings')

    def create_provider(self, *, name='OpenAI', adapter_type=LLMProvider.AdapterType.OPENAI):
        return LLMProvider.objects.create(
            name=name,
            adapter_type=adapter_type,
            api_key_environment_variable=f'{name.upper()}_API_KEY',
        )

    def test_settings_are_staff_only(self):
        self.client.force_login(self.author)

        response = self.client.get(self.settings_url)

        self.assertEqual(response.status_code, 403)

    def test_staff_can_create_provider_without_any_key_entry_field(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            self.settings_url,
            {
                'action': 'save_provider',
                'name': 'OpenAI',
                'adapter_type': LLMProvider.AdapterType.OPENAI,
                'base_url': '',
                'enabled': 'on',
                'display_order': 0,
            },
        )

        provider = LLMProvider.objects.get(name='OpenAI')
        self.assertRedirects(response, f'{self.settings_url}?provider={provider.pk}')
        self.assertEqual(provider.api_key_environment_variable, 'OPENAI_API_KEY')
        page = self.client.get(self.settings_url)
        self.assertNotContains(page, 'OPENAI_API_KEY')
        self.assertNotContains(page, 'api_key_environment_variable')

    def test_key_indicator_is_safe_and_never_includes_key_value(self):
        provider = self.create_provider()
        self.client.force_login(self.staff)

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'super-secret-value'}, clear=False):
            response = self.client.get(self.settings_url)

        self.assertContains(response, 'API key configured')
        self.assertNotContains(response, 'super-secret-value')
        self.assertNotContains(response, 'OPENAI_API_KEY')
        self.assertContains(response, provider.name)

    def test_staff_can_create_filter_and_delete_models_by_provider(self):
        provider = self.create_provider()
        other_provider = self.create_provider(
            name='Test Anthropic', adapter_type=LLMProvider.AdapterType.ANTHROPIC
        )
        other_model = LLMModel.objects.create(provider=other_provider, identifier='other-model')
        self.client.force_login(self.staff)

        response = self.client.post(
            self.settings_url,
            {
                'action': 'save_model',
                'provider_id': provider.pk,
                'identifier': 'course-model',
                'display_name': 'Course model',
                'enabled': 'on',
                'default_temperature': '0.4',
                'default_max_output_tokens': 2000,
                'display_order': 0,
            },
        )
        model = LLMModel.objects.get(provider=provider, identifier='course-model')
        self.assertRedirects(response, f'{self.settings_url}?provider={provider.pk}')

        response = self.client.get(reverse('generation:provider-models', args=[provider.pk]))
        self.assertContains(response, 'Course model')
        self.assertNotContains(response, other_model.identifier)

        response = self.client.post(
            self.settings_url,
            {'action': 'delete_model', 'model_id': model.pk},
        )
        self.assertRedirects(response, f'{self.settings_url}?provider={provider.pk}')
        self.assertFalse(LLMModel.objects.filter(pk=model.pk).exists())

    def test_default_selection_rejects_model_from_another_provider(self):
        provider = self.create_provider()
        other_provider = self.create_provider(
            name='Test Anthropic', adapter_type=LLMProvider.AdapterType.ANTHROPIC
        )
        other_model = LLMModel.objects.create(provider=other_provider, identifier='other-model')
        self.client.force_login(self.staff)

        response = self.client.post(
            self.settings_url,
            {
                'action': 'save_defaults',
                'default_provider': provider.pk,
                'default_model': other_model.pk,
                'default_temperature': '0.7',
                'max_output_tokens': 4000,
                'max_continuations': 3,
                'request_timeout_seconds': 120,
                'max_retries': 3,
                'daily_cost_budget': '',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(GenerationSettings.get_solo().default_provider)

    def test_staff_can_select_enabled_default_provider_and_model(self):
        provider = self.create_provider()
        model = LLMModel.objects.create(provider=provider, identifier='course-model')
        self.client.force_login(self.staff)

        response = self.client.post(
            self.settings_url,
            {
                'action': 'save_defaults',
                'default_provider': provider.pk,
                'default_model': model.pk,
                'default_temperature': '0.5',
                'max_output_tokens': 3000,
                'curriculum_response_limit': 4,
                'max_continuations': 2,
                'request_timeout_seconds': 60,
                'max_retries': 2,
                'daily_cost_budget': '10.00',
            },
        )

        self.assertRedirects(response, f'{self.settings_url}?tab=generation')
        settings = GenerationSettings.get_solo()
        self.assertEqual(settings.default_provider, provider)
        self.assertEqual(settings.default_model, model)
        self.assertEqual(settings.max_output_tokens, 3000)
        self.assertEqual(settings.curriculum_response_limit, 4)

        page = self.client.get(self.settings_url)
        self.assertContains(page, 'Curriculum response limit')
        self.assertContains(page, 'Leave blank for Unlimited')

    def test_settings_page_separates_generation_controls_and_provider_catalog_into_tabs(self):
        provider = self.create_provider()
        self.client.force_login(self.staff)

        controls = self.client.get(f'{self.settings_url}?tab=generation')
        providers = self.client.get(f'{self.settings_url}?tab=providers')

        self.assertContains(controls, 'Generation controls')
        self.assertContains(controls, 'role="tab"')
        self.assertContains(controls, 'aria-selected="true"')
        self.assertContains(controls, 'id="provider-models-panel" role="tabpanel" aria-labelledby="provider-models-tab" hidden')
        self.assertContains(providers, 'LLM Providers')
        self.assertContains(providers, provider.name)
        self.assertContains(providers, 'id="generation-controls-panel" role="tabpanel" aria-labelledby="generation-controls-tab" hidden')

    def test_provider_model_page_uses_seeded_catalog_and_safe_key_status(self):
        groq = LLMProvider.objects.get(name='Groq')
        self.client.force_login(self.staff)

        response = self.client.get(f'{self.settings_url}?provider={groq.pk}')

        self.assertContains(response, 'LLM Providers')
        self.assertContains(response, 'Provider &amp; Model configuration')
        self.assertContains(response, 'Groq-hosted chat models.')
        self.assertContains(response, 'llama-3.3-70b-versatile')
        self.assertContains(response, 'openai/gpt-oss-120b')
        self.assertContains(response, 'API key ')
        self.assertNotContains(response, 'GROQ_API_KEY')

    def test_use_model_sets_the_default_provider_and_model_for_generation(self):
        provider = self.create_provider(name='Selected provider')
        model = LLMModel.objects.create(provider=provider, identifier='selected-model')
        self.client.force_login(self.staff)

        response = self.client.post(
            self.settings_url,
            {'action': 'set_default_model', 'model_id': model.pk},
        )

        self.assertRedirects(response, f'{self.settings_url}?provider={provider.pk}')
        settings = GenerationSettings.get_solo()
        self.assertEqual(settings.default_provider, provider)
        self.assertEqual(settings.default_model, model)

        page = self.client.get(f'{self.settings_url}?provider={provider.pk}')
        self.assertContains(page, 'Current model:')
        self.assertContains(page, model.identifier)
        self.assertContains(page, 'provider-card-selector')
        self.assertNotContains(page, '>Enabled<')
