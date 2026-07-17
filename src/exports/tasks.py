from celery import shared_task

from .services import process_export_job


@shared_task(name='exports.run_export_job')
def run_export_job(job_id):
    job = process_export_job(job_id)
    return {'job_id': str(job.public_id), 'status': job.status}
