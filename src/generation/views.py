from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from .models import GenerationJob
from .previews import parse_curriculum_preview


class GenerationJobStatusView(LoginRequiredMixin, View):
    """Return safe, owner-authorized job progress for polling clients."""

    def get(self, request, job_id):
        job = get_object_or_404(
            GenerationJob.objects.select_related('course'),
            public_id=job_id,
            course__owner=request.user,
        )
        if request.htmx:
            latest_attempt = job.attempts.order_by('-attempt_number').first()
            return render(
                request,
                (
                    'components/curriculum_job_status.html'
                    if job.job_type == GenerationJob.JobType.CURRICULUM
                    else 'components/job_status.html'
                ),
                {
                    'job': job,
                    'latest_attempt': latest_attempt,
                    'curriculum_preview': parse_curriculum_preview(latest_attempt.response_preview)
                    if latest_attempt and job.job_type == GenerationJob.JobType.CURRICULUM else None,
                    'status_url': reverse('generation:job-status', args=[job.public_id]),
                },
            )
        return JsonResponse(
            {
                'id': str(job.public_id),
                'job_type': job.job_type,
                'status': job.status,
                'progress_current': job.progress_current,
                'progress_total': job.progress_total,
                'error_code': job.error_code,
                'error_message': job.error_message,
                'cancellation_requested': bool(job.cancellation_requested_at),
                'created_at': job.created_at.isoformat(),
                'started_at': job.started_at.isoformat() if job.started_at else None,
                'completed_at': job.completed_at.isoformat() if job.completed_at else None,
                'latest_attempt': _attempt_payload(job.attempts.order_by('-attempt_number').first()),
            }
        )


def _attempt_payload(attempt):
    if attempt is None:
        return None
    return {
        'number': attempt.attempt_number,
        'status': attempt.status,
        'started_at': attempt.started_at.isoformat(),
        'completed_at': attempt.completed_at.isoformat() if attempt.completed_at else None,
        'response_preview': attempt.response_preview,
    }
