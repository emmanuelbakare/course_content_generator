"""Generation job orchestration independent of views, HTML, and Celery transport."""

import json
from dataclasses import dataclass, replace

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from pydantic import ValidationError as PydanticValidationError

from courses.models import Course, Lesson
from courses.services import (
    LessonSpec,
    ProjectSpec,
    SectionSpec,
    create_curriculum_revision,
    create_lesson_revision,
)

from .adapters import GenerationRequest, ProviderConfigurationError, get_adapter
from .models import GenerationAttempt, GenerationJob, GenerationSettings
from .schemas import CurriculumOutput, LessonOutput


class GenerationConfigurationError(RuntimeError):
    """Raised when no enabled default provider/model is available."""


class RetryableGenerationError(RuntimeError):
    """Raised for provider calls that may succeed when retried by Celery."""


class ResponseValidationError(RuntimeError):
    """Raised when an LLM response cannot satisfy a required output schema."""


class ActiveLessonGenerationJobError(RuntimeError):
    """Raised when a lesson already has queued, running, or retrying generation work."""


@dataclass(frozen=True)
class BatchLessonEnqueueResult:
    """Result from :func:`enqueue_lesson_jobs`, the batch workspace boundary."""

    queued_jobs: tuple[GenerationJob, ...]
    skipped_lessons: tuple[Lesson, ...]


def enqueue_curriculum_job(course_id, revision_instruction=None):
    """Create and enqueue curriculum work through the only course-view boundary."""
    course = Course.objects.get(pk=course_id)
    job = _create_job(
        course=course,
        job_type=GenerationJob.JobType.CURRICULUM,
        revision_instruction=revision_instruction or '',
    )
    from .tasks import run_generation_job

    run_generation_job.delay(job.pk)
    return job


def enqueue_lesson_job(lesson_id, revision_instruction=None):
    """Create and enqueue one lesson-generation job for later workspace use."""
    lesson = Lesson.objects.select_related('section__curriculum_version__course').get(pk=lesson_id)
    try:
        with transaction.atomic():
            if _has_active_lesson_job(lesson.pk):
                raise ActiveLessonGenerationJobError(
                    f'Lesson "{lesson.title}" already has an active generation job.'
                )
            job = _create_job(
                course=lesson.section.curriculum_version.course,
                lesson=lesson,
                job_type=GenerationJob.JobType.LESSON,
                revision_instruction=revision_instruction or '',
            )
    except IntegrityError as exc:
        raise ActiveLessonGenerationJobError(
            f'Lesson "{lesson.title}" already has an active generation job.'
        ) from exc
    from .tasks import run_generation_job

    run_generation_job.delay(job.pk)
    return job


def enqueue_lesson_jobs(lessons, *, revision_instruction='') -> BatchLessonEnqueueResult:
    """Queue selected lessons, skipping any that already have active generation work.

    Callers must owner-scope ``lessons`` before passing them here. This boundary is
    deliberately independent of the workspace HTML and provider adapter internals.
    """
    queued_jobs = []
    skipped_lessons = []
    seen_lesson_ids = set()
    for lesson in lessons:
        if lesson.pk in seen_lesson_ids:
            continue
        seen_lesson_ids.add(lesson.pk)
        try:
            queued_jobs.append(
                enqueue_lesson_job(lesson.pk, revision_instruction=revision_instruction)
            )
        except ActiveLessonGenerationJobError:
            skipped_lessons.append(lesson)
    return BatchLessonEnqueueResult(tuple(queued_jobs), tuple(skipped_lessons))


def request_job_cancellation(job: GenerationJob) -> GenerationJob:
    """Request cooperative cancellation; the worker checks before/after each call."""
    if job.status in _terminal_statuses():
        return job
    job.cancellation_requested_at = timezone.now()
    job.save(update_fields=('cancellation_requested_at',))
    return job


def process_generation_job(job_id, *, adapter_factory=get_adapter) -> GenerationJob:
    """Run one job synchronously. Celery handles retries around this function."""
    job = GenerationJob.objects.select_related(
        'course', 'provider', 'llm_model', 'lesson__section__curriculum_version__course'
    ).get(pk=job_id)
    if _cancel_if_requested(job):
        return job

    job.status = GenerationJob.Status.RUNNING
    job.started_at = job.started_at or timezone.now()
    job.progress_total = max(job.progress_total, 1)
    job.error_code = ''
    job.error_message = ''
    job.save(update_fields=('status', 'started_at', 'progress_total', 'error_code', 'error_message'))

    try:
        adapter = adapter_factory(job.provider)
    except ProviderConfigurationError as exc:
        return _mark_job_failed(job, 'provider_configuration_error', str(exc))

    if job.job_type == GenerationJob.JobType.CURRICULUM:
        return _process_curriculum_job(job, adapter)
    return _process_lesson_job(job, adapter)


