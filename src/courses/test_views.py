import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.client import RequestFactory
from django.urls import reverse
from django.utils import timezone

from exports.models import ExportJob
from generation.models import GenerationJob, LLMModel, LLMProvider
from generation.services import GenerationDispatchError

from .models import Course, CurriculumVersion, Lesson
from .services import (
    LessonSpec,
    ProjectSpec,
    SectionSpec,
    create_curriculum_revision,
    create_draft_course,
)
from .views import CourseListView


class CourseAuthoringWorkflowTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('owner', password='safe-password')
        self.other_user = get_user_model().objects.create_user('other', password='safe-password')
        self.course = create_draft_course(
            self.owner,
            title='Python foundations',
            topic='Learn Python fundamentals.',
            desired_duration_minutes=60,
        )
        self.sections = [
            SectionSpec(
                title='Foundations',
                duration_minutes=60,
                lessons=[LessonSpec(title='Variables', duration_minutes=30), LessonSpec(title='Practice', duration_minutes=30)],
            )
        ]

    def test_course_list_only_shows_owned_courses(self):
        create_draft_course(self.other_user, title='Private course', topic='Hidden')
        self.client.force_login(self.owner)

        response = self.client.get(reverse('courses:list'))

        self.assertContains(response, 'Python foundations')
        self.assertNotContains(response, 'Private course')
        self.assertContains(response, 'Delete course')

    def test_only_the_owner_can_confirm_and_delete_a_course(self):
        delete_url = reverse('courses:delete', args=[self.course.public_id])
        self.client.force_login(self.other_user)

        self.assertEqual(self.client.get(delete_url).status_code, 404)
        self.assertEqual(
            self.client.post(delete_url).status_code,
            404,
        )

        self.client.force_login(self.owner)
        response = self.client.get(delete_url)
        self.assertContains(response, 'Permanently delete')
        self.assertContains(response, self.course.title)

        self.assertContains(response, 'Are you sure you want to permanently delete this course?')
        self.assertNotContains(response, 'confirmation-title')

        response = self.client.post(delete_url)
        self.assertRedirects(response, reverse('courses:list'))
        self.assertFalse(Course.objects.filter(pk=self.course.pk).exists())

    def test_course_delete_is_blocked_while_generation_or_export_work_is_active(self):
        delete_url = reverse('courses:delete', args=[self.course.public_id])
        provider = LLMProvider.objects.create(
            name='Delete test provider',
            adapter_type=LLMProvider.AdapterType.OPENAI,
            api_key_environment_variable='DELETE_TEST_PROVIDER_API_KEY',
        )
        model = LLMModel.objects.create(provider=provider, identifier='delete-test-model')
        GenerationJob.objects.create(
            course=self.course,
            provider=provider,
            llm_model=model,
            job_type=GenerationJob.JobType.CURRICULUM,
        )
        self.client.force_login(self.owner)

        response = self.client.post(delete_url)

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'Background work is still active', status_code=409)
        self.assertTrue(Course.objects.filter(pk=self.course.pk).exists())

        response = self.client.post(delete_url, {'action': 'cancel_active_work'}, follow=True)
        generation_job = GenerationJob.objects.get(course=self.course)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Cancellation requested')
        self.assertIsNotNone(generation_job.cancellation_requested_at)
        self.assertEqual(generation_job.status, GenerationJob.Status.CANCELLED)
        self.assertContains(response, 'Permanently delete course')

        response = self.client.post(delete_url)
        self.assertRedirects(response, reverse('courses:list'))
        self.assertFalse(Course.objects.filter(pk=self.course.pk).exists())

    def test_course_delete_recovers_a_stale_cancelled_running_job(self):
        delete_url = reverse('courses:delete', args=[self.course.public_id])
        provider = LLMProvider.objects.create(
            name='Stale delete provider',
            adapter_type=LLMProvider.AdapterType.OPENAI,
            api_key_environment_variable='STALE_DELETE_TEST_PROVIDER_API_KEY',
        )
        model = LLMModel.objects.create(provider=provider, identifier='stale-delete-test-model')
        job = GenerationJob.objects.create(
            course=self.course,
            provider=provider,
            llm_model=model,
            job_type=GenerationJob.JobType.CURRICULUM,
            status=GenerationJob.Status.RUNNING,
        )
        now = timezone.now()
        GenerationJob.objects.filter(pk=job.pk).update(
            started_at=now - timedelta(seconds=1_000),
            cancellation_requested_at=now - timedelta(seconds=900),
        )
        self.client.force_login(self.owner)

        response = self.client.post(delete_url)

        self.assertRedirects(response, reverse('courses:list'))
        self.assertFalse(Course.objects.filter(pk=self.course.pk).exists())

    def test_course_deletion_cascades_completed_export_records(self):
        curriculum = create_curriculum_revision(self.course, sections=self.sections, approve=True)
        export_job = ExportJob.objects.create(
            course=self.course,
            curriculum_version=curriculum,
            requested_by=self.owner,
            export_format=ExportJob.Format.MARKDOWN,
            status=ExportJob.Status.SUCCEEDED,
        )
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse('courses:delete', args=[self.course.public_id]),
            {},
        )

        self.assertRedirects(response, reverse('courses:list'))
        self.assertFalse(Course.objects.filter(pk=self.course.pk).exists())
        self.assertFalse(ExportJob.objects.filter(pk=export_job.pk).exists())

    def test_course_list_displays_approved_duration_and_lesson_completion(self):
        curriculum = create_curriculum_revision(self.course, sections=self.sections, approve=True)
        lessons = list(curriculum.sections.get().lessons.all())
        Lesson.objects.filter(pk=lessons[0].pk).update(status=Lesson.Status.READY)
        Lesson.objects.filter(pk=lessons[1].pk).update(status=Lesson.Status.APPROVED)
        self.client.force_login(self.owner)

        response = self.client.get(reverse('courses:list'))
        listed_course = response.context['courses'][0]

        self.assertEqual(listed_course.approved_lesson_total, 2)
        self.assertEqual(listed_course.completed_lesson_count, 2)
        self.assertEqual(listed_course.active_duration_minutes, 60)
        self.assertContains(response, 'Active: 60 minutes')
        self.assertContains(response, '2 of 2 lessons ready')
        self.assertContains(response, 'Last updated')

    def test_course_list_handles_empty_and_proposed_curricula(self):
        proposed = create_curriculum_revision(self.course, sections=self.sections)
        empty_course = create_draft_course(self.owner, title='Empty course', topic='Awaiting a plan.')
        self.client.force_login(self.owner)

        response = self.client.get(reverse('courses:list'))
        listed_courses = {course.pk: course for course in response.context['courses']}

        self.assertEqual(listed_courses[self.course.pk].approved_lesson_total, 0)
        self.assertEqual(listed_courses[self.course.pk].completed_lesson_count, 0)
        self.assertEqual(listed_courses[self.course.pk].proposed_duration_minutes, 60)
        self.assertEqual(listed_courses[empty_course.pk].curriculum_version_count, 0)
        self.assertIsNone(listed_courses[empty_course.pk].proposed_duration_minutes)
        self.assertContains(response, 'Proposed: 60 minutes')
        self.assertContains(response, 'Available after curriculum approval')
        self.assertContains(response, 'Empty course')
        self.assertEqual(proposed.status, CurriculumVersion.Status.DRAFT)

    def test_course_list_annotations_do_not_add_queries_per_course(self):
        for number in range(3):
            course = create_draft_course(
                self.owner,
                title=f'Additional course {number}',
                topic='A concise topic.',
            )
            create_curriculum_revision(course, sections=self.sections, approve=True)
        request = RequestFactory().get(reverse('courses:list'))
        request.user = self.owner
        view = CourseListView()
        view.request = request

        with self.assertNumQueries(1):
            courses = list(view.get_queryset())

        self.assertEqual(len(courses), 4)

    @patch('courses.views.enqueue_curriculum_job')
    def test_course_creation_queues_curriculum_through_boundary(self, enqueue):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('courses:create'),
            {
                'title': 'Django foundations',
                'topic': 'Build web applications.',
                'target_audience': 'Developers',
                'starting_level': Course.Level.BEGINNER,
                'target_completion_level': Course.Level.ADVANCED,
                'language': 'en',
                'delivery_mode': Course.DeliveryMode.INSTRUCTOR_LED,
                'desired_duration_minutes': 60,
                'learning_outcomes_text': 'Create a project\nBuild a view',
            },
        )

        course = Course.objects.get(title='Django foundations')
        self.assertRedirects(response, reverse('courses:detail', args=[course.public_id]))
        self.assertEqual(course.owner, self.owner)
        self.assertEqual(course.status, Course.Status.PLANNING)
        self.assertEqual(course.learning_outcomes, ['Create a project', 'Build a view'])
        self.assertEqual(course.starting_level, Course.Level.BEGINNER)
        self.assertEqual(course.target_completion_level, Course.Level.ADVANCED)
        self.assertEqual(course.level_progression_display, 'Beginner → Advanced')
        self.assertEqual(course.level, Course.Level.MIXED)
        enqueue.assert_called_once_with(course.pk)

    @patch('courses.views.enqueue_curriculum_job')
    def test_course_creation_rejects_a_descending_learning_progression(self, enqueue):
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse('courses:create'),
            {
                'title': 'Advanced to beginner course',
                'topic': 'Invalid learning progression.',
                'starting_level': Course.Level.ADVANCED,
                'target_completion_level': Course.Level.BEGINNER,
                'language': 'en',
                'delivery_mode': Course.DeliveryMode.INSTRUCTOR_LED,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'cannot be below the starting level')
        self.assertFalse(Course.objects.filter(title='Advanced to beginner course').exists())
        enqueue.assert_not_called()

    @patch('courses.views.enqueue_curriculum_job', side_effect=GenerationDispatchError('Broker unavailable'))
    def test_course_creation_handles_an_unavailable_generation_queue(self, enqueue):
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse('courses:create'),
            {
                'title': 'Queued later course',
                'topic': 'Create safely when Redis is unavailable.',
                'starting_level': Course.Level.BEGINNER,
                'target_completion_level': Course.Level.BEGINNER,
                'language': 'en',
                'delivery_mode': Course.DeliveryMode.INSTRUCTOR_LED,
            },
            follow=True,
        )

        course = Course.objects.get(title='Queued later course')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'curriculum planning could not be queued')
        self.assertEqual(course.status, Course.Status.PLANNING)
        enqueue.assert_called_once_with(course.pk)

    def test_owner_can_create_reordered_manual_curriculum_revision(self):
        self.client.force_login(self.owner)
        payload = [
            {
                'title': 'Second section first',
                'summary': 'Moved to the front.',
                'learning_outcomes': ['Apply the concept.'],
                'duration_minutes': 60,
                'lessons': [
                    {'title': 'Lesson B', 'objectives': [], 'outline': '', 'duration_minutes': 30},
                    {'title': 'Lesson A', 'objectives': [], 'outline': '', 'duration_minutes': 30},
                ],
            }
        ]
        response = self.client.post(
            reverse('courses:manual-curriculum', args=[self.course.public_id]),
            {
                'sections_json': json.dumps(payload),
                'change_summary': 'Manually reordered',
                'project_json': json.dumps(
                    {
                        'title': 'Build a learning journal',
                        'description': 'Create a small journal application.',
                        'deliverables': ['Repository', 'Readme'],
                        'evaluation_criteria': ['Runs locally'],
                    }
                ),
            },
        )

        curriculum = CurriculumVersion.objects.get(course=self.course)
        self.assertRedirects(
            response,
            reverse('courses:curriculum-review', args=[self.course.public_id, curriculum.public_id]),
        )
        self.assertEqual(curriculum.status, CurriculumVersion.Status.DRAFT)
        section = curriculum.sections.get()
        self.assertEqual(section.title, 'Second section first')
        self.assertEqual(list(section.lessons.values_list('title', flat=True)), ['Lesson B', 'Lesson A'])
        self.assertEqual(curriculum.course_project.title, 'Build a learning journal')
        self.assertEqual(curriculum.course_project.deliverables, ['Repository', 'Readme'])

    def test_manual_revision_prepopulates_the_latest_project(self):
        create_curriculum_revision(
            self.course,
            sections=self.sections,
            project=ProjectSpec(
                title='Build a blog',
                description='Create a small blog application.',
                deliverables=['Repository'],
                evaluation_criteria=['Working posts'],
            ),
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse('courses:manual-curriculum', args=[self.course.public_id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Build a blog')
        self.assertContains(response, 'Working posts')

    def test_owner_can_approve_and_request_revision(self):
        curriculum = create_curriculum_revision(self.course, sections=self.sections)
        self.client.force_login(self.owner)
        review_url = reverse('courses:curriculum-review', args=[self.course.public_id, curriculum.public_id])

        response = self.client.post(review_url, {'action': 'approve'})
        curriculum.refresh_from_db()
        self.course.refresh_from_db()

        self.assertRedirects(response, review_url)
        self.assertEqual(curriculum.status, CurriculumVersion.Status.APPROVED)
        self.assertEqual(self.course.status, Course.Status.APPROVED)

        with patch('courses.views.enqueue_curriculum_job') as enqueue:
            response = self.client.post(
                review_url,
                {'action': 'request_revision', 'revision_instruction': 'Make it more advanced.'},
            )

        self.assertRedirects(response, review_url)
        enqueue.assert_called_once_with(
            self.course.pk,
            revision_instruction='Make it more advanced.',
            source_curriculum_version_id=curriculum.pk,
        )

    def test_instructor_review_checklist_is_reused_across_review_workspace_and_export(self):
        curriculum = create_curriculum_revision(self.course, sections=self.sections, approve=True)
        self.client.force_login(self.owner)

        for url in (
            reverse('courses:curriculum-review', args=[self.course.public_id, curriculum.public_id]),
            reverse('courses:workspace', args=[self.course.public_id]),
            reverse('courses:detail', args=[self.course.public_id]),
        ):
            response = self.client.get(url)
            self.assertContains(response, 'Instructor review checklist')
            self.assertContains(response, 'Verify factual accuracy and code correctness.')
            self.assertContains(response, 'AI-generated content requires your professional review.')

    def test_other_users_cannot_view_or_edit_a_course(self):
        curriculum = create_curriculum_revision(self.course, sections=self.sections)
        self.client.force_login(self.other_user)

        self.assertEqual(self.client.get(reverse('courses:detail', args=[self.course.public_id])).status_code, 404)
        self.assertEqual(
            self.client.get(
                reverse('courses:curriculum-review', args=[self.course.public_id, curriculum.public_id])
            ).status_code,
            404,
        )

    def test_owner_can_compare_versions_and_restore_a_historical_version(self):
        first = create_curriculum_revision(
            self.course,
            sections=self.sections,
            course_description='Original plan',
            approve=True,
        )
        second = create_curriculum_revision(
            self.course,
            sections=[
                SectionSpec(
                    title='Changed foundations',
                    duration_minutes=60,
                    lessons=[LessonSpec(title='Changed lesson', duration_minutes=60)],
                )
            ],
            course_description='Changed plan',
            approve=True,
        )
        self.client.force_login(self.owner)
        compare_url = reverse('courses:curriculum-compare', args=[self.course.public_id])

        response = self.client.get(
            compare_url, {'left': first.public_id, 'right': second.public_id}
        )
        restore_response = self.client.post(
            reverse('courses:curriculum-restore', args=[self.course.public_id, first.public_id])
        )

        restored = CurriculumVersion.objects.get(version_number=3)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Original plan')
        self.assertContains(response, 'Changed plan')
        self.assertRedirects(
            restore_response,
            reverse('courses:curriculum-review', args=[self.course.public_id, restored.public_id]),
        )
        self.assertEqual(restored.status, CurriculumVersion.Status.DRAFT)
        first.refresh_from_db()
        self.assertEqual(first.status, CurriculumVersion.Status.SUPERSEDED)

        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(compare_url).status_code, 404)
        self.assertEqual(
            self.client.post(
                reverse('courses:curriculum-restore', args=[self.course.public_id, first.public_id])
            ).status_code,
            404,
        )
