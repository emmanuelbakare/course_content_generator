"""Read-only operational metrics derived from existing application records."""

from datetime import timedelta
from statistics import median

from django.conf import settings
from django.db.models import Exists, OuterRef, Q, Subquery
from django.utils import timezone

from courses.models import Course, CurriculumVersion
from exports.models import ExportJob

from .models import GenerationJob

GENERATION_TERMINAL_STATUSES = (
    GenerationJob.Status.SUCCEEDED,
    GenerationJob.Status.FAILED,
    GenerationJob.Status.CANCELLED,
    GenerationJob.Status.NEEDS_REVIEW,
)
EXPORT_TERMINAL_STATUSES = (
    ExportJob.Status.SUCCEEDED,
    ExportJob.Status.FAILED,
    ExportJob.Status.CANCELLED,
)
EXCLUDED_GENERATION_ERROR_CODES = {'provider_configuration_error', 'invalid_job_target'}


def build_operational_metrics(*, now=None) -> dict:
    """Return staff reporting data using one set of documented, bounded queries."""
    now = now or timezone.now()
    days = settings.OPERATIONS_METRICS_DAYS
    threshold_minutes = settings.OPERATIONS_STUCK_JOB_MINUTES
    window_start = now - timedelta(days=days)
    stuck_before = now - timedelta(minutes=threshold_minutes)
    terminal_generation_jobs = GenerationJob.objects.filter(
        created_at__gte=window_start,
        status__in=GENERATION_TERMINAL_STATUSES,
    ).exclude(error_code__in=EXCLUDED_GENERATION_ERROR_CODES)
    terminal_exports = ExportJob.objects.filter(
        created_at__gte=window_start,
        status__in=EXPORT_TERMINAL_STATUSES,
    )

    curriculum_jobs = terminal_generation_jobs.filter(job_type=GenerationJob.JobType.CURRICULUM)
    lesson_jobs = terminal_generation_jobs.filter(job_type=GenerationJob.JobType.LESSON)
    generated_curricula = CurriculumVersion.objects.filter(
        generation_jobs__job_type=GenerationJob.JobType.CURRICULUM,
        generation_jobs__status=GenerationJob.Status.SUCCEEDED,
        generation_jobs__created_at__gte=window_start,
    ).distinct().annotate(
        later_revision_exists=Exists(
            CurriculumVersion.objects.filter(source_version=OuterRef('pk'))
        )
    )
    first_approved_version = CurriculumVersion.objects.filter(
        course=OuterRef('pk'),
        status__in=(CurriculumVersion.Status.APPROVED, CurriculumVersion.Status.SUPERSEDED),
    ).order_by('created_at')
    approval_durations = [
        (course.first_approved_created_at - course.created_at).total_seconds() / 60
        for course in Course.objects.annotate(
            first_approved_created_at=Subquery(first_approved_version.values('created_at')[:1])
        ).filter(first_approved_created_at__gte=window_start)
        if course.first_approved_created_at and course.first_approved_created_at >= course.created_at
    ]
    stuck_generation_jobs = GenerationJob.objects.filter(
        status__in=(
            GenerationJob.Status.QUEUED,
            GenerationJob.Status.RUNNING,
            GenerationJob.Status.RETRYING,
        ),
        created_at__lt=stuck_before,
    ).select_related('course').order_by('created_at')
    stuck_export_jobs = ExportJob.objects.filter(
        status__in=(ExportJob.Status.QUEUED, ExportJob.Status.RUNNING),
        created_at__lt=stuck_before,
    ).select_related('course').order_by('created_at')

    return {
        'window_start': window_start,
        'window_end': now,
        'window_days': days,
        'stuck_threshold_minutes': threshold_minutes,
        'curriculum_success': _success_rate(curriculum_jobs),
        'lesson_success': _success_rate(lesson_jobs),
        'export_success': _success_rate(terminal_exports),
        'approval_or_edit': _approval_or_edit_rate(generated_curricula),
        'median_approval_minutes': median(approval_durations) if approval_durations else None,
        'approval_duration_count': len(approval_durations),
        'stuck_generation_jobs': stuck_generation_jobs,
        'stuck_export_jobs': stuck_export_jobs,
    }


def _success_rate(queryset) -> dict:
    total = queryset.count()
    succeeded = queryset.filter(status='succeeded').count()
    return {'succeeded': succeeded, 'total': total, 'percent': round((succeeded / total) * 100, 1) if total else None}


def _approval_or_edit_rate(queryset) -> dict:
    total = queryset.count()
    accepted = queryset.filter(
        Q(status=CurriculumVersion.Status.APPROVED) | Q(later_revision_exists=True)
    ).count()
    return {'accepted': accepted, 'total': total, 'percent': round((accepted / total) * 100, 1) if total else None}