def mark_job_retrying(job_id, message):
    job = GenerationJob.objects.get(pk=job_id)
    if job.status not in _terminal_statuses():
        job.status = GenerationJob.Status.RETRYING
        job.error_code = 'provider_request_error'
        job.error_message = message
        job.save(update_fields=('status', 'error_code', 'error_message'))
    return job


def mark_job_failed(job_id, error_code, message):
    return _mark_job_failed(GenerationJob.objects.get(pk=job_id), error_code, message)


def _create_job(*, course, job_type, lesson=None, revision_instruction=''):
    settings = GenerationSettings.get_solo()
    if not settings.default_provider_id or not settings.default_model_id:
        raise GenerationConfigurationError('An enabled default provider and model must be configured first.')
    job = GenerationJob(
        course=course,
        lesson=lesson,
        provider=settings.default_provider,
        llm_model=settings.default_model,
        job_type=job_type,
        input_metadata={'revision_instruction': revision_instruction} if revision_instruction else {},
    )
    job.full_clean()
    job.save()
    return job


def _process_curriculum_job(job, adapter):
    course = job.course
    request = _request_with_generation_settings(
        _curriculum_request(course, job.input_metadata.get('revision_instruction', '')),
        job,
    )
    result = _generate_with_bounded_continuations(
        job,
        adapter,
        request,
        CurriculumOutput,
        prompt_template_version='curriculum-v1',
    )
    if result is None:
        return GenerationJob.objects.get(pk=job.pk)
    if _cancel_if_requested(job):
        return job

    sections = [
        SectionSpec(
            title=section.title,
            duration_minutes=section.duration_minutes,
            summary=section.summary,
            learning_outcomes=section.learning_outcomes,
            lessons=[
                LessonSpec(
                    title=lesson.title,
                    duration_minutes=lesson.duration_minutes,
                    objectives=lesson.objectives,
                    outline=lesson.outline,
                )
                for lesson in section.lessons
            ],
        )
        for section in result.sections
    ]
    project = None
    if result.project:
        project = ProjectSpec(
            title=result.project.title,
            description=result.project.description,
            deliverables=result.project.deliverables,
            evaluation_criteria=result.project.evaluation_criteria,
        )
    try:
        curriculum = create_curriculum_revision(
            course,
            created_by=course.owner,
            sections=sections,
            course_description=result.course_description,
            overall_learning_outcomes=result.overall_learning_outcomes,
            prerequisites=result.prerequisites,
            suggested_duration_minutes=result.suggested_duration_minutes,
            duration_estimate_explanation=result.duration_estimate_explanation,
            revision_instruction=job.input_metadata.get('revision_instruction', ''),
            project=project,
        )
    except Exception as exc:
        return _mark_job_failed(job, 'curriculum_persistence_error', str(exc))

    Course.objects.filter(pk=course.pk).update(status=Course.Status.READY_FOR_REVIEW)
    return _mark_job_succeeded(job, curriculum_version=curriculum)


def _process_lesson_job(job, adapter):
    lesson = job.lesson
    if lesson is None:
        return _mark_job_failed(job, 'invalid_job_target', 'Lesson generation requires a lesson target.')
    Lesson.objects.filter(pk=lesson.pk).update(status=Lesson.Status.GENERATING)
    request = _request_with_generation_settings(
        _lesson_request(lesson, job.input_metadata.get('revision_instruction', '')),
        job,
    )
    result = _generate_with_bounded_continuations(
        job,
        adapter,
        request,
        LessonOutput,
        prompt_template_version='lesson-v1',
        result_validator=lambda output: _validate_lesson_duration(output, lesson),
    )
    if result is None:
        return GenerationJob.objects.get(pk=job.pk)
    if _cancel_if_requested(job):
        return job

    create_lesson_revision(
        lesson,
        created_by=lesson.section.curriculum_version.course.owner,
        content_markdown=_render_lesson_markdown(result),
        metadata={
            'generation_schema_version': 'lesson-v2',
            'lesson_plan': result.model_dump(mode='json'),
        },
        change_summary='Generated structured lesson content',
    )
    return _mark_job_succeeded(job)


