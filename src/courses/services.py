"""Service-layer APIs for constructing course and curriculum snapshots."""

from dataclasses import dataclass, field
from typing import Sequence

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from .models import Course, CourseProject, CourseSection, CurriculumVersion, Lesson


@dataclass(frozen=True)
class LessonSpec:
    title: str
    duration_minutes: int
    objectives: list[str] = field(default_factory=list)
    outline: str = ''


@dataclass(frozen=True)
class SectionSpec:
    title: str
    duration_minutes: int
    lessons: Sequence[LessonSpec]
    summary: str = ''
    learning_outcomes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProjectSpec:
    title: str
    description: str
    deliverables: list[str] = field(default_factory=list)
    evaluation_criteria: list[str] = field(default_factory=list)


def create_draft_course(owner, *, title: str, topic: str, **attributes) -> Course:
    """Create a validated, user-owned course draft without starting generation."""
    course = Course(owner=owner, title=title, topic=topic, **attributes)
    course.full_clean()
    course.save()
    return course


def _validate_curriculum_spec(course: Course, sections: Sequence[SectionSpec]) -> None:
    if not sections:
        raise ValidationError('A curriculum revision requires at least one section.')

    total_duration = 0
    for section in sections:
        if not section.lessons:
            raise ValidationError(f'Section "{section.title}" requires at least one lesson.')
        lesson_duration = sum(lesson.duration_minutes for lesson in section.lessons)
        if lesson_duration != section.duration_minutes:
            raise ValidationError(
                f'Section "{section.title}" duration must equal the sum of its lessons.'
            )
        total_duration += section.duration_minutes

    if (
        course.desired_duration_minutes is not None
        and total_duration != course.desired_duration_minutes
    ):
        raise ValidationError(
            'Curriculum duration must equal the course desired duration in minutes.'
        )


def create_curriculum_revision(
    course: Course,
    *,
    sections: Sequence[SectionSpec],
    created_by=None,
    course_description: str = '',
    suggested_duration_minutes: int | None = None,
    revision_instruction: str = '',
    change_summary: str = '',
    project: ProjectSpec | None = None,
    approve: bool = False,
) -> CurriculumVersion:
    """Create an immutable curriculum snapshot and its ordered child objects.

    The API never edits prior curriculum versions. Approving this snapshot atomically
    supersedes the former approved version for the course.
    """
    created_by = created_by or course.owner
    if created_by.pk != course.owner_id:
        raise ValidationError('Only the course owner may create a curriculum revision.')
    _validate_curriculum_spec(course, sections)

    try:
        with transaction.atomic():
            locked_course = Course.objects.select_for_update().get(pk=course.pk)
            latest_version = (
                CurriculumVersion.objects.filter(course=locked_course)
                .order_by('-version_number')
                .first()
            )
            version_number = (latest_version.version_number if latest_version else 0) + 1
            if approve:
                CurriculumVersion.objects.filter(
                    course=locked_course,
                    status=CurriculumVersion.Status.APPROVED,
                ).update(status=CurriculumVersion.Status.SUPERSEDED)
            curriculum = CurriculumVersion(
                course=locked_course,
                source_version=latest_version,
                created_by=created_by,
                version_number=version_number,
                status=(CurriculumVersion.Status.APPROVED if approve else CurriculumVersion.Status.DRAFT),
                course_description=course_description,
                suggested_duration_minutes=suggested_duration_minutes,
                revision_instruction=revision_instruction,
                change_summary=change_summary,
            )
            curriculum.full_clean()
            curriculum.save()

            for section_position, section_spec in enumerate(sections, start=1):
                section = CourseSection(
                    curriculum_version=curriculum,
                    title=section_spec.title,
                    summary=section_spec.summary,
                    learning_outcomes=section_spec.learning_outcomes,
                    duration_minutes=section_spec.duration_minutes,
                    position=section_position,
                )
                section.full_clean()
                section.save()
                for lesson_position, lesson_spec in enumerate(section_spec.lessons, start=1):
                    lesson = Lesson(
                        section=section,
                        title=lesson_spec.title,
                        objectives=lesson_spec.objectives,
                        outline=lesson_spec.outline,
                        duration_minutes=lesson_spec.duration_minutes,
                        position=lesson_position,
                    )
                    lesson.full_clean()
                    lesson.save()

            if project:
                course_project = CourseProject(
                    curriculum_version=curriculum,
                    title=project.title,
                    description=project.description,
                    deliverables=project.deliverables,
                    evaluation_criteria=project.evaluation_criteria,
                )
                course_project.full_clean()
                course_project.save()

            if approve:
                Course.objects.filter(pk=locked_course.pk).update(status=Course.Status.APPROVED)

            return curriculum
    except IntegrityError as exc:
        raise ValidationError('Could not create a unique curriculum revision.') from exc
