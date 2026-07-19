"""Generation job orchestration independent of views, HTML, and Celery transport."""

import json
import math
import re
from dataclasses import dataclass, replace
from datetime import timedelta

from django.conf import settings as django_settings
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from kombu.exceptions import OperationalError as KombuOperationalError
from pydantic import ValidationError as PydanticValidationError

from courses.models import Course, CurriculumVersion, Lesson
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


class GenerationDispatchError(RuntimeError):
    """Raised when a validated job cannot be sent to the Celery broker."""


class RetryableGenerationError(RuntimeError):
    """Raised for provider calls that may succeed when retried by Celery."""


class ResponseValidationError(RuntimeError):
    """Raised when an LLM response cannot satisfy a required output schema."""


class ActiveLessonGenerationJobError(RuntimeError):
    """Raised when a lesson already has queued, running, or retrying generation work."""


RESPONSE_PREVIEW_MAX_LENGTH = 30_000
STALE_JOB_GRACE_SECONDS = 60


@dataclass(frozen=True)
class BatchLessonEnqueueResult:
    """Result from :func:`enqueue_lesson_jobs`, the batch workspace boundary."""

    queued_jobs: tuple[GenerationJob, ...]
    skipped_lessons: tuple[Lesson, ...]


def enqueue_curriculum_job(course_id, revision_instruction=None, source_curriculum_version_id=None):
    """Create and enqueue curriculum work through the only course-view boundary."""
    course = Course.objects.get(pk=course_id)
    source_curriculum = None
    if source_curriculum_version_id:
        source_curriculum = CurriculumVersion.objects.filter(
            pk=source_curriculum_version_id,
            course=course,
        ).first()
        if source_curriculum is None:
            raise ValueError('The source curriculum version does not belong to this course.')
    job = _create_job(
        course=course,
        job_type=GenerationJob.JobType.CURRICULUM,
        revision_instruction=revision_instruction or '',
        source_curriculum_version_id=source_curriculum.pk if source_curriculum else None,
    )
    _dispatch_generation_job(job)
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
    _dispatch_generation_job(job)
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
    """Cancel queued work immediately; request cooperative cancellation for running work."""
    if job.status in _terminal_statuses():
        return job
    job.cancellation_requested_at = timezone.now()
    if job.status in {GenerationJob.Status.QUEUED, GenerationJob.Status.RETRYING}:
        job.status = GenerationJob.Status.CANCELLED
        job.completed_at = job.cancellation_requested_at
        job.error_code = 'cancelled'
        job.error_message = 'Cancelled before background generation started.'
        job.save(
            update_fields=(
                'status',
                'cancellation_requested_at',
                'completed_at',
                'error_code',
                'error_message',
            )
        )
        return job
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


def recover_stale_generation_jobs(*, course=None, now=None) -> int:
    """Finalize running jobs that exceeded the worker/provider deadline.

    This is deliberately conservative: queued jobs can wait behind valid work,
    while a running job is only recovered after both the configured provider
    timeout and Celery's hard task limit have elapsed, plus a small grace period.
    """
    now = now or timezone.now()
    generation_settings = GenerationSettings.get_solo()
    deadline_seconds = max(
        generation_settings.request_timeout_seconds,
        django_settings.CELERY_TASK_TIME_LIMIT,
    ) + STALE_JOB_GRACE_SECONDS
    cutoff = now - timedelta(seconds=deadline_seconds)
    jobs = GenerationJob.objects.filter(
        status=GenerationJob.Status.RUNNING,
        started_at__lte=cutoff,
    )
    if course is not None:
        jobs = jobs.filter(course=course)

    recovered = 0
    for job in jobs:
        if job.cancellation_requested_at:
            status = GenerationJob.Status.CANCELLED
            error_code = 'cancelled'
            message = 'Cancelled after the generation worker exceeded its execution deadline.'
        else:
            status = GenerationJob.Status.FAILED
            error_code = 'generation_timeout'
            message = 'Generation exceeded its execution deadline. Please retry.'
        updated = GenerationJob.objects.filter(
            pk=job.pk,
            status=GenerationJob.Status.RUNNING,
            started_at__lte=cutoff,
        ).update(
            status=status,
            error_code=error_code,
            error_message=message,
            completed_at=now,
        )
        if updated:
            recovered += 1
            if job.lesson_id and status == GenerationJob.Status.FAILED:
                Lesson.objects.filter(pk=job.lesson_id).update(status=Lesson.Status.FAILED)
    return recovered