def _generate_with_bounded_continuations(
    job, adapter, request, schema, *, prompt_template_version, result_validator=None
):
    settings = GenerationSettings.get_solo()
    continuation = 0
    current_request = request
    while True:
        if _cancel_if_requested(job):
            return None
        attempt = _start_attempt(job, prompt_template_version, continuation)
        try:
            response = adapter.generate(current_request)
        except ProviderConfigurationError as exc:
            _fail_attempt(attempt, 'provider_configuration_error', str(exc))
            _mark_job_failed(job, 'provider_configuration_error', str(exc))
            return None
        except Exception as exc:
            _fail_attempt(attempt, 'provider_request_error', str(exc))
            raise RetryableGenerationError(str(exc)) from exc
        if _cancel_if_requested(job):
            _fail_attempt(attempt, 'cancelled', 'Cancellation was requested during generation.')
            return None
        try:
            parsed = _parse_response(response.text, schema)
            if result_validator:
                result_validator(parsed)
        except ResponseValidationError as exc:
            _fail_attempt(attempt, 'response_validation_error', str(exc))
            if continuation >= settings.max_continuations:
                _mark_job_needs_review(job, str(exc))
                return None
            continuation += 1
            job.progress_total = continuation + 1
            job.save(update_fields=('progress_total',))
            current_request = _continuation_request(request, str(exc))
            continue

        _succeed_attempt(attempt, response)
        return parsed


def _start_attempt(job, prompt_template_version, continuation):
    number = (job.attempts.aggregate(Max('attempt_number'))['attempt_number__max'] or 0) + 1
    return GenerationAttempt.objects.create(
        job=job,
        provider=job.provider,
        llm_model=job.llm_model,
        attempt_number=number,
        prompt_template_version=prompt_template_version,
        request_metadata={'continuation': continuation},
    )


def _succeed_attempt(attempt, response):
    attempt.status = GenerationAttempt.Status.SUCCEEDED
    attempt.response_metadata = response.metadata
    attempt.input_tokens = response.input_tokens
    attempt.output_tokens = response.output_tokens
    attempt.completed_at = timezone.now()
    attempt.save(
        update_fields=(
            'status', 'response_metadata', 'input_tokens', 'output_tokens', 'completed_at',
        )
    )


def _fail_attempt(attempt, error_code, message):
    attempt.status = GenerationAttempt.Status.FAILED
    attempt.error_code = error_code
    attempt.error_message = message[:5000]
    attempt.completed_at = timezone.now()
    attempt.save(update_fields=('status', 'error_code', 'error_message', 'completed_at'))


def _mark_job_succeeded(job, *, curriculum_version=None):
    job.status = GenerationJob.Status.SUCCEEDED
    job.progress_current = job.progress_total
    job.curriculum_version = curriculum_version or job.curriculum_version
    job.completed_at = timezone.now()
    job.save(update_fields=('status', 'progress_current', 'curriculum_version', 'completed_at'))
    return job


def _mark_job_needs_review(job, message):
    job.status = GenerationJob.Status.NEEDS_REVIEW
    job.error_code = 'response_validation_error'
    job.error_message = message[:5000]
    job.completed_at = timezone.now()
    job.save(update_fields=('status', 'error_code', 'error_message', 'completed_at'))
    return job


def _mark_job_failed(job, error_code, message):
    job.status = GenerationJob.Status.FAILED
    job.error_code = error_code
    job.error_message = str(message)[:5000]
    job.completed_at = timezone.now()
    job.save(update_fields=('status', 'error_code', 'error_message', 'completed_at'))
    if job.lesson_id:
        Lesson.objects.filter(pk=job.lesson_id).update(status=Lesson.Status.FAILED)
    return job


def _cancel_if_requested(job):
    job.refresh_from_db(fields=('cancellation_requested_at', 'status'))
    if not job.cancellation_requested_at:
        return False
    job.status = GenerationJob.Status.CANCELLED
    job.completed_at = timezone.now()
    job.save(update_fields=('status', 'completed_at'))
    return True


def _terminal_statuses():
    return {
        GenerationJob.Status.SUCCEEDED,
        GenerationJob.Status.FAILED,
        GenerationJob.Status.CANCELLED,
        GenerationJob.Status.NEEDS_REVIEW,
    }


def _has_active_lesson_job(lesson_id) -> bool:
    return GenerationJob.objects.filter(
        lesson_id=lesson_id,
        job_type=GenerationJob.JobType.LESSON,
        status__in=(
            GenerationJob.Status.QUEUED,
            GenerationJob.Status.RUNNING,
            GenerationJob.Status.RETRYING,
        ),
    ).exists()


def _parse_response(text, schema):
    try:
        payload = json.loads(_strip_code_fence(text))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ResponseValidationError('The provider response was not valid JSON.') from exc
    try:
        return schema.model_validate(payload)
    except PydanticValidationError as exc:
        raise ResponseValidationError(f'The provider response failed schema validation: {exc.errors()[0]["msg"]}.') from exc


def _strip_code_fence(text):
    text = (text or '').strip()
    if text.startswith('```') and text.endswith('```'):
        return text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    return text


