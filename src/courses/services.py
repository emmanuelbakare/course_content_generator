"""Service-layer APIs for constructing course and curriculum snapshots."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max

from .models import Course, CourseProject, CourseSection, CurriculumVersion, Lesson, LessonRevision


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


def _validate_curriculum_spec(course: Course, sections: Sequence[SectionSpec]) -> int:
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
    return total_duration


def create_curriculum_revision(
    course: Course,
    *,
    sections: Sequence[SectionSpec],
    created_by=None,
    course_description: str = '',
    overall_learning_outcomes: list[str] | None = None,
    prerequisites: str | None = None,
    suggested_duration_minutes: int | None = None,
    duration_estimate_explanation: str = '',
    revision_instruction: str = '',
    change_summary: str = '',
    project: ProjectSpec | None = None,
    source_version: CurriculumVersion | None = None,
    approve: bool = False,
) -> CurriculumVersion:
    """Create an immutable curriculum snapshot and its ordered child objects.

    The API never edits prior curriculum versions. Approving this snapshot atomically
    supersedes the former approved version for the course.
    """
    created_by = created_by or course.owner
    if created_by.pk != course.owner_id:
        raise ValidationError('Only the course owner may create a curriculum revision.')
    calculated_duration_minutes = _validate_curriculum_spec(course, sections)
    overall_learning_outcomes = (
        list(course.learning_outcomes)
        if overall_learning_outcomes is None
        else overall_learning_outcomes
    )
    prerequisites = course.prerequisites if prerequisites is None else prerequisites
    if course.desired_duration_minutes is None:
        suggested_duration_minutes = suggested_duration_minutes or calculated_duration_minutes
        if suggested_duration_minutes != calculated_duration_minutes:
            raise ValidationError('The proposed duration must equal the calculated lesson total.')
    elif suggested_duration_minutes is None:
        suggested_duration_minutes = calculated_duration_minutes
    duration_estimate_explanation = duration_estimate_explanation or (
        f'The curriculum allocates {calculated_duration_minutes} minutes across its lessons.'
    )

    try:
        with transaction.atomic():
            locked_course = Course.objects.select_for_update().get(pk=course.pk)
            latest_version = (
                CurriculumVersion.objects.filter(course=locked_course)
                .order_by('-version_number')
                .first()
            )
            if source_version is not None and source_version.course_id != locked_course.pk:
                raise ValidationError('The source curriculum version must belong to the course.')
            version_number = (latest_version.version_number if latest_version else 0) + 1
            if approve:
                CurriculumVersion.objects.filter(
                    course=locked_course,
                    status=CurriculumVersion.Status.APPROVED,
                ).update(status=CurriculumVersion.Status.SUPERSEDED)
            curriculum = CurriculumVersion(
                course=locked_course,
                source_version=source_version or latest_version,
                created_by=created_by,
                version_number=version_number,
                status=(CurriculumVersion.Status.APPROVED if approve else CurriculumVersion.Status.DRAFT),
                course_description=course_description,
                overall_learning_outcomes=overall_learning_outcomes,
                prerequisites=prerequisites,
                suggested_duration_minutes=suggested_duration_minutes,
                calculated_duration_minutes=calculated_duration_minutes,
                duration_estimate_explanation=duration_estimate_explanation,
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


def restore_curriculum_version(source: CurriculumVersion, *, restored_by) -> CurriculumVersion:
    """Clone a historical version into a new draft without changing the source."""
    source = CurriculumVersion.objects.prefetch_related(
        'sections__lessons', 'course_project'
    ).select_related('course').get(pk=source.pk)
    if restored_by.pk != source.course.owner_id:
        raise ValidationError('Only the course owner may restore a curriculum version.')
    sections = [
        SectionSpec(
            title=section.title,
            duration_minutes=section.duration_minutes,
            summary=section.summary,
            learning_outcomes=list(section.learning_outcomes),
            lessons=[
                LessonSpec(
                    title=lesson.title,
                    duration_minutes=lesson.duration_minutes,
                    objectives=list(lesson.objectives),
                    outline=lesson.outline,
                )
                for lesson in section.lessons.all()
            ],
        )
        for section in source.sections.all()
    ]
    project = None
    if hasattr(source, 'course_project'):
        source_project = source.course_project
        project = ProjectSpec(
            title=source_project.title,
            description=source_project.description,
            deliverables=list(source_project.deliverables),
            evaluation_criteria=list(source_project.evaluation_criteria),
        )
    return create_curriculum_revision(
        source.course,
        created_by=restored_by,
        sections=sections,
        course_description=source.course_description,
        overall_learning_outcomes=list(source.overall_learning_outcomes),
        prerequisites=source.prerequisites,
        suggested_duration_minutes=source.suggested_duration_minutes,
        duration_estimate_explanation=source.duration_estimate_explanation,
        revision_instruction=source.revision_instruction,
        change_summary=f'Restored from curriculum version {source.version_number}.',
        project=project,
        source_version=source,
    )


def approve_curriculum_version(curriculum: CurriculumVersion, *, approved_by) -> CurriculumVersion:
    """Approve a draft snapshot without changing its curriculum content."""
    if approved_by.pk != curriculum.course.owner_id:
        raise ValidationError('Only the course owner may approve a curriculum revision.')
    if curriculum.status == CurriculumVersion.Status.SUPERSEDED:
        raise ValidationError('A superseded curriculum version cannot be approved.')

    with transaction.atomic():
        locked_course = Course.objects.select_for_update().get(pk=curriculum.course_id)
        locked_curriculum = CurriculumVersion.objects.get(pk=curriculum.pk)
        CurriculumVersion.objects.filter(
            course=locked_course,
            status=CurriculumVersion.Status.APPROVED,
        ).exclude(pk=locked_curriculum.pk).update(status=CurriculumVersion.Status.SUPERSEDED)
        locked_curriculum.status = CurriculumVersion.Status.APPROVED
        locked_curriculum.full_clean()
        locked_curriculum.save(update_fields=('status',))
        Course.objects.filter(pk=locked_course.pk).update(status=Course.Status.APPROVED)
        return locked_curriculum


def create_lesson_revision(lesson: Lesson, *, created_by, content_markdown: str, metadata=None, change_summary=''):
    """Append an immutable manual revision to a lesson owned by the author."""
    if created_by.pk != lesson.section.curriculum_version.course.owner_id:
        raise ValidationError('Only the course owner may create a lesson revision.')
    with transaction.atomic():
        locked_lesson = Lesson.objects.select_for_update().select_related(
            'section__curriculum_version__course'
        ).get(pk=lesson.pk)
        revision_number = (locked_lesson.revisions.aggregate(Max('revision_number'))['revision_number__max'] or 0) + 1
        revision = LessonRevision(
            lesson=locked_lesson,
            created_by=created_by,
            revision_number=revision_number,
            content_markdown=content_markdown,
            metadata=metadata or {},
            change_summary=change_summary,
        )
        revision.full_clean()
        revision.save()
        locked_lesson.status = Lesson.Status.READY
        locked_lesson.save(update_fields=('status', 'updated_at'))
    return revision


def restore_lesson_revision(source: LessonRevision, *, restored_by) -> LessonRevision:
    """Append a new revision cloned from history; source content is never modified."""
    source = LessonRevision.objects.select_related(
        'lesson__section__curriculum_version__course'
    ).get(pk=source.pk)
    if restored_by.pk != source.lesson.section.curriculum_version.course.owner_id:
        raise ValidationError('Only the course owner may restore a lesson revision.')
    return create_lesson_revision(
        source.lesson,
        created_by=restored_by,
        content_markdown=source.content_markdown,
        metadata=deepcopy(source.metadata),
        change_summary=f'Restored from revision {source.revision_number}.',
    )