def _create_job(*, course, job_type, lesson=None, revision_instruction='', source_curriculum_version_id=None):
    settings = GenerationSettings.get_solo()
    if not settings.default_provider_id or not settings.default_model_id:
        raise GenerationConfigurationError('An enabled default provider and model must be configured first.')
    job = GenerationJob(
        course=course,
        lesson=lesson,
        provider=settings.default_provider,
        llm_model=settings.default_model,
        job_type=job_type,
        input_metadata={
            key: value
            for key, value in {
                'revision_instruction': revision_instruction,
                'source_curriculum_version_id': source_curriculum_version_id,
            }.items()
            if value
        },
    )
    if job_type == GenerationJob.JobType.CURRICULUM:
        # Snapshot the limit so an administrator changing settings later does
        # not alter the owner-visible behavior of already queued work.
        job.input_metadata['curriculum_response_limit'] = settings.curriculum_response_limit
    job.full_clean()
    job.save()
    return job


def _dispatch_generation_job(job: GenerationJob) -> None:
    """Send a job to Celery and preserve a safe failed record if Redis is unavailable."""
    from .tasks import run_generation_job

    try:
        run_generation_job.delay(job.pk)
    except KombuOperationalError as exc:
        _mark_job_failed(
            job,
            'broker_unavailable',
            'The generation queue is unavailable. Check the Redis broker and Celery worker, then retry.',
        )
        raise GenerationDispatchError(
            'The generation queue is unavailable. Check Redis and the Celery worker, then retry.'
        ) from exc


