from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from .factories import CourseFactory
from .models import Course, CourseSection, CurriculumVersion
from .rendering import render_safe_markdown
from .services import (
    LessonSpec,
    ProjectSpec,
    SectionSpec,
    create_curriculum_revision,
    create_draft_course,
    restore_curriculum_version,
)


class CourseModelTests(TestCase):
    def test_course_has_a_unique_public_uuid(self):
        first = CourseFactory()
        second = CourseFactory()

        self.assertIsNotNone(first.public_id)
        self.assertNotEqual(first.public_id, second.public_id)

    def test_section_and_lesson_positions_must_be_unique_per_parent(self):
        course = CourseFactory()
        curriculum = CurriculumVersion.objects.create(
            course=course,
            created_by=course.owner,
            version_number=1,
        )
        CourseSection.objects.create(
            curriculum_version=curriculum,
            title='First',
            duration_minutes=30,
            position=1,
        )
        with self.assertRaises(IntegrityError):
            CourseSection.objects.create(
                curriculum_version=curriculum,
                title='Duplicate',
                duration_minutes=30,
                position=1,
            )

    def test_course_validates_and_displays_a_learning_progression(self):
        course = CourseFactory(
            starting_level=Course.Level.BEGINNER,
            target_completion_level=Course.Level.ADVANCED,
        )

        course.full_clean()

        self.assertEqual(course.level, Course.Level.MIXED)
        self.assertEqual(course.level_progression_display, 'Beginner → Advanced')
        course.target_completion_level = Course.Level.BEGINNER
        course.starting_level = Course.Level.ADVANCED
        with self.assertRaises(ValidationError):
            course.full_clean()


class MarkdownRenderingTests(TestCase):
    def test_explicit_mermaid_fence_has_an_escaped_source_fallback(self):
        rendered = str(
            render_safe_markdown(
                '```mermaid\nflowchart TD\n  Start --> Finish\n```'
            )
        )

        self.assertIn('class="mermaid-diagram"', rendered)
        self.assertIn('class="mermaid-fallback"', rendered)
        self.assertIn('flowchart TD', rendered)
        self.assertNotIn('<script', rendered)

    def test_unsafe_html_links_and_raw_mermaid_markup_remain_blocked(self):
        rendered = str(
            render_safe_markdown(
                '<script>alert(1)</script>\n'
                '[Bad link](javascript:alert(1))\n'
                '<div class="mermaid-diagram" data-mermaid-source="flowchart TD">forged</div>\n'
                '```javascript\nalert(2)\n```'
            )
        )

        self.assertNotIn('<script', rendered)
        self.assertNotIn('javascript:', rendered)
        self.assertNotIn('class="mermaid-diagram"', rendered)
        self.assertIn('alert(2)', rendered)


class CourseServiceTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username='author', password='safe-password')
        self.sections = [
            SectionSpec(
                title='Foundations',
                duration_minutes=60,
                lessons=[
                    LessonSpec(title='Introduction', duration_minutes=30),
                    LessonSpec(title='Practice', duration_minutes=30),
                ],
            )
        ]

    def test_create_draft_course_validates_and_sets_draft_status(self):
        course = create_draft_course(
            self.owner,
            title='Django fundamentals',
            topic='A practical introduction to Django.',
            learning_outcomes=['Build a Django app'],
            desired_duration_minutes=60,
        )

        self.assertEqual(course.owner, self.owner)
        self.assertEqual(course.status, Course.Status.DRAFT)

    def test_revision_service_creates_an_ordered_immutable_snapshot(self):
        course = create_draft_course(
            self.owner,
            title='Django fundamentals',
            topic='A practical introduction to Django.',
            desired_duration_minutes=60,
        )
        first = create_curriculum_revision(
            course,
            sections=self.sections,
            project=ProjectSpec(title='Build a blog', description='Create a small blog.'),
            approve=True,
        )
        second = create_curriculum_revision(
            course,
            sections=self.sections,
            change_summary='Refined flow',
            approve=True,
        )
        first.refresh_from_db()

        self.assertEqual(first.version_number, 1)
        self.assertEqual(first.status, CurriculumVersion.Status.SUPERSEDED)
        self.assertEqual(second.version_number, 2)
        self.assertEqual(second.source_version, first)
        self.assertEqual(list(first.sections.values_list('position', flat=True)), [1])
        self.assertEqual(list(first.sections.first().lessons.values_list('position', flat=True)), [1, 2])
        self.assertEqual(first.course_project.title, 'Build a blog')
        self.assertEqual(first.calculated_duration_minutes, 60)
        self.assertEqual(first.suggested_duration_minutes, 60)

    def test_revision_without_requested_duration_persists_the_estimate_and_total(self):
        course = create_draft_course(
            self.owner,
            title='Flexible course',
            topic='A flexible course plan.',
            learning_outcomes=['Apply the material.'],
            prerequisites='None.',
        )

        curriculum = create_curriculum_revision(
            course,
            sections=self.sections,
            duration_estimate_explanation='Two lessons require one hour.',
        )

        self.assertEqual(curriculum.suggested_duration_minutes, 60)
        self.assertEqual(curriculum.calculated_duration_minutes, 60)
        self.assertEqual(curriculum.overall_learning_outcomes, ['Apply the material.'])
        self.assertEqual(curriculum.prerequisites, 'None.')

    def test_revision_service_rejects_duration_mismatch(self):
        course = create_draft_course(
            self.owner,
            title='Django fundamentals',
            topic='A practical introduction to Django.',
            desired_duration_minutes=90,
        )

        with self.assertRaises(ValidationError):
            create_curriculum_revision(course, sections=self.sections)

    def test_restore_creates_a_new_draft_without_mutating_the_source(self):
        course = create_draft_course(
            self.owner,
            title='Django fundamentals',
            topic='A practical introduction to Django.',
            desired_duration_minutes=60,
        )
        source = create_curriculum_revision(
            course,
            sections=self.sections,
            project=ProjectSpec(title='Build a blog', description='Create a small blog.'),
            approve=True,
        )
        replacement = create_curriculum_revision(course, sections=self.sections, approve=True)
        source.refresh_from_db()

        restored = restore_curriculum_version(source, restored_by=self.owner)

        source.refresh_from_db()
        self.assertEqual(source.status, CurriculumVersion.Status.SUPERSEDED)
        self.assertEqual(replacement.status, CurriculumVersion.Status.APPROVED)
        self.assertEqual(restored.status, CurriculumVersion.Status.DRAFT)
        self.assertEqual(restored.source_version, source)
        self.assertEqual(restored.change_summary, 'Restored from curriculum version 1.')
        self.assertEqual(restored.sections.get().lessons.count(), 2)
        self.assertEqual(restored.course_project.title, 'Build a blog')