def _curriculum_request(course, revision_instruction):
    duration = course.desired_duration_minutes or 'Propose an appropriate duration'
    return GenerationRequest(
        model='',
        prompt=(
            'Return JSON only with course_description, overall_learning_outcomes, prerequisites, '
            'suggested_duration_minutes, duration_estimate_explanation, sections, and optional project. '
            'Each section must contain title, duration_minutes, learning_outcomes, summary, and lessons. '
            'Each lesson must contain title, duration_minutes, objectives, and outline.\n\n'
            f'Course title: {course.title}\nTopic: {course.topic}\nAudience: {course.target_audience}\n'
            f'Level: {course.level}\nTarget duration: {duration}\nLearning outcomes: {course.learning_outcomes}\n'
            f'Prerequisites from brief: {course.prerequisites}\nConstraints: {course.constraints}\n'
            f'Revision instruction: {revision_instruction}\n'
            'The suggested duration must equal the sum of all lesson durations. Explain the estimate.'
        ),
    )


def _lesson_request(lesson, revision_instruction=''):
    course = lesson.section.curriculum_version.course
    return GenerationRequest(
        model='',
        prompt=(
            'Return JSON only. The object must include objectives, expected_duration_minutes, preparation, '
            'materials, timed_teaching_flow, concepts_explanations, examples, activities, assessment, '
            'common_misconceptions, and optional project_linkage. Every list must contain at least one item. '
            'Each timed_teaching_flow item needs title, description, and duration_minutes; their durations must '
            'total expected_duration_minutes. Each activity needs title, description, and expected_output. '
            'assessment needs check_for_understanding and expected_answers_or_rubric. Each concept, example, and '
            'misconception needs title and description. project_linkage, when present, needs project_title and '
            'connection. expected_duration_minutes must equal the planned lesson duration.\n\n'
            f'Course: {course.title}\nSection: {lesson.section.title}\nLesson: {lesson.title}\n'
            f'Duration: {lesson.duration_minutes}\nObjectives: {lesson.objectives}\nOutline: {lesson.outline}\n'
            f'Revision instruction: {revision_instruction}'
        ),
    )


def _validate_lesson_duration(result: LessonOutput, lesson: Lesson):
    if result.expected_duration_minutes != lesson.duration_minutes:
        raise ResponseValidationError(
            f'Expected lesson duration must be {lesson.duration_minutes} minutes, '
            f'not {result.expected_duration_minutes}.'
        )


def _render_lesson_markdown(result: LessonOutput) -> str:
    """Turn validated provider data into an instructor-editable Markdown revision."""

    lines = [
        '## Learning objectives',
        *[f'- {objective}' for objective in result.objectives],
        '',
        f'## Expected duration\n\n{result.expected_duration_minutes} minutes',
        '',
        '## Preparation',
        *[f'- {item}' for item in result.preparation],
        '',
        '## Materials',
        *[f'- {item}' for item in result.materials],
        '',
        '## Timed teaching flow',
    ]
    for step in result.timed_teaching_flow:
        lines.extend((f'### {step.title} ({step.duration_minutes} minutes)', step.description, ''))
    lines.append('## Concepts and explanations')
    for concept in result.concepts_explanations:
        lines.extend((f'### {concept.title}', concept.description, ''))
    lines.append('## Examples')
    for example in result.examples:
        lines.extend((f'### {example.title}', example.description, ''))
    lines.append('## Activities')
    for activity in result.activities:
        lines.extend(
            (f'### {activity.title}', activity.description, '**Expected learner output:**', activity.expected_output, '')
        )
    lines.extend(
        (
            '## Assessment / check for understanding',
            result.assessment.check_for_understanding,
            '',
            '### Expected answers or rubric',
            *[f'- {item}' for item in result.assessment.expected_answers_or_rubric],
            '',
            '## Common misconceptions',
        )
    )
    for misconception in result.common_misconceptions:
        lines.extend((f'### {misconception.title}', misconception.description, ''))
    if result.project_linkage:
        lines.extend(
            (
                '## Project linkage',
                f'### {result.project_linkage.project_title}',
                result.project_linkage.connection,
                '',
            )
        )
    return '\n'.join(lines).strip() + '\n'


def _continuation_request(original, validation_message):
    return GenerationRequest(
        model=original.model,
        system_instruction=original.system_instruction,
        temperature=original.temperature,
        max_output_tokens=original.max_output_tokens,
        prompt=(
            f'{original.prompt}\n\nYour previous response was incomplete or invalid: {validation_message} '
            'Return a complete replacement JSON object only, with all required fields.'
        ),
    )


def _request_with_generation_settings(request, job):
    settings = GenerationSettings.get_solo()
    return replace(
        request,
        model=job.llm_model.identifier,
        temperature=settings.default_temperature,
        max_output_tokens=settings.max_output_tokens,
    )