def _process_curriculum_job(job, adapter):
    course = job.course
    source_curriculum = _source_curriculum_for_job(job)
    if job.input_metadata.get('source_curriculum_version_id') and source_curriculum is None:
        return _mark_job_failed(
            job,
            'invalid_source_curriculum',
            'The curriculum draft selected for revision is no longer available.',
        )
    request = _request_with_generation_settings(
        _curriculum_request(
            course,
            job.input_metadata.get('revision_instruction', ''),
            source_curriculum=source_curriculum,
        ),
        job,
    )
    result = _generate_with_bounded_continuations(
        job,
        adapter,
        request,
        CurriculumOutput,
        prompt_template_version='curriculum-v1',
        continue_until_complete=True,
        response_limit=job.input_metadata.get(
            'curriculum_response_limit',
            GenerationSettings.get_solo().curriculum_response_limit,
        ),
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
            source_version=source_curriculum,
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
        _with_structured_output(
            _lesson_request(lesson, job.input_metadata.get('revision_instruction', '')),
            LessonOutput,
            'lesson_output',
        ),
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

    content_markdown = _render_lesson_markdown(result)
    create_lesson_revision(
        lesson,
        created_by=lesson.section.curriculum_version.course.owner,
        content_markdown=content_markdown,
        metadata={
            'generation_schema_version': 'lesson-v2',
            'lesson_plan': result.model_dump(mode='json'),
            'estimated_content_duration_minutes': estimate_content_duration_minutes(content_markdown),
        },
        change_summary='Generated structured lesson content',
    )
    return _mark_job_succeeded(job)


def _generate_with_bounded_continuations(
    job,
    adapter,
    request,
    schema,
    *,
    prompt_template_version,
    result_validator=None,
    max_continuations=None,
    continue_until_complete=False,
    response_limit=None,
):
    settings = GenerationSettings.get_solo()
    continuation_limit = settings.max_continuations if max_continuations is None else max_continuations
    continuation = 0
    repair_count = 0
    current_request = request
    accumulated_text = ''
    while True:
        if _cancel_if_requested(job):
            return None
        response_count = job.attempts.count()
        if response_limit is not None and response_count >= response_limit:
            _mark_job_needs_review(
                job,
                f'Reached the configured limit of {response_limit} provider responses before the curriculum was complete.',
            )
            return None
        attempt = _start_attempt(job, prompt_template_version, continuation)
        request_for_attempt = replace(
            current_request,
            on_text_delta=lambda delta, attempt_id=attempt.pk: _append_attempt_response_preview(attempt_id, delta),
        )
        try:
            response = adapter.generate(request_for_attempt)
        except ProviderConfigurationError as exc:
            _fail_attempt(attempt, 'provider_configuration_error', str(exc))
            _mark_job_failed(job, 'provider_configuration_error', str(exc))
            return None
        except Exception as exc:
            _fail_attempt(attempt, 'provider_request_error', str(exc))
            raise RetryableGenerationError(str(exc)) from exc
        _record_attempt_response(attempt, response)
        if _cancel_if_requested(job):
            _fail_attempt(attempt, 'cancelled', 'Cancellation was requested during generation.')
            return None
        try:
            candidate_text = accumulated_text + response.text
            parsed = _parse_response(candidate_text, schema)
            if result_validator:
                result_validator(parsed)
        except ResponseValidationError as exc:
            if continue_until_complete and _response_is_truncated_json(response, candidate_text):
                _succeed_attempt(attempt, response)
                accumulated_text = candidate_text
                continuation += 1
                job.progress_total = max(job.progress_total, continuation + 1)
                job.save(update_fields=('progress_total',))
                current_request = _json_fragment_continuation_request(request, accumulated_text)
                continue
            _fail_attempt(attempt, 'response_validation_error', str(exc))
            if repair_count >= continuation_limit:
                _mark_job_needs_review(job, str(exc))
                return None
            repair_count += 1
            continuation += 1
            job.progress_total = continuation + 1
            job.save(update_fields=('progress_total',))
            current_request = _continuation_request(request, str(exc))
            accumulated_text = ''
            continue

        _succeed_attempt(attempt, replace(response, text=candidate_text))
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
        # Be explicit here as well as at the model layer.  A worker that is
        # restarted around a migration must never insert NULL into this
        # non-null database column before it can call the provider.
        response_preview='',
    )


def _succeed_attempt(attempt, response):
    attempt.status = GenerationAttempt.Status.SUCCEEDED
    attempt.response_metadata = response.metadata
    attempt.response_preview = (response.text or '')[:RESPONSE_PREVIEW_MAX_LENGTH]
    attempt.input_tokens = response.input_tokens
    attempt.output_tokens = response.output_tokens
    attempt.completed_at = timezone.now()
    attempt.save(
        update_fields=(
            'status', 'response_metadata', 'response_preview', 'input_tokens', 'output_tokens', 'completed_at',
        )
    )


def _record_attempt_response(attempt, response):
    """Persist safe provider diagnostics without retaining generated content."""
    metadata = dict(response.metadata)
    metadata.update(
        {
            'provider_request_id': response.provider_request_id,
            'response_text_length': len(response.text or ''),
        }
    )
    attempt.response_metadata = metadata
    attempt.response_preview = (response.text or '')[:RESPONSE_PREVIEW_MAX_LENGTH]
    attempt.save(update_fields=('response_metadata', 'response_preview'))


def _append_attempt_response_preview(attempt_id, delta):
    """Append a bounded native-provider stream preview for the job owner only."""
    if not delta:
        return
    attempt = GenerationAttempt.objects.only('response_preview').get(pk=attempt_id)
    remaining = RESPONSE_PREVIEW_MAX_LENGTH - len(attempt.response_preview)
    if remaining <= 0:
        return
    attempt.response_preview += delta[:remaining]
    attempt.save(update_fields=('response_preview',))


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
        error = exc.errors()[0]
        location = '.'.join(str(part) for part in error.get('loc', ())) or 'response'
        raise ResponseValidationError(
            f'The provider response field "{location}" is invalid: {error["msg"]}.'
        ) from exc


def _strip_code_fence(text):
    text = (text or '').strip()
    if text.startswith('```') and text.endswith('```'):
        return text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    return text


