from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View

from courses.models import Lesson

from .models import GenerationJob
from .services import GenerationConfigurationError, enqueue_lesson_job, request_job_cancellation


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
        except GenerationConfigurationError:
            messages.error(request, 'Configure an enabled default provider and model before generating lessons.')
        else:
            messages.success(request, 'Lesson generation has been queued.')
        return self.workspace_redirect(lesson)


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
            except GenerationConfigurationError:
                messages.error(request, 'Configure an enabled default provider and model before retrying.')
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
