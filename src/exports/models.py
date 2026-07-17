"""Private, user-owned course export records."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models


def private_export_upload_path(instance, filename):
    """Keep exports outside any public media URL namespace."""
    return f'private_exports/{instance.owner_id}/{instance.job.public_id}/{filename}'


class ExportJob(models.Model):
    class Format(models.TextChoices):
        MARKDOWN = 'markdown', 'Markdown'
        DOCX = 'docx', 'Word document'
        PDF = 'pdf', 'PDF'

    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        RUNNING = 'running', 'Running'
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    course = models.ForeignKey('courses.Course', on_delete=models.CASCADE, related_name='export_jobs')
    curriculum_version = models.ForeignKey(
        'courses.CurriculumVersion', on_delete=models.PROTECT, related_name='export_jobs'
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='export_jobs'
    )
    export_format = models.CharField(max_length=20, choices=Format.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    error_message = models.TextField(blank=True)
    progress_current = models.PositiveSmallIntegerField(default=0)
    progress_total = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.get_export_format_display()} export {self.public_id}'

    def clean(self):
        super().clean()
        errors = {}
        if self.course_id and self.requested_by_id and self.course.owner_id != self.requested_by_id:
            errors['requested_by'] = 'Only the course owner may request an export.'
        if self.curriculum_version_id:
            if self.curriculum_version.course_id != self.course_id:
                errors['curriculum_version'] = 'The curriculum version must belong to the course.'
            elif self.curriculum_version.status != 'approved':
                errors['curriculum_version'] = 'Only an approved curriculum version may be exported.'
        if self.progress_current > self.progress_total:
            errors['progress_current'] = 'Progress cannot exceed its total.'
        if errors:
            raise ValidationError(errors)


class ExportFile(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    job = models.OneToOneField(ExportJob, on_delete=models.CASCADE, related_name='export_file')
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='private_export_files'
    )
    file = models.FileField(upload_to=private_export_upload_path, max_length=500)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return self.original_filename

    def clean(self):
        super().clean()
        if self.owner_id and self.job_id and self.owner_id != self.job.requested_by_id:
            raise ValidationError({'owner': 'The file owner must match the export requester.'})