def _curriculum_request(course, revision_instruction, *, source_curriculum=None):
    duration = course.desired_duration_minutes or 'Propose an appropriate duration'
    return GenerationRequest(
        model='',
        prompt=(
            'Return exactly one JSON object; do not include Markdown, code fences, or prose outside the object. '
            'Use strings for every description, summary, outline, and explanation; use arrays only where a list '
            'is requested. Include course_description, overall_learning_outcomes, prerequisites, '
            'suggested_duration_minutes, duration_estimate_explanation, sections, and project. Use null for '
            'project when the course does not need one. '
            'Each section must contain title, duration_minutes, learning_outcomes, summary, and lessons. '
            'Each lesson must contain title, duration_minutes, objectives, and outline.\n\n'
            'When project is an object, include title, description, deliverables, and evaluation_criteria.\n\n'
            f'Course title: {course.title}\nTopic: {course.topic}\nAudience: {course.target_audience}\n'
            f'Learning progression: {course.level_progression_display}\n'
            f'Starting level: {course.get_starting_level_display() if course.starting_level else course.get_level_display()}\n'
            f'Target completion level: {course.get_target_completion_level_display() if course.target_completion_level else course.get_level_display()}\n'
            f'Target duration: {duration}\nLearning outcomes: {course.learning_outcomes}\n'
            f'Prerequisites from brief: {course.prerequisites}\nConstraints: {course.constraints}\n'
            f'Revision instruction: {revision_instruction}\n'
            f'{_source_curriculum_prompt(source_curriculum)}'
            'Sequence the sections and lessons from the starting level toward the target completion level. '
            'The suggested duration must equal the sum of all lesson durations. Explain the estimate.'
        ),
    )


def _source_curriculum_for_job(job):
    source_id = job.input_metadata.get('source_curriculum_version_id')
    if not source_id:
        return None
    return CurriculumVersion.objects.prefetch_related('sections__lessons', 'course_project').filter(
        pk=source_id,
        course_id=job.course_id,
    ).first()


def _source_curriculum_prompt(curriculum):
    if curriculum is None:
        return ''
    payload = {
        'course_description': curriculum.course_description,
        'overall_learning_outcomes': curriculum.overall_learning_outcomes,
        'prerequisites': curriculum.prerequisites,
        'suggested_duration_minutes': curriculum.suggested_duration_minutes,
        'duration_estimate_explanation': curriculum.duration_estimate_explanation,
        'sections': [
            {
                'title': section.title,
                'duration_minutes': section.duration_minutes,
                'summary': section.summary,
                'learning_outcomes': section.learning_outcomes,
                'lessons': [
                    {
                        'title': lesson.title,
                        'duration_minutes': lesson.duration_minutes,
                        'objectives': lesson.objectives,
                        'outline': lesson.outline,
                    }
                    for lesson in section.lessons.all()
                ],
            }
            for section in curriculum.sections.all()
        ],
        'project': _source_project_payload(curriculum),
    }
    return (
        'Current proposed curriculum draft follows. Rewrite this draft according to the revision instruction; '
        'preserve useful material unless the instruction requires changing it.\n'
        f'{json.dumps(payload, ensure_ascii=False)}\n'
    )


def _source_project_payload(curriculum):
    if not hasattr(curriculum, 'course_project'):
        return None
    project = curriculum.course_project
    return {
        'title': project.title,
        'description': project.description,
        'deliverables': project.deliverables,
        'evaluation_criteria': project.evaluation_criteria,
    }


