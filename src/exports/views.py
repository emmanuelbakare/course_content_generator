from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from courses.models import Course

from .models import ExportJob
from .services import (
    ExportConfigurationError,
    cancel_export_job,
    enqueue_course_export,
    retry_export_job,
)


class CourseExportCreateView(LoginRequiredMixin, View):
    def post(self, request, course_id):
        course = get_object_or_404(Course, public_id=course_id, owner=request.user)
        try:
            job = enqueue_course_export(
                course_id=course.pk,
                requested_by=request.user,
                export_format=request.POST.get('export_format', ''),
            )
        except ExportConfigurationError as exc:
            messages.error(request, '; '.join(exc.messages))
        else:
            messages.success(request, f'{job.get_export_format_display()} export has been queued.')
        return redirect('courses:detail', course_id=course.public_id)


class ExportJobStatusView(LoginRequiredMixin, View):
    def get(self, request, job_id):
        job = get_object_or_404(
            ExportJob.objects.select_related('export_file'),
            public_id=job_id,
            requested_by=request.user,
        )
        response = {
            'id': str(job.public_id),
            'format': job.export_format,
            'status': job.status,
            'progress_current': job.progress_current,
            'progress_total': job.progress_total,
            'error_message': job.error_message,
            'created_at': job.created_at.isoformat(),
            'completed_at': job.completed_at.isoformat() if job.completed_at else None,
        }
        if job.status == ExportJob.Status.SUCCEEDED:
            response['download_url'] = reverse('exports:download', args=[job.public_id])
        if request.htmx:
            return render(
                request,
                'components/export_job_status.html',
                {'job': job, 'status_url': reverse('exports:job-status', args=[job.public_id])},
            )
        return JsonResponse(response)


class ExportJobCancelView(LoginRequiredMixin, View):
    def post(self, request, job_id):
        job = get_object_or_404(ExportJob, public_id=job_id, requested_by=request.user)
        if cancel_export_job(job, requested_by=request.user):
            messages.success(request, 'Export cancelled before rendering began.')
        else:
            messages.warning(request, 'This export has already started or reached a terminal state.')
        return redirect('courses:detail', course_id=job.course.public_id)


class ExportJobRetryView(LoginRequiredMixin, View):
    def post(self, request, job_id):
        job = get_object_or_404(ExportJob.objects.select_related('course'), public_id=job_id, requested_by=request.user)
        try:
            retry_export_job(job, requested_by=request.user)
        except ExportConfigurationError as exc:
            messages.error(request, '; '.join(exc.messages))
        else:
            messages.success(request, f'{job.get_export_format_display()} export retry has been queued.')
        return redirect('courses:detail', course_id=job.course.public_id)


class ExportDownloadView(LoginRequiredMixin, View):
    """Stream a private export only after checking job ownership."""

    def get(self, request, job_id):
        job = get_object_or_404(
            ExportJob.objects.select_related('export_file'),
            public_id=job_id,
            requested_by=request.user,
            status=ExportJob.Status.SUCCEEDED,
        )
        try:
            export_file = job.export_file
        except ExportJob.export_file.RelatedObjectDoesNotExist as exc:
            raise Http404('Export file is not available.') from exc
        return FileResponse(
            export_file.file.open('rb'),
            as_attachment=True,
            filename=export_file.original_filename,
            content_type=export_file.content_type,
        )
