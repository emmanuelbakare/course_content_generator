import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from courses.services import LessonSpec, SectionSpec, create_curriculum_revision, create_draft_course

from .adapters import GenerationResponse
from .models import GenerationJob, GenerationSettings, LLMModel, LLMProvider
from .services import (
    RetryableGenerationError,
    enqueue_curriculum_job,
    process_generation_job,
    request_job_cancellation,
)
from .tasks import run_generation_job


class FakeAdapter:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return GenerationResponse(text=self.responses.pop(0), input_tokens=10, output_tokens=20)


class GenerationOrchestrationTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('owner', password='safe-password')
        self.course = create_draft_course(
            self.owner,
            title='Python foundations',
            topic='Learn Python fundamentals.',
            desired_duration_minutes=60,
        )
        self.provider = LLMProvider.objects.create(
            name='Test provider',
            adapter_type=LLMProvider.AdapterType.OPENAI,
            api_key_environment_variable='TEST_PROVIDER_API_KEY',
        )
        self.model = LLMModel.objects.create(provider=self.provider, identifier='test-model')
        settings = GenerationSettings.get_solo()
        settings.default_provider = self.provider
        settings.default_model = self.model
        settings.max_continuations = 1
        settings.max_retries = 0
        settings.save()

    def create_job(self, *, job_type=GenerationJob.JobType.CURRICULUM, lesson=None):
        return GenerationJob.objects.create(
            course=self.course,
            lesson=lesson,
            provider=self.provider,
            llm_model=self.model,
            job_type=job_type,
        )

    def test_enqueue_creates_a_job_and_calls_the_celery_boundary(self):
        with patch('generation.tasks.run_generation_job.delay') as delay:
            job = enqueue_curriculum_job(self.course.pk, revision_instruction='Make it practical.')

        self.assertEqual(job.status, GenerationJob.Status.QUEUED)
        self.assertEqual(job.input_metadata['revision_instruction'], 'Make it practical.')
        delay.assert_called_once_with(job.pk)

    def test_curriculum_job_validates_and_persists_a_draft_revision(self):
        job = self.create_job()
        response = json.dumps(
            {
                'course_description': 'A practical Python course.',
                'suggested_duration_minutes': 60,
                'sections': [
                    {
                        'title': 'Foundations',
                        'duration_minutes': 60,
                        'learning_outcomes': ['Use variables.'],
                        'lessons': [
                            {'title': 'Variables', 'duration_minutes': 30, 'objectives': ['Create variables.'], 'outline': 'Explain values.'},
                            {'title': 'Practice', 'duration_minutes': 30, 'objectives': ['Use variables.'], 'outline': 'Practice.'},
                        ],
                    }
                ],
            }
        )
        adapter = FakeAdapter([response])

        result = process_generation_job(job.pk, adapter_factory=lambda provider: adapter)

        self.assertEqual(result.status, GenerationJob.Status.SUCCEEDED)
        self.assertIsNotNone(result.curriculum_version)
        self.assertEqual(result.curriculum_version.status, 'draft')
        self.assertEqual(result.attempts.count(), 1)
        self.assertEqual(adapter.requests[0].model, 'test-model')
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, 'ready_for_review')

    def test_invalid_responses_use_bounded_continuations_then_need_review(self):
        job = self.create_job()
        adapter = FakeAdapter(['not json', 'also not json'])

        result = process_generation_job(job.pk, adapter_factory=lambda provider: adapter)

        self.assertEqual(result.status, GenerationJob.Status.NEEDS_REVIEW)
        self.assertEqual(result.attempts.count(), 2)
        self.assertEqual(len(adapter.requests), 2)

    def test_lesson_job_creates_a_lesson_revision(self):
        curriculum = create_curriculum_revision(
            self.course,
            sections=[
                SectionSpec(
                    title='Foundations',
                    duration_minutes=60,
                    lessons=[LessonSpec(title='Variables', duration_minutes=60)],
                )
            ],
        )
        lesson = curriculum.sections.get().lessons.get()
        job = self.create_job(job_type=GenerationJob.JobType.LESSON, lesson=lesson)
        adapter = FakeAdapter([json.dumps({'content_markdown': '# Variables\nTeach variables.', 'metadata': {'format': 'markdown'}})])

        result = process_generation_job(job.pk, adapter_factory=lambda provider: adapter)

        lesson.refresh_from_db()
        self.assertEqual(result.status, GenerationJob.Status.SUCCEEDED)
        self.assertEqual(lesson.status, 'ready')
        self.assertEqual(lesson.revisions.get().content_markdown, '# Variables\nTeach variables.')

    def test_cancellation_stops_work_before_a_provider_call(self):
        job = self.create_job()
        request_job_cancellation(job)
        adapter = FakeAdapter(['{}'])

        result = process_generation_job(job.pk, adapter_factory=lambda provider: adapter)

        self.assertEqual(result.status, GenerationJob.Status.CANCELLED)
        self.assertEqual(adapter.requests, [])

    def test_provider_failure_is_marked_failed_after_retry_budget_is_exhausted(self):
        job = self.create_job()
        with patch('generation.tasks.process_generation_job', side_effect=RetryableGenerationError('temporary provider error')):
            task_result = run_generation_job.apply(args=[job.pk])

        job.refresh_from_db()
        self.assertTrue(task_result.successful())
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertEqual(job.error_code, 'provider_request_error')

    def test_owner_can_read_json_job_status_but_other_users_cannot(self):
        job = self.create_job()
        self.client.force_login(self.owner)

        response = self.client.get(reverse('generation:job-status', args=[job.public_id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], GenerationJob.Status.QUEUED)
        other = get_user_model().objects.create_user('other', password='safe-password')
        self.client.force_login(other)
        self.assertEqual(
            self.client.get(reverse('generation:job-status', args=[job.public_id])).status_code,
            404,
        )
