import io
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from .management.commands.production_readiness import Command
from .models import GenerationSettings, LLMModel, LLMProvider


class ProductionReadinessCommandTests(TestCase):
    def setUp(self):
        self.provider = LLMProvider.objects.create(
            name='Readiness provider',
            adapter_type=LLMProvider.AdapterType.OPENAI,
            api_key_environment_variable='READINESS_PROVIDER_KEY',
        )
        self.model = LLMModel.objects.create(provider=self.provider, identifier='readiness-model')
        settings = GenerationSettings.get_solo()
        settings.default_provider = self.provider
        settings.default_model = self.model
        settings.save()

    def command(self):
        return Command()

    def test_configuration_failure_paths_are_actionable_and_secret_safe(self):
        with self.subTest('debug'):
            with override_settings(DEBUG=True):
                self.assertIn('DJANGO_DEBUG=False', self.command()._check_debug())
        with self.subTest('weak secret'):
            with override_settings(SECRET_KEY='django-insecure-change-me'):
                self.assertIn('strong production', self.command()._check_secret_key())
        with self.subTest('allowed hosts'):
            with override_settings(ALLOWED_HOSTS=['*']):
                self.assertIn('DJANGO_ALLOWED_HOSTS', self.command()._check_allowed_hosts())
        with self.subTest('csrf origins'):
            with override_settings(CSRF_TRUSTED_ORIGINS=['http://example.test']):
                self.assertIn('HTTPS origins', self.command()._check_csrf_origins())

    @patch('generation.management.commands.production_readiness.connections')
    def test_database_failure_is_reported_without_connection_details(self, connections):
        connections.__getitem__.side_effect = RuntimeError('postgres://secret-user:secret-password@host')

        result = self.command()._check_database()

        self.assertEqual(
            result,
            'Database connectivity check failed. Verify DATABASE_URL, network access, and migrations.',
        )
        self.assertNotIn('secret-password', result)

    @patch('generation.management.commands.production_readiness.Redis.from_url')
    def test_broker_and_result_backend_failures_are_reported_without_urls(self, from_url):
        from_url.side_effect = RuntimeError('redis://:secret@host')
        command = self.command()

        broker = command._check_broker()
        backend = command._check_result_backend()

        self.assertIn('Celery broker connectivity check failed', broker)
        self.assertIn('Celery result backend connectivity check failed', backend)
        self.assertNotIn('secret@host', broker + backend)

    def test_default_provider_model_and_key_failure_paths(self):
        GenerationSettings.objects.filter(pk=1).delete()
        self.assertIn('enabled default LLM provider and model', self.command()._check_generation_defaults())

        settings = GenerationSettings.get_solo()
        settings.default_provider = self.provider
        settings.default_model = self.model
        settings.save()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                self.command()._check_generation_defaults(),
                'The default provider API key environment variable is not configured.',
            )

    @patch('generation.management.commands.production_readiness.tempfile.NamedTemporaryFile')
    def test_private_export_storage_failure_is_reported(self, named_temporary_file):
        named_temporary_file.side_effect = OSError('permission denied')
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            result = self.command()._check_private_export_storage()

        self.assertEqual(result, 'Private export storage is not writable. Verify MEDIA_ROOT and storage permissions.')

    def test_static_manifest_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as static_root, override_settings(STATIC_ROOT=Path(static_root)):
            self.assertEqual(
                self.command()._check_static_manifest(),
                'Static manifest is unavailable. Run python manage.py collectstatic --noinput for this release.',
            )

    @patch('generation.management.commands.production_readiness.Redis.from_url')
    @patch('generation.management.commands.production_readiness.connections')
    def test_successful_command_uses_connectivity_checks_without_provider_calls(self, connections, from_url):
        connection = MagicMock()
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        connections.__getitem__.return_value = connection
        connection.cursor.return_value = cursor
        from_url.return_value.ping.return_value = True
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {'READINESS_PROVIDER_KEY': 'not-printed-secret'}, clear=False
        ), override_settings(
            DEBUG=False,
            SECRET_KEY='x' * 60,
            ALLOWED_HOSTS=['courses.example.test'],
            CSRF_TRUSTED_ORIGINS=['https://courses.example.test'],
            MEDIA_ROOT=Path(root) / 'media',
            STATIC_ROOT=Path(root) / 'staticfiles',
        ):
            static_root = Path(root) / 'staticfiles'
            static_root.mkdir()
            (static_root / 'staticfiles.json').write_text('{}', encoding='utf-8')
            stdout = io.StringIO()
            call_command('production_readiness', stdout=stdout)

        self.assertIn('Production readiness checks passed', stdout.getvalue())
        self.assertNotIn('not-printed-secret', stdout.getvalue())
        self.assertEqual(from_url.return_value.ping.call_count, 2)

    def test_command_raises_actionable_failure(self):
        with patch.object(Command, '_checks', return_value=(lambda: 'Mocked safe failure.',)):
            with self.assertRaisesRegex(CommandError, 'Mocked safe failure'):
                call_command('production_readiness')
