"""LLM configuration and auditable generation lifecycle models."""

import os
import re
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

ENVIRONMENT_VARIABLE_PATTERN = re.compile(r'^[A-Z_][A-Z0-9_]*$')


class LLMProvider(models.Model):
    class AdapterType(models.TextChoices):
        OPENAI = 'openai', 'OpenAI'
        ANTHROPIC = 'anthropic', 'Anthropic'
        GOOGLE_GENAI = 'google_genai', 'Google GenAI'
        OPENAI_COMPATIBLE = 'openai_compatible', 'OpenAI-compatible'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    adapter_type = models.CharField(max_length=30, choices=AdapterType.choices)
    api_key_environment_variable = models.CharField(max_length=100)
    base_url = models.URLField(blank=True)
    enabled = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('display_order', 'name')

    def __str__(self):
        return self.name

    def get_api_key(self) -> str:
        """Return a key from the process environment without reading secret files."""
        return os.environ.get(self.api_key_environment_variable, '')

    @property
    def is_api_key_configured(self) -> bool:
        return bool(self.get_api_key())

    def clean(self):
        super().clean()
        if not ENVIRONMENT_VARIABLE_PATTERN.fullmatch(self.api_key_environment_variable or ''):
            raise ValidationError(
                {'api_key_environment_variable': 'Use an uppercase environment-variable name.'}
            )
        if self.adapter_type == self.AdapterType.OPENAI_COMPATIBLE and not self.base_url:
            raise ValidationError({'base_url': 'OpenAI-compatible providers require a base URL.'})


class LLMModel(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    provider = models.ForeignKey(LLMProvider, on_delete=models.CASCADE, related_name='models')
    identifier = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    enabled = models.BooleanField(default=True)
    default_temperature = models.FloatField(
        default=0.7,
        validators=[MinValueValidator(0), MaxValueValidator(2)],
    )
    default_max_output_tokens = models.PositiveIntegerField(
        default=4000,
        validators=[MinValueValidator(1)],
    )
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('provider__display_order', 'display_order', 'identifier')
        constraints = [
            models.UniqueConstraint(
                fields=('provider', 'identifier'),
                name='unique_llm_model_identifier_per_provider',
            )
        ]

    def __str__(self):
        return self.display_name or self.identifier


class GenerationSettings(models.Model):
    """A singleton containing safe global defaults and generation limits."""

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    default_provider = models.ForeignKey(
        LLMProvider,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='default_generation_settings',
    )
    default_model = models.ForeignKey(
        LLMModel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='default_generation_settings',
    )
    default_temperature = models.FloatField(
        default=0.7,
        validators=[MinValueValidator(0), MaxValueValidator(2)],
    )
    max_output_tokens = models.PositiveIntegerField(default=4000, validators=[MinValueValidator(1)])
    curriculum_response_limit = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text='Leave blank to continue curriculum responses until completion, cancellation, or task timeout.',
    )
    max_continuations = models.PositiveSmallIntegerField(default=3)
    request_timeout_seconds = models.PositiveIntegerField(default=120, validators=[MinValueValidator(1)])
    max_retries = models.PositiveSmallIntegerField(default=3)
    daily_cost_budget = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0'))],
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'generation settings'
        verbose_name_plural = 'generation settings'

    def __str__(self):
        return 'Generation settings'

    def clean(self):
        super().clean()
        if self.default_model_id and not self.default_provider_id:
            raise ValidationError({'default_provider': 'Select the provider for the default model.'})
        if (
            self.default_model_id
            and self.default_provider_id
            and self.default_model.provider_id != self.default_provider_id
        ):
            raise ValidationError({'default_model': 'The default model must belong to the default provider.'})
        if self.default_provider_id and not self.default_provider.enabled:
            raise ValidationError({'default_provider': 'The default provider must be enabled.'})
        if self.default_model_id and not self.default_model.enabled:
            raise ValidationError({'default_model': 'The default model must be enabled.'})

    def save(self, *args, **kwargs):
        self.singleton_id = 1
        self.full_clean()
        return super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        settings, _ = cls.objects.get_or_create(singleton_id=1)
        return settings


