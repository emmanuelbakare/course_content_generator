from celery import shared_task

from .models import GenerationJob, GenerationSettings
from .services import (
    RetryableGenerationError,
    mark_job_failed,
    mark_job_retrying,
    process_generation_job,
    recover_stale_generation_jobs,
)


@shared_task(bind=True, name='generation.run_generation_job')
def run_generation_job(self, job_id):
    """Run a job and delegate bounded retries to Celery."""
    try:
        job = process_generation_job(job_id)
        return {'job_id': job_id, 'status': job.status}
    except GenerationJob.DoesNotExist:
        # A queued task can arrive after its job was cancelled and its course
        # permanently deleted. It is safe to discard that stale broker message.
        return {'job_id': job_id, 'status': 'discarded'}
    except RetryableGenerationError as exc:
        settings = GenerationSettings.get_solo()
        if self.request.retries < settings.max_retries:
            mark_job_retrying(job_id, str(exc))
            raise self.retry(exc=exc, countdown=2 ** self.request.retries)
        job = mark_job_failed(job_id, 'provider_request_error', str(exc))
        return {'job_id': job_id, 'status': job.status}
    except Exception:
        # Fail closed for database/orchestration defects too.  Otherwise an
        # exception after ``process_generation_job`` marks a job running can
        # strand it there permanently with no retry or owner-visible error.
        job = mark_job_failed(
            job_id,
            'generation_internal_error',
            'Generation stopped unexpectedly before the provider could complete the request. Please retry.',
        )
        return {'job_id': job_id, 'status': job.status}


@shared_task(name='generation.recover_stale_generation_jobs')
def recover_stale_generation_jobs_task():
    """Periodic recovery for orphaned running generation jobs."""
    return {'recovered': recover_stale_generation_jobs()}
