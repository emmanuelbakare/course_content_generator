from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View

from courses.models import Course, CurriculumVersion, Lesson

from .models import GenerationJob
from .services import (
    ActiveLessonGenerationJobError,
    GenerationConfigurationError,
    GenerationDispatchError,
    enqueue_curriculum_job,
    enqueue_lesson_job,
    enqueue_lesson_jobs,
    request_job_cancellation,
)


class OwnedLessonActionMixin(LoginRequiredMixin):
    def get_lesson(self):
        return get_object_or_404(
            Lesson.objects.select_related('section__curriculum_version__course'),
            public_id=self.kwargs['lesson_id'],
            section__curriculum_version__course__owner=self.request.user,
        )

    def workspace_redirect(self, lesson):
        course = lesson.section.curriculum_version.course
        return redirect(f'{reverse("courses:workspace", args=[course.public_id])}?lesson={lesson.public_id}')


class LessonGenerationView(OwnedLessonActionMixin, View):
    def post(self, request, lesson_id):
        lesson = self.get_lesson()
        try:
            enqueue_lesson_job(lesson.pk, revision_instruction=request.POST.get('revision_instruction', '').strip())
        except ActiveLessonGenerationJobError:
            messages.warning(request, 'Lesson generation is already queued or running for this lesson.')
        except GenerationConfigurationError:
            messages.error(request, 'Configure an enabled default provider and model before generating lessons.')
        except GenerationDispatchError:
            messages.error(request, 'Lesson generation could not be queued. Check Redis and the Celery worker, then retry.')
        else:
            messages.success(request, 'Lesson generation has been queued.')
        return self.workspace_redirect(lesson)


class BatchLessonGenerationView(LoginRequiredMixin, View):
    """Queue selected approved-curriculum lessons through ``enqueue_lesson_jobs``."""

    def post(self, request, course_id):
        course = get_object_or_404(Course, public_id=course_id, owner=request.user)
        selected_ids = request.POST.getlist('lesson_ids')
        if not selected_ids:
            messages.warning(request, 'Select one or more lessons to generate.')
            return redirect('courses:workspace', course_id=course.public_id)
        lessons = list(
            Lesson.objects.select_related('section__curriculum_version__course').filter(
                public_id__in=selected_ids,
                section__curriculum_version__course=course,
                section__curriculum_version__status=CurriculumVersion.Status.APPROVED,
            )
        )
        if len(lessons) != len(set(selected_ids)):
            raise Http404('One or more selected lessons are not in your approved curriculum.')
        try:
            result = enqueue_lesson_jobs(lessons)
        except GenerationConfigurationError:
            messages.error(request, 'Configure an enabled default provider and model before generating lessons.')
        except GenerationDispatchError:
            messages.error(request, 'Lesson generation could not be queued. Check Redis and the Celery worker, then retry.')
        else:
            if result.queued_jobs:
                messages.success(request, f'Queued generation for {len(result.queued_jobs)} lesson(s).')
            if result.skipped_lessons:
                names = ', '.join(lesson.title for lesson in result.skipped_lessons)
                messages.warning(
                    request,
                    f'Skipped {len(result.skipped_lessons)} lesson(s) with active generation jobs: {names}.',
                )
        return redirect('courses:workspace', course_id=course.public_id)


class LessonRetryView(LoginRequiredMixin, View):
    def post(self, request, job_id):
        job = get_object_or_404(
            GenerationJob.objects.select_related('lesson__section__curriculum_version__course'),
            public_id=job_id,
            lesson__section__curriculum_version__course__owner=request.user,
        )
        if job.status not in {
            GenerationJob.Status.FAILED,
            GenerationJob.Status.CANCELLED,
            GenerationJob.Status.NEEDS_REVIEW,
        }:
            messages.error(request, 'Only unsuccessful lesson jobs can be retried.')
        else:
            try:
                enqueue_lesson_job(
                    job.lesson_id,
                    revision_instruction=job.input_metadata.get('revision_instruction', ''),
                )
            except ActiveLessonGenerationJobError:
                messages.warning(request, 'Lesson generation is already queued or running for this lesson.')
            except GenerationConfigurationError:
                messages.error(request, 'Configure an enabled default provider and model before retrying.')
            except GenerationDispatchError:
                messages.error(request, 'Lesson retry could not be queued. Check Redis and the Celery worker, then retry.')
            else:
                messages.success(request, 'Lesson retry has been queued.')
        course = job.lesson.section.curriculum_version.course
        return redirect(f'{reverse("courses:workspace", args=[course.public_id])}?lesson={job.lesson.public_id}')


class LessonCancelView(LoginRequiredMixin, View):
    def post(self, request, job_id):
        job = get_object_or_404(
            GenerationJob.objects.select_related('lesson__section__curriculum_version__course'),
            public_id=job_id,
            lesson__section__curriculum_version__course__owner=request.user,
        )
        request_job_cancellation(job)
        messages.success(request, 'Cancellation requested. The worker will stop after its current provider call.')
        course = job.lesson.section.curriculum_version.course
        return redirect(f'{reverse("courses:workspace", args=[course.public_id])}?lesson={job.lesson.public_id}')


class CurriculumRetryView(LoginRequiredMixin, View):
    def post(self, request, job_id):
        job = get_object_or_404(
            GenerationJob.objects.select_related('course'),
            public_id=job_id,
            course__owner=request.user,
            job_type=GenerationJob.JobType.CURRICULUM,
        )
        if job.status not in {
            GenerationJob.Status.FAILED,
            GenerationJob.Status.CANCELLED,
            GenerationJob.Status.NEEDS_REVIEW,
        }:
            messages.error(request, 'Only unsuccessful curriculum jobs can be retried.')
        else:
            try:
                source_curriculum_version_id = job.input_metadata.get('source_curriculum_version_id')
                enqueue_kwargs = {}
                if source_curriculum_version_id:
                    enqueue_kwargs['source_curriculum_version_id'] = source_curriculum_version_id
                enqueue_curriculum_job(
                    job.course_id,
                    revision_instruction=job.input_metadata.get('revision_instruction', ''),
                    **enqueue_kwargs,
                )
            except GenerationConfigurationError:
                messages.error(request, 'Configure an enabled default provider and model before retrying.')
            except GenerationDispatchError:
                messages.error(request, 'Curriculum retry could not be queued. Check Redis and the Celery worker, then retry.')
            else:
                messages.success(request, 'Curriculum generation retry has been queued.')
        return redirect('courses:detail', course_id=job.course.public_id)


class CurriculumCancelView(LoginRequiredMixin, View):
    def post(self, request, job_id):
        job = get_object_or_404(
            GenerationJob.objects.select_related('course'),
            public_id=job_id,
            course__owner=request.user,
            job_type=GenerationJob.JobType.CURRICULUM,
        )
        if job.status in {GenerationJob.Status.QUEUED, GenerationJob.Status.RUNNING, GenerationJob.Status.RETRYING}:
            request_job_cancellation(job)
            messages.success(request, 'Curriculum cancellation requested. The worker will stop safely.')
        else:
            messages.warning(request, 'This curriculum job has already reached a terminal state.')
        return redirect('courses:detail', course_id=job.course.public_id)
