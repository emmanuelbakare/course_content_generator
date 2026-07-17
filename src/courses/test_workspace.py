from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from generation.models import GenerationJob, GenerationSettings, LLMModel, LLMProvider

from .services import (
    LessonSpec,
    SectionSpec,
    create_curriculum_revision,
    create_draft_course,
    create_lesson_revision,
)


class CourseWorkspaceTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('owner', password='safe-password')
        self.other_user = get_user_model().objects.create_user('other', password='safe-password')
        self.course = create_draft_course(
            self.owner,
            title='Python foundations',
            topic='Learn Python fundamentals.',
            desired_duration_minutes=60,
        )
        self.curriculum = create_curriculum_revision(
            self.course,
            sections=[
                SectionSpec(
                    title='Foundations',
                    duration_minutes=60,
                    lessons=[
                        LessonSpec(title='Variables', duration_minutes=30),
                        LessonSpec(title='Practice', duration_minutes=30),
                    ],
                )
            ],
            approve=True,
        )
        self.lesson = self.curriculum.sections.get().lessons.get(position=1)
        self.second_lesson = self.curriculum.sections.get().lessons.get(position=2)
        create_lesson_revision(
            self.lesson,
            created_by=self.owner,
            content_markdown='# Variables\n<script>alert("unsafe")</script>\n[Unsafe link](javascript:alert(1))',
            change_summary='Initial draft',
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
        settings.save()
        self.workspace_url = reverse('courses:workspace', args=[self.course.public_id])

    def create_job(self, status=GenerationJob.Status.QUEUED, **kwargs):
        return GenerationJob.objects.create(
            course=self.course,
            lesson=self.lesson,
            provider=self.provider,
            llm_model=self.model,
            job_type=GenerationJob.JobType.LESSON,
            status=status,
            **kwargs,
        )

    def test_workspace_shows_curriculum_and_sanitizes_lesson_markdown(self):
        self.client.force_login(self.owner)

        response = self.client.get(f'{self.workspace_url}?lesson={self.lesson.public_id}')

        self.assertContains(response, 'Foundations')
        self.assertContains(response, 'Variables')
        self.assertContains(response, '<h1>Variables</h1>', html=True)
        self.assertNotContains(response, '<script>')
        self.assertNotIn('javascript:', str(response.context['rendered_content']))
        self.assertContains(response, 'Revision 1')
        self.assertContains(response, 'Generate selected lessons')

    def test_workspace_edit_creates_a_new_lesson_revision(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('courses:lesson-edit', args=[self.course.public_id, self.lesson.public_id]),
            {'content_markdown': '# Updated lesson', 'change_summary': 'Clarified introduction'},
        )

        self.lesson.refresh_from_db()
        self.assertRedirects(response, f'{self.workspace_url}?lesson={self.lesson.public_id}')
        self.assertEqual(self.lesson.revisions.count(), 2)
        self.assertEqual(self.lesson.revisions.first().content_markdown, '# Updated lesson')
        self.assertEqual(self.lesson.revisions.first().change_summary, 'Clarified introduction')

    def test_workspace_and_lesson_edit_are_owner_scoped(self):
        self.client.force_login(self.other_user)

        self.assertEqual(self.client.get(self.workspace_url).status_code, 404)
        self.assertEqual(
            self.client.post(
                reverse('courses:lesson-edit', args=[self.course.public_id, self.lesson.public_id]),
                {'content_markdown': 'Unauthorized'},
            ).status_code,
            404,
        )

    @patch('generation.action_views.enqueue_lesson_job')
    def test_generate_and_retry_actions_enqueue_owned_lesson(self, enqueue):
        self.client.force_login(self.owner)
        generate_url = reverse('generation:lesson-generate', args=[self.lesson.public_id])

        response = self.client.post(generate_url, {'revision_instruction': 'Add another example.'})

        self.assertRedirects(response, f'{self.workspace_url}?lesson={self.lesson.public_id}')
        enqueue.assert_called_once_with(self.lesson.pk, revision_instruction='Add another example.')
        failed_job = self.create_job(
            status=GenerationJob.Status.FAILED,
            input_metadata={'revision_instruction': 'Add another example.'},
        )
        enqueue.reset_mock()

        response = self.client.post(reverse('generation:lesson-retry', args=[failed_job.public_id]))

        self.assertRedirects(response, f'{self.workspace_url}?lesson={self.lesson.public_id}')
        enqueue.assert_called_once_with(self.lesson.pk, revision_instruction='Add another example.')

    def test_cancel_action_records_a_cooperative_cancellation_request(self):
        job = self.create_job()
        self.client.force_login(self.owner)

        response = self.client.post(reverse('generation:lesson-cancel', args=[job.public_id]))

        job.refresh_from_db()
        self.assertRedirects(response, f'{self.workspace_url}?lesson={self.lesson.public_id}')
        self.assertIsNotNone(job.cancellation_requested_at)

    @patch('generation.tasks.run_generation_job.delay')
    def test_batch_generation_queues_selected_lessons_and_skips_active_jobs(self, delay):
        active_job = self.create_job()
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse('generation:lesson-batch-generate', args=[self.course.public_id]),
            {'lesson_ids': [str(self.lesson.public_id), str(self.second_lesson.public_id)]},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Queued generation for 1 lesson(s).')
        self.assertContains(response, 'Skipped 1 lesson(s) with active generation jobs: Variables.')
        self.assertEqual(
            GenerationJob.objects.filter(lesson=self.lesson).count(),
            1,
        )
        self.assertEqual(GenerationJob.objects.filter(lesson=self.second_lesson).count(), 1)
        delay.assert_called_once_with(GenerationJob.objects.get(lesson=self.second_lesson).pk)
        self.assertEqual(active_job.status, GenerationJob.Status.QUEUED)

    def test_revision_history_can_be_viewed_and_restored_as_a_new_revision(self):
        current = create_lesson_revision(
            self.lesson,
            created_by=self.owner,
            content_markdown='# Newer version',
            metadata={'origin': 'manual'},
            change_summary='Newer wording',
        )
        source = self.lesson.revisions.get(revision_number=1)
        detail_url = reverse(
            'courses:lesson-revision-detail',
            args=[self.course.public_id, self.lesson.public_id, source.public_id],
        )
        restore_url = reverse(
            'courses:lesson-revision-restore',
            args=[self.course.public_id, self.lesson.public_id, source.public_id],
        )
        self.client.force_login(self.owner)

        response = self.client.get(detail_url)
        self.assertContains(response, '<h1>Variables</h1>', html=True)
        response = self.client.post(restore_url)

        self.assertRedirects(response, f'{self.workspace_url}?lesson={self.lesson.public_id}')
        self.lesson.refresh_from_db()
        restored = self.lesson.revisions.first()
        self.assertEqual(self.lesson.revisions.count(), 3)
        self.assertEqual(restored.content_markdown, source.content_markdown)
        self.assertEqual(restored.metadata, source.metadata)
        self.assertEqual(restored.change_summary, 'Restored from revision 1.')
        current.refresh_from_db()
        self.assertEqual(current.content_markdown, '# Newer version')

    def test_other_user_cannot_call_lesson_generation_actions(self):
        job = self.create_job(status=GenerationJob.Status.FAILED)
        self.client.force_login(self.other_user)

        self.assertEqual(
            self.client.post(reverse('generation:lesson-generate', args=[self.lesson.public_id])).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse('generation:lesson-retry', args=[job.public_id])).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse('generation:lesson-cancel', args=[job.public_id])).status_code,
            404,
        )
        revision = self.lesson.revisions.first()
        self.assertEqual(
            self.client.post(
                reverse(
                    'generation:lesson-batch-generate',
                    args=[self.course.public_id],
                ),
                {'lesson_ids': [str(self.lesson.public_id)]},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse(
                    'courses:lesson-revision-detail',
                    args=[self.course.public_id, self.lesson.public_id, revision.public_id],
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    'courses:lesson-revision-restore',
                    args=[self.course.public_id, self.lesson.public_id, revision.public_id],
                )
            ).status_code,
            404,
        )