class GenerationJob(models.Model):
    class JobType(models.TextChoices):
        CURRICULUM = 'curriculum', 'Curriculum'
        LESSON = 'lesson', 'Lesson'

    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        RUNNING = 'running', 'Running'
        RETRYING = 'retrying', 'Retrying'
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'
        NEEDS_REVIEW = 'needs_review', 'Needs review'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    course = models.ForeignKey('courses.Course', on_delete=models.CASCADE, related_name='generation_jobs')
    curriculum_version = models.ForeignKey(
        'courses.CurriculumVersion',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='generation_jobs',
    )
    lesson = models.ForeignKey(
        'courses.Lesson',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='generation_jobs',
    )
    provider = models.ForeignKey(LLMProvider, on_delete=models.PROTECT, related_name='generation_jobs')
    llm_model = models.ForeignKey(LLMModel, on_delete=models.PROTECT, related_name='generation_jobs')
    job_type = models.CharField(max_length=20, choices=JobType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    input_metadata = models.JSONField(default=dict, blank=True)
    progress_current = models.PositiveIntegerField(default=0)
    progress_total = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    cancellation_requested_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=('lesson',),
                condition=models.Q(
                    job_type='lesson',
                    status__in=('queued', 'running', 'retrying'),
                ),
                name='unique_active_lesson_generation_job',
            )
        ]

    def __str__(self):
        return f'{self.get_job_type_display()} job {self.public_id}'

    def clean(self):
        super().clean()
        errors = {}
        if not isinstance(self.input_metadata, dict):
            errors['input_metadata'] = 'Input metadata must be an object.'
        if self.llm_model_id and self.provider_id and self.llm_model.provider_id != self.provider_id:
            errors['llm_model'] = 'The selected model must belong to the selected provider.'
        if self.provider_id and not self.provider.enabled:
            errors['provider'] = 'Generation jobs require an enabled provider.'
        if self.llm_model_id and not self.llm_model.enabled:
            errors['llm_model'] = 'Generation jobs require an enabled model.'
        if self.curriculum_version_id and self.curriculum_version.course_id != self.course_id:
            errors['curriculum_version'] = 'The curriculum version must belong to the course.'
        if self.lesson_id:
            lesson_course_id = self.lesson.section.curriculum_version.course_id
            if lesson_course_id != self.course_id:
                errors['lesson'] = 'The lesson must belong to the course.'
        if self.job_type == self.JobType.CURRICULUM and self.lesson_id:
            errors['lesson'] = 'Curriculum jobs cannot target a lesson.'
        if self.job_type == self.JobType.LESSON and not self.lesson_id:
            errors['lesson'] = 'Lesson jobs require a lesson target.'
        if self.progress_total and self.progress_current > self.progress_total:
            errors['progress_current'] = 'Progress cannot exceed its total.'
        if errors:
            raise ValidationError(errors)


class GenerationAttempt(models.Model):
    class Status(models.TextChoices):
        STARTED = 'started', 'Started'
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    job = models.ForeignKey(GenerationJob, on_delete=models.CASCADE, related_name='attempts')
    provider = models.ForeignKey(LLMProvider, on_delete=models.PROTECT, related_name='generation_attempts')
    llm_model = models.ForeignKey(LLMModel, on_delete=models.PROTECT, related_name='generation_attempts')
    attempt_number = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.STARTED)
    prompt_template_version = models.CharField(max_length=100)
    request_metadata = models.JSONField(default=dict, blank=True)
    response_metadata = models.JSONField(default=dict, blank=True)
    response_preview = models.TextField(blank=True, default='')
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    estimated_cost = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-attempt_number',)
        constraints = [
            models.UniqueConstraint(
                fields=('job', 'attempt_number'),
                name='unique_generation_attempt_number_per_job',
            )
        ]

    def __str__(self):
        return f'{self.job} attempt {self.attempt_number}'

    def clean(self):
        super().clean()
        errors = {}
        if self.llm_model_id and self.provider_id and self.llm_model.provider_id != self.provider_id:
            errors['llm_model'] = 'The selected model must belong to the selected provider.'
        if self.job_id and self.provider_id and self.job.provider_id != self.provider_id:
            errors['provider'] = 'The attempt provider must match the job provider.'
        if self.job_id and self.llm_model_id and self.job.llm_model_id != self.llm_model_id:
            errors['llm_model'] = 'The attempt model must match the job model.'
        if not isinstance(self.request_metadata, dict):
            errors['request_metadata'] = 'Request metadata must be an object.'
        if not isinstance(self.response_metadata, dict):
            errors['response_metadata'] = 'Response metadata must be an object.'
        if errors:
            raise ValidationError(errors)
