import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from kombu.exceptions import OperationalError as KombuOperationalError

from courses.services import (
    LessonSpec,
    SectionSpec,
    create_curriculum_revision,
    create_draft_course,
)

from .adapters import GenerationResponse
from .models import GenerationAttempt, GenerationJob, GenerationSettings, LLMModel, LLMProvider
from .schemas import CurriculumOutput
from .services import (
    GenerationDispatchError,
    ResponseValidationError,
    RetryableGenerationError,
    _curriculum_request,
    _parse_response,
    _start_attempt,
    enqueue_curriculum_job,
    process_generation_job,
    recover_stale_generation_jobs,
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

    def lesson_response(self, *, duration=60):
        return {
            'objectives': ['Create and update Python variables.'],
            'expected_duration_minutes': duration,
            'preparation': ['Open a Python interpreter before learners arrive.'],
            'materials': ['Python 3.12', 'A shared code example'],
            'timed_teaching_flow': [
                {
                    'title': 'Demonstrate assignment',
                    'description': 'Model assigning a value and printing it.',
                    'duration_minutes': 20,
                },
                {
                    'title': 'Guided practice',
                    'description': 'Learners update variables with a partner.',
                    'duration_minutes': duration - 20,
                },
            ],
            'concepts_explanations': [
                {'title': 'Variables', 'description': 'A variable names a value for later use.'},
            ],
            'examples': [
                {'title': 'Greeting', 'description': "Use `name = 'Ada'` and print the value."},
            ],
            'activities': [
                {
                    'title': 'Rename the greeting',
                    'description': 'Change the stored name and run the code.',
                    'expected_output': 'The updated name appears in the console.',
                },
            ],
            'assessment': {
                'check_for_understanding': 'Ask learners to explain what a variable stores.',
                'expected_answers_or_rubric': ['It stores a named reference to a value.'],
            },
            'common_misconceptions': [
                {
                    'title': 'Variables are boxes',
                    'description': 'Clarify that the box metaphor is useful but simplified.',
                },
            ],
            'project_linkage': {
                'project_title': 'Learning journal',
                'connection': 'Use variables to store one journal entry.',
            },
        }

    def test_enqueue_creates_a_job_and_calls_the_celery_boundary(self):
        with patch('generation.tasks.run_generation_job.delay') as delay:
            job = enqueue_curriculum_job(self.course.pk, revision_instruction='Make it practical.')

        self.assertEqual(job.status, GenerationJob.Status.QUEUED)
        self.assertEqual(job.input_metadata['revision_instruction'], 'Make it practical.')
        self.assertEqual(job.provider, self.provider)
        self.assertEqual(job.llm_model, self.model)
        delay.assert_called_once_with(job.pk)

    def test_revision_job_includes_its_source_draft_in_the_prompt_and_keeps_lineage(self):
        source = create_curriculum_revision(
            self.course,
            sections=[
                SectionSpec(
                    title='Original foundations',
                    duration_minutes=60,
                    lessons=[LessonSpec(title='Original variables', duration_minutes=60)],
                )
            ],
        )
        job = self.create_job()
        job.input_metadata = {
            'revision_instruction': 'Add more practice.',
            'source_curriculum_version_id': source.pk,
        }
        job.save(update_fields=('input_metadata',))
        response = json.dumps({
            'course_description': 'A practical Python course.',
            'overall_learning_outcomes': ['Use Python variables.'],
            'prerequisites': 'None.',
            'suggested_duration_minutes': 60,
            'duration_estimate_explanation': 'One planned hour.',
            'sections': [{'title': 'Revised foundations', 'duration_minutes': 60, 'summary': '', 'learning_outcomes': [], 'lessons': [{'title': 'Practice variables', 'duration_minutes': 60, 'objectives': [], 'outline': ''}]}],
            'project': None,
        })
        adapter = FakeAdapter([response])

        result = process_generation_job(job.pk, adapter_factory=lambda provider: adapter)

        self.assertIn('Original foundations', adapter.requests[0].prompt)
        self.assertIn('Add more practice.', adapter.requests[0].prompt)
        self.assertEqual(result.curriculum_version.source_version_id, source.pk)

    def test_unavailable_broker_marks_the_job_failed_without_losing_the_course(self):
        with patch(
            'generation.tasks.run_generation_job.delay',
            side_effect=KombuOperationalError('Redis is unavailable'),
        ):
            with self.assertRaises(GenerationDispatchError):
                enqueue_curriculum_job(self.course.pk)

        job = GenerationJob.objects.get(course=self.course)
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertEqual(job.error_code, 'broker_unavailable')
        self.assertNotIn('Redis is unavailable', job.error_message)

    def test_curriculum_prompt_instructs_the_model_to_follow_the_learning_progression(self):
        self.course.starting_level = self.course.Level.BEGINNER
        self.course.target_completion_level = self.course.Level.ADVANCED
        self.course.full_clean()
        self.course.save()

        request = _curriculum_request(self.course, '')

        self.assertIn('Learning progression: Beginner → Advanced', request.prompt)
        self.assertIn('Starting level: Beginner', request.prompt)
        self.assertIn('Target completion level: Advanced', request.prompt)
        self.assertIn('Sequence the sections and lessons from the starting level', request.prompt)

    def test_curriculum_job_uses_json_prompt_output_that_can_be_continued(self):
        job = self.create_job()
        adapter = FakeAdapter([json.dumps({
            'course_description': 'A practical Python course.',
            'overall_learning_outcomes': ['Use Python variables.'],
            'prerequisites': 'None.',
            'suggested_duration_minutes': 60,
            'duration_estimate_explanation': 'One planned hour.',
            'sections': [{'title': 'Foundations', 'duration_minutes': 60, 'summary': '', 'learning_outcomes': ['Use variables.'], 'lessons': [{'title': 'Variables', 'duration_minutes': 60, 'objectives': ['Create variables.'], 'outline': 'Explain values.'}]}],
            'project': None,
        })])

        result = process_generation_job(job.pk, adapter_factory=lambda provider: adapter)

        self.assertEqual(result.status, GenerationJob.Status.SUCCEEDED)
        request = adapter.requests[0]
        self.assertIsNone(request.json_schema)
        self.assertIn('Return exactly one JSON object', request.prompt)

    def test_curriculum_accepts_string_lists_for_textual_brief_fields(self):
        parsed = _parse_response(
            json.dumps(
                {
                    'course_description': ['A practical course.'],
                    'overall_learning_outcomes': ['Use Python variables.'],
                    'prerequisites': ['A computer.', 'No prior coding required.'],
                    'suggested_duration_minutes': 60,
                    'duration_estimate_explanation': ['Two short activities.', 'One review.'],
                    'sections': [{'title': 'Foundations', 'duration_minutes': 60, 'lessons': [{'title': 'Variables', 'duration_minutes': 60}]}],
                }
            ),
            CurriculumOutput,
        )

        self.assertEqual(parsed.prerequisites, 'A computer.\nNo prior coding required.')

    def test_schema_error_identifies_the_invalid_field_without_storing_content(self):
        with self.assertRaisesMessage(ResponseValidationError, 'field "suggested_duration_minutes"'):
            _parse_response(
                json.dumps(
                    {
                        'course_description': 'A course.',
                        'overall_learning_outcomes': ['Use Python variables.'],
                        'prerequisites': 'None.',
                        'suggested_duration_minutes': 'one hour',
                        'duration_estimate_explanation': 'A short course.',
                        'sections': [{'title': 'Foundations', 'duration_minutes': 60, 'lessons': [{'title': 'Variables', 'duration_minutes': 60}]}],
                    }
                ),
                CurriculumOutput,
            )

    def test_curriculum_job_validates_and_persists_a_draft_revision(self):
        job = self.create_job()
        response = json.dumps(
            {
                'course_description': 'A practical Python course.',
                'overall_learning_outcomes': ['Use Python variables.'],
                'prerequisites': 'None.',
                'suggested_duration_minutes': 60,
                'duration_estimate_explanation': 'Two 30-minute lessons fill the requested hour.',
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
        self.assertEqual(result.curriculum_version.calculated_duration_minutes, 60)
        self.assertEqual(result.curriculum_version.overall_learning_outcomes, ['Use Python variables.'])
        self.assertEqual(result.attempts.count(), 1)
        self.assertEqual(adapter.requests[0].model, 'test-model')
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, 'ready_for_review')

    def test_curriculum_generation_uses_one_primary_call_and_one_repair_at_most(self):
        job = self.create_job()
        adapter = FakeAdapter(['not json', 'still not json', 'unused third response'])

        result = process_generation_job(job.pk, adapter_factory=lambda provider: adapter)

        self.assertEqual(result.status, GenerationJob.Status.NEEDS_REVIEW)
        self.assertEqual(result.attempts.count(), 2)
        self.assertEqual(len(adapter.requests), 2)

    def test_curriculum_generation_continues_truncated_json_until_complete(self):
        job = self.create_job()
        adapter = FakeAdapter([
            '{"course_description":"A practical course.","overall_learning_outcomes":["Use Python variables."],',
            '"prerequisites":"None.","suggested_duration_minutes":60,"duration_estimate_explanation":"One planned hour.","sections":[{"title":"Foundations","duration_minutes":60,"summary":"","learning_outcomes":[],"lessons":[{"title":"Variables","duration_minutes":60,"objectives":[],"outline":"Explain values."}]}],"project":null}',
        ])

        result = process_generation_job(job.pk, adapter_factory=lambda provider: adapter)

        self.assertEqual(result.status, GenerationJob.Status.SUCCEEDED)
        self.assertEqual(len(adapter.requests), 2)
        self.assertIn('Continue from its final character', adapter.requests[1].prompt)

    def test_curriculum_generation_stops_at_its_configured_response_limit(self):
        job = self.create_job()
        job.input_metadata = {'curriculum_response_limit': 1}
        job.save(update_fields=('input_metadata',))
        adapter = FakeAdapter([
            '{"course_description":"A practical course.","overall_learning_outcomes":["Use Python variables."],',
        ])

        result = process_generation_job(job.pk, adapter_factory=lambda provider: adapter)

        self.assertEqual(result.status, GenerationJob.Status.NEEDS_REVIEW)
        self.assertIn('configured limit of 1 provider responses', result.error_message)
        self.assertEqual(len(adapter.requests), 1)

    def test_incomplete_lesson_response_is_continued_then_needs_review(self):
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
        incomplete = self.lesson_response()
        incomplete.pop('assessment')
        adapter = FakeAdapter([json.dumps(incomplete), json.dumps(incomplete)])

        result = process_generation_job(job.pk, adapter_factory=lambda provider: adapter)

        self.assertEqual(result.status, GenerationJob.Status.NEEDS_REVIEW)
        self.assertEqual(result.attempts.count(), 2)
        self.assertEqual(len(adapter.requests), 2)
        self.assertIn('previous response was incomplete or invalid', adapter.requests[1].prompt)

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
        adapter = FakeAdapter([json.dumps(self.lesson_response())])

        result = process_generation_job(job.pk, adapter_factory=lambda provider: adapter)

        lesson.refresh_from_db()
        self.assertEqual(result.status, GenerationJob.Status.SUCCEEDED)
        self.assertEqual(lesson.status, 'ready')
        revision = lesson.revisions.get()
        self.assertIn('## Timed teaching flow', revision.content_markdown)
        self.assertIn('## Assessment / check for understanding', revision.content_markdown)
        self.assertEqual(revision.metadata['generation_schema_version'], 'lesson-v2')
        self.assertEqual(revision.metadata['lesson_plan']['expected_duration_minutes'], 60)
        self.assertGreater(revision.metadata['estimated_content_duration_minutes'], 0)

    def test_incomplete_lesson_response_can_be_repaired_by_a_continuation(self):
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
        incomplete = self.lesson_response()
        incomplete['timed_teaching_flow'][1]['duration_minutes'] = 35
        adapter = FakeAdapter([json.dumps(incomplete), json.dumps(self.lesson_response())])

        result = process_generation_job(job.pk, adapter_factory=lambda provider: adapter)

        self.assertEqual(result.status, GenerationJob.Status.SUCCEEDED)
        self.assertEqual(result.attempts.count(), 2)
        self.assertEqual(lesson.revisions.count(), 1)

    def test_cancellation_stops_work_before_a_provider_call(self):
        job = self.create_job()
        request_job_cancellation(job)
        adapter = FakeAdapter(['{}'])

        result = process_generation_job(job.pk, adapter_factory=lambda provider: adapter)

        self.assertEqual(result.status, GenerationJob.Status.CANCELLED)
        self.assertEqual(adapter.requests, [])

    def test_queued_cancellation_is_terminal_immediately_and_stale_task_is_discarded(self):
        job = self.create_job()
        job_id = job.pk

        request_job_cancellation(job)
        job.refresh_from_db()
        task_result = run_generation_job.apply(args=[job_id])

        self.assertEqual(job.status, GenerationJob.Status.CANCELLED)
        self.assertEqual(job.error_code, 'cancelled')
        self.assertTrue(task_result.successful())
        self.assertEqual(task_result.result['status'], GenerationJob.Status.CANCELLED)

        job.delete()
        stale_task_result = run_generation_job.apply(args=[job_id])
        self.assertTrue(stale_task_result.successful())
        self.assertEqual(stale_task_result.result['status'], 'discarded')

    def test_provider_failure_is_marked_failed_after_retry_budget_is_exhausted(self):
        job = self.create_job()
        with patch('generation.tasks.process_generation_job', side_effect=RetryableGenerationError('temporary provider error')):
            task_result = run_generation_job.apply(args=[job.pk])

        job.refresh_from_db()
        self.assertTrue(task_result.successful())
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertEqual(job.error_code, 'provider_request_error')

    def test_unexpected_orchestration_error_marks_job_failed(self):
        job = self.create_job()
        with patch('generation.tasks.process_generation_job', side_effect=IntegrityError('missing preview')):
            task_result = run_generation_job.apply(args=[job.pk])

        job.refresh_from_db()
        self.assertTrue(task_result.successful())
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertEqual(job.error_code, 'generation_internal_error')
        self.assertNotIn('missing preview', job.error_message)

    def test_attempt_creation_always_initializes_response_preview(self):
        job = self.create_job()

        attempt = _start_attempt(job, 'curriculum-v1', 0)

        self.assertEqual(attempt.response_preview, '')
        self.assertEqual(GenerationAttempt.objects.get(pk=attempt.pk).response_preview, '')

    def test_stale_cancelled_running_job_is_finalized(self):
        job = self.create_job()
        now = timezone.now()
        GenerationJob.objects.filter(pk=job.pk).update(
            status=GenerationJob.Status.RUNNING,
            started_at=now - timedelta(seconds=1_000),
            cancellation_requested_at=now - timedelta(seconds=900),
        )

        recovered = recover_stale_generation_jobs(now=now)

        job.refresh_from_db()
        self.assertEqual(recovered, 1)
        self.assertEqual(job.status, GenerationJob.Status.CANCELLED)
        self.assertEqual(job.error_code, 'cancelled')

    def test_stale_running_job_without_cancellation_fails_for_retry(self):
        job = self.create_job()
        now = timezone.now()
        GenerationJob.objects.filter(pk=job.pk).update(
            status=GenerationJob.Status.RUNNING,
            started_at=now - timedelta(seconds=1_000),
        )

        recover_stale_generation_jobs(now=now)

        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertEqual(job.error_code, 'generation_timeout')

    def test_owner_can_read_json_job_status_but_other_users_cannot(self):
        job = self.create_job()
        attempt = job.attempts.create(
            provider=self.provider,
            llm_model=self.model,
            attempt_number=1,
            prompt_template_version='curriculum-v1',
            response_preview='<script>alert(1)</script>Draft outline',
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse('generation:job-status', args=[job.public_id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], GenerationJob.Status.QUEUED)
        self.assertEqual(response.json()['latest_attempt']['response_preview'], attempt.response_preview)
        other = get_user_model().objects.create_user('other', password='safe-password')
        self.client.force_login(other)
        self.assertEqual(
            self.client.get(reverse('generation:job-status', args=[job.public_id])).status_code,
            404,
        )

    def test_htmx_job_status_request_returns_the_reusable_component(self):
        job = self.create_job()
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse('generation:job-status', args=[job.public_id]),
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'job-status-{job.public_id}')
        self.assertContains(response, 'Curriculum generation')

    def test_htmx_curriculum_status_escapes_the_owner_visible_response_preview(self):
        job = self.create_job()
        job.attempts.create(
            provider=self.provider,
            llm_model=self.model,
            attempt_number=1,
            prompt_template_version='curriculum-v1',
            response_preview='<script>alert(1)</script>Draft outline',
        )
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse('generation:job-status', args=[job.public_id]),
            HTTP_HX_REQUEST='true',
        )

        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;Draft outline')
        self.assertNotContains(response, '<script>alert(1)</script>', html=True)

    def test_htmx_curriculum_status_formats_a_valid_draft_for_review(self):
        job = self.create_job()
        job.input_metadata = {'curriculum_response_limit': 4}
        job.save(update_fields=('input_metadata',))
        job.attempts.create(
            provider=self.provider,
            llm_model=self.model,
            attempt_number=1,
            prompt_template_version='curriculum-v1',
            response_preview=json.dumps(
                {
                    'course_description': 'A practical Python course.',
                    'overall_learning_outcomes': ['Use Python variables.'],
                    'prerequisites': 'None.',
                    'suggested_duration_minutes': 60,
                    'duration_estimate_explanation': 'One planned hour.',
                    'sections': [{'title': 'Foundations', 'duration_minutes': 60, 'summary': 'Start here.', 'learning_outcomes': ['Use variables.'], 'lessons': [{'title': 'Variables', 'duration_minutes': 60, 'objectives': ['Create variables.'], 'outline': 'Explain values.'}]}],
                    'project': None,
                }
            ),
        )
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse('generation:job-status', args=[job.public_id]),
            HTTP_HX_REQUEST='true',
        )

        self.assertContains(response, 'Model curriculum draft')
        self.assertContains(response, 'of up to 4')
        self.assertContains(response, 'Foundations')
        self.assertContains(response, 'Variables')
        self.assertNotContains(response, 'raw escaped text')

    @patch('generation.action_views.enqueue_curriculum_job')
    def test_owner_can_cancel_and_retry_curriculum_jobs(self, enqueue):
        job = self.create_job()
        self.client.force_login(self.owner)

        response = self.client.post(reverse('generation:curriculum-cancel', args=[job.public_id]))

        self.assertRedirects(response, reverse('courses:detail', args=[self.course.public_id]))
        job.refresh_from_db()
        self.assertIsNotNone(job.cancellation_requested_at)
        job.status = GenerationJob.Status.FAILED
        job.save(update_fields=('status',))
        response = self.client.post(reverse('generation:curriculum-retry', args=[job.public_id]))

        self.assertRedirects(response, reverse('courses:detail', args=[self.course.public_id]))
        enqueue.assert_called_once_with(self.course.pk, revision_instruction='')
        other = get_user_model().objects.create_user('other-curriculum', password='safe-password')
        self.client.force_login(other)
        self.assertEqual(
            self.client.post(reverse('generation:curriculum-cancel', args=[job.public_id])).status_code,
            404,
        )
