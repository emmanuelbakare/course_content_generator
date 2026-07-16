from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from .models import GenerationJob


class GenerationJobStatusView(LoginRequiredMixin, View):
    """Return safe, owner-authorized job progress for polling clients."""

    def get(self, request, job_id):
        job = get_object_or_404(
            GenerationJob.objects.select_related('course'),
            public_id=job_id,
            course__owner=request.user,
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
            }
        )
