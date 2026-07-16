import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Course, CurriculumVersion
from .services import LessonSpec, SectionSpec, create_curriculum_revision, create_draft_course


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

    @patch('courses.views.enqueue_curriculum_job')
    def test_course_creation_queues_curriculum_through_boundary(self, enqueue):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('courses:create'),
            {
                'title': 'Django foundations',
                'topic': 'Build web applications.',
                'target_audience': 'Developers',
                'level': Course.Level.BEGINNER,
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
            {'sections_json': json.dumps(payload), 'change_summary': 'Manually reordered'},
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
        enqueue.assert_called_once_with(self.course.pk, revision_instruction='Make it more advanced.')

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
