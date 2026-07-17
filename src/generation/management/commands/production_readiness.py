"""Validate production deployment dependencies without invoking LLM providers."""

import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from redis import Redis

from generation.models import GenerationSettings


class Command(BaseCommand):
    help = 'Validate production configuration and dependencies without sending LLM requests.'

    def handle(self, *args, **options):
        failures = [failure for check in self._checks() if (failure := check())]
        if failures:
            formatted_failures = '\n'.join(f'- {failure}' for failure in failures)
            raise CommandError(f'Production readiness checks failed:\n{formatted_failures}')
        self.stdout.write(self.style.SUCCESS('Production readiness checks passed. No LLM requests were sent.'))

    def _checks(self):
        return (
            self._check_debug,
            self._check_secret_key,
            self._check_allowed_hosts,
            self._check_csrf_origins,
            self._check_database,
            self._check_broker,
            self._check_result_backend,
            self._check_generation_defaults,
            self._check_private_export_storage,
            self._check_static_manifest,
        )

    def _check_debug(self):
        return 'Set DJANGO_DEBUG=False for production.' if settings.DEBUG else None

    def _check_secret_key(self):
        secret_key = settings.SECRET_KEY or ''
        weak_markers = ('django-insecure', 'development-only', 'replace-with', 'change-me')
        if len(secret_key) < 50 or any(marker in secret_key.lower() for marker in weak_markers):
            return 'Configure a strong production DJANGO_SECRET_KEY (at least 50 non-placeholder characters).'
        return None

    def _check_allowed_hosts(self):
        if not settings.ALLOWED_HOSTS or '*' in settings.ALLOWED_HOSTS:
            return 'Configure explicit production DJANGO_ALLOWED_HOSTS; do not use a wildcard.'
        return None

    def _check_csrf_origins(self):
        origins = settings.CSRF_TRUSTED_ORIGINS
        if not origins:
            return 'Configure at least one HTTPS DJANGO_CSRF_TRUSTED_ORIGINS value.'
        if any(urlparse(origin).scheme != 'https' or not urlparse(origin).netloc for origin in origins):
            return 'Use absolute HTTPS origins for DJANGO_CSRF_TRUSTED_ORIGINS.'
        return None

    def _check_database(self):
        try:
            connection = connections['default']
            connection.ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                cursor.fetchone()
        except Exception:
            return 'Database connectivity check failed. Verify DATABASE_URL, network access, and migrations.'
        return None

    def _check_broker(self):
        return self._check_redis(settings.CELERY_BROKER_URL, 'Celery broker')

    def _check_result_backend(self):
        return self._check_redis(settings.CELERY_RESULT_BACKEND, 'Celery result backend')

    def _check_redis(self, url, label):
        try:
            Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2).ping()
        except Exception:
            return f'{label} connectivity check failed. Verify Redis URL, credentials, TLS, and network access.'
        return None

    def _check_generation_defaults(self):
        generation_settings = GenerationSettings.objects.select_related(
            'default_provider', 'default_model'
        ).filter(pk=1).first()
        if not generation_settings or not generation_settings.default_provider_id or not generation_settings.default_model_id:
            return 'Configure an enabled default LLM provider and model in Generation settings.'
        provider = generation_settings.default_provider
        llm_model = generation_settings.default_model
        if not provider.enabled or not llm_model.enabled or llm_model.provider_id != provider.pk:
            return 'The default provider and model must be enabled and belong together.'
        if not os.getenv(provider.api_key_environment_variable, '').strip():
            return 'The default provider API key environment variable is not configured.'
        return None

    def _check_private_export_storage(self):
        try:
            private_root = Path(settings.MEDIA_ROOT) / 'private_exports'
            private_root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=private_root, prefix='.readiness-', delete=True) as probe:
                probe.write(b'check')
                probe.flush()
        except Exception:
            return 'Private export storage is not writable. Verify MEDIA_ROOT and storage permissions.'
        return None

    def _check_static_manifest(self):
        manifest = Path(settings.STATIC_ROOT) / 'staticfiles.json'
        if not manifest.is_file():
            return 'Static manifest is unavailable. Run python manage.py collectstatic --noinput for this release.'
        return None