def _lesson_request(lesson, revision_instruction=''):
    course = lesson.section.curriculum_version.course
    return GenerationRequest(
        model='',
        prompt=(
            'Return exactly one JSON object; do not include Markdown, code fences, or prose outside the object. '
            'The object must include objectives, expected_duration_minutes, preparation, '
            'materials, timed_teaching_flow, concepts_explanations, examples, activities, assessment, '
            'common_misconceptions, and optional project_linkage. Every list must contain at least one item. '
            'Each timed_teaching_flow item needs title, description, and duration_minutes; their durations must '
            'total expected_duration_minutes. Each activity needs title, description, and expected_output. '
            'assessment needs check_for_understanding and expected_answers_or_rubric. Each concept, example, and '
            'misconception needs title and description. project_linkage, when present, needs project_title and '
            'connection; use null when there is no project linkage. expected_duration_minutes must equal the '
            'planned lesson duration.\n\n'
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


def estimate_content_duration_minutes(content_markdown: str) -> int:
    """Estimate self-guided content time from generated reading and code volume."""
    code_blocks = re.findall(r'```.*?```', content_markdown, flags=re.DOTALL)
    code_lines = sum(block.count('\n') + 1 for block in code_blocks)
    text_without_code = re.sub(r'```.*?```', ' ', content_markdown, flags=re.DOTALL)
    word_count = len(re.findall(r"\b[\w'-]+\b", text_without_code))
    minutes = (word_count / 180) + (code_lines * 0.35)
    return max(5, math.ceil(minutes / 5) * 5)


def _continuation_request(original, validation_message):
    return GenerationRequest(
        model=original.model,
        system_instruction=original.system_instruction,
        temperature=original.temperature,
        max_output_tokens=original.max_output_tokens,
        json_schema=original.json_schema,
        json_schema_name=original.json_schema_name,
        prompt=(
            f'{original.prompt}\n\nYour previous response was incomplete or invalid: {validation_message} '
            'Return a complete replacement JSON object only, with all required fields and the exact requested types.'
        ),
    )


def _json_fragment_continuation_request(original, partial_json):
    """Ask for the remainder of a JSON object that exceeded one response.

    Curriculum requests intentionally use ordinary chat output rather than a
    provider's one-shot structured-output wrapper so a length-limited JSON
    response can be completed and validated locally across multiple calls.
    """
    return GenerationRequest(
        model=original.model,
        system_instruction=original.system_instruction,
        temperature=original.temperature,
        max_output_tokens=original.max_output_tokens,
        timeout_seconds=original.timeout_seconds,
        prompt=(
            f'{original.prompt}\n\nThe JSON response below was cut off before completion. '
            'Continue from its final character. Return only the remaining JSON characters: '
            'do not repeat any earlier text, do not start a new object, and do not use Markdown fences.\n\n'
            f'Partial JSON:\n{partial_json}'
        ),
    )


def _response_is_truncated_json(response, text):
    metadata = response.metadata or {}
    finish_reason = str(
        metadata.get('finish_reason') or metadata.get('stop_reason') or metadata.get('finishReason') or ''
    ).casefold()
    if finish_reason in {'length', 'max_tokens', 'max_token', 'token_limit'}:
        return True
    try:
        json.loads(_strip_code_fence(text))
    except json.JSONDecodeError as exc:
        clean_text = _strip_code_fence(text)
        return exc.pos >= len(clean_text) - 2 or 'Unterminated' in exc.msg
    return False


def _request_with_generation_settings(request, job):
    settings = GenerationSettings.get_solo()
    return replace(
        request,
        model=job.llm_model.identifier,
        temperature=settings.default_temperature,
        max_output_tokens=settings.max_output_tokens,
        timeout_seconds=settings.request_timeout_seconds,
    )


def _with_structured_output(request, schema, name):
    """Attach a strict, provider-neutral JSON Schema to a generation request."""
    return replace(
        request,
        json_schema=_strict_json_schema(schema.model_json_schema()),
        json_schema_name=name,
    )


def _strict_json_schema(schema):
    """Make Pydantic's schema suitable for strict OpenAI Structured Outputs.

    Strict Responses schemas require object nodes to reject unknown keys and to
    explicitly list all object properties as required. Nullable values (such as
    ``project``) remain optional in meaning by accepting ``null``.
    """
    if isinstance(schema, dict):
        properties = schema.get('properties')
        if schema.get('type') == 'object' and isinstance(properties, dict):
            schema['additionalProperties'] = False
            schema['required'] = list(properties)
        for value in schema.values():
            _strict_json_schema(value)
    elif isinstance(schema, list):
        for value in schema:
            _strict_json_schema(value)
    return schema
