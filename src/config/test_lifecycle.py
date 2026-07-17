"""Cross-app lifecycle coverage using a provider-neutral fake LLM adapter."""

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from courses.models import Course, CurriculumVersion
from courses.services import approve_curriculum_version, create_draft_course
from exports.models import ExportJob
from exports.services import enqueue_course_export, process_export_job
from generation.adapters import GenerationResponse
from generation.models import GenerationSettings, LLMModel, LLMProvider
from generation.services import enqueue_curriculum_job, enqueue_lesson_job, process_generation_job

from .logging import REDACTED_VALUE, redact_value


class FakeLifecycleAdapter:
    """A deterministic adapter that proves orchestration is provider-neutral."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return GenerationResponse(
            text=next(self.responses), input_tokens=12, output_tokens=24, metadata={'fake': True}
        )


class FullCourseLifecycleTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.media_override = override_settings(MEDIA_ROOT=self.media_root)
        self.media_override.enable()
        self.owner = get_user_model().objects.create_user('owner', password='safe-password')
        self.other_user = get_user_model().objects.create_user('other', password='safe-password')
        self.secret = 'fake-provider-secret-that-must-not-leak'
        self.provider = LLMProvider.objects.create(
            name='Lifecycle fake provider',
            adapter_type=LLMProvider.AdapterType.OPENAI,
            api_key_environment_variable='LIFECYCLE_FAKE_API_KEY',
        )
        self.model = LLMModel.objects.create(provider=self.provider, identifier='fake-course-model')
        settings = GenerationSettings.get_solo()
        settings.default_provider = self.provider
        settings.default_model = self.model
        settings.max_continuations = 0
        settings.save()

    def tearDown(self):
        self.media_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)
        super().tearDown()

    def test_owner_can_generate_approve_generate_lessons_and_export_privately(self):
        course = create_draft_course(
            self.owner,
            title='Lifecycle Python course',
            topic='Build a small Python application.',
            desired_duration_minutes=60,
        )
        curriculum_response = json.dumps(
            {
                'course_description': 'A compact, practical Python course.',
                'suggested_duration_minutes': 60,
                'sections': [
                    {
                        'title': 'Getting started',
                        'duration_minutes': 60,
                        'learning_outcomes': ['Build a Python program.'],
                        'lessons': [
                            {
                                'title': 'First program',
                                'duration_minutes': 60,
                                'objectives': ['Write and run Python.'],
                                'outline': 'Create a small program.',
                            }
                        ],
                    }
                ],
            }
        )
        curriculum_adapter = FakeLifecycleAdapter([curriculum_response])
        with patch('generation.tasks.run_generation_job.delay') as delay:
            curriculum_job = enqueue_curriculum_job(course.pk, revision_instruction='Keep it practical.')
        delay.assert_called_once_with(curriculum_job.pk)

        curriculum_job = process_generation_job(
            curriculum_job.pk, adapter_factory=lambda provider: curriculum_adapter
        )
        self.assertEqual(curriculum_job.status, 'succeeded')
        curriculum = curriculum_job.curriculum_version
        self.assertIsNotNone(curriculum)
        assert curriculum is not None
        self.assertEqual(curriculum.status, CurriculumVersion.Status.DRAFT)
        self.assertNotIn(self.secret, curriculum_adapter.requests[0].prompt)

        approve_curriculum_version(curriculum, approved_by=self.owner)
        course.refresh_from_db()
        self.assertEqual(course.status, Course.Status.APPROVED)
        lesson = curriculum.sections.get().lessons.get()

        lesson_adapter = FakeLifecycleAdapter(
            [json.dumps({'content_markdown': '# First program\n\nWrite a safe example.', 'metadata': {}})]
        )
        with patch('generation.tasks.run_generation_job.delay') as delay:
            lesson_job = enqueue_lesson_job(lesson.pk)
        delay.assert_called_once_with(lesson_job.pk)
        lesson_job = process_generation_job(lesson_job.pk, adapter_factory=lambda provider: lesson_adapter)
        lesson.refresh_from_db()
        self.assertEqual(lesson_job.status, 'succeeded')
        self.assertEqual(lesson.status, 'ready')

        with patch('exports.tasks.run_export_job.delay') as delay:
            export_job = enqueue_course_export(
                course_id=course.pk,
                requested_by=self.owner,
                export_format=ExportJob.Format.MARKDOWN,
            )
        delay.assert_called_once_with(str(export_job.public_id))
        export_job = process_export_job(export_job.public_id)
        self.assertEqual(export_job.status, ExportJob.Status.SUCCEEDED)
        content = Path(export_job.export_file.file.path).read_text(encoding='utf-8')
        self.assertIn('Write a safe example.', content)
        self.assertIsNotNone(export_job.export_file.file.name)
        assert export_job.export_file.file.name is not None
        self.assertTrue(export_job.export_file.file.name.startswith(f'private_exports/{self.owner.pk}/'))

        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(reverse('exports:job-status', args=[export_job.public_id])).status_code, 200
        )
        download = self.client.get(reverse('exports:download', args=[export_job.public_id]))
        self.assertEqual(download.status_code, 200)
        self.assertIn('attachment;', download['Content-Disposition'])

        self.client.force_login(self.other_user)
        self.assertEqual(
            self.client.get(reverse('courses:detail', args=[course.public_id])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse('generation:job-status', args=[lesson_job.public_id])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse('exports:download', args=[export_job.public_id])).status_code, 404
        )
        self.assertEqual(redact_value({'authorization': f'Bearer {self.secret}'})['authorization'], REDACTED_VALUE)
