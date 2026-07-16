from celery import shared_task

from .models import GenerationSettings
from .services import (
    RetryableGenerationError,
    mark_job_failed,
    mark_job_retrying,
    process_generation_job,
)


@shared_task(bind=True, name='generation.run_generation_job')
def run_generation_job(self, job_id):
    """Run a job and delegate bounded retries to Celery."""
    try:
        job = process_generation_job(job_id)
        return {'job_id': job_id, 'status': job.status}
    except RetryableGenerationError as exc:
        settings = GenerationSettings.get_solo()
        if self.request.retries < settings.max_retries:
            mark_job_retrying(job_id, str(exc))
            raise self.retry(exc=exc, countdown=2 ** self.request.retries)
        job = mark_job_failed(job_id, 'provider_request_error', str(exc))
        return {'job_id': job_id, 'status': job.status}
