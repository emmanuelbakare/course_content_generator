"""Persistent course-authoring domain models."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class Course(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        PLANNING = 'planning', 'Planning'
        READY_FOR_REVIEW = 'ready_for_review', 'Ready for review'
        APPROVED = 'approved', 'Approved'
        ARCHIVED = 'archived', 'Archived'

    class Level(models.TextChoices):
        BEGINNER = 'beginner', 'Beginner'
        INTERMEDIATE = 'intermediate', 'Intermediate'
        ADVANCED = 'advanced', 'Advanced'
        MIXED = 'mixed', 'Mixed level'

    class DeliveryMode(models.TextChoices):
        INSTRUCTOR_LED = 'instructor_led', 'Instructor-led'
        SELF_PACED = 'self_paced', 'Self-paced'
        WORKSHOP = 'workshop', 'Workshop'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='courses',
    )
    title = models.CharField(max_length=255)
    topic = models.TextField()
    target_audience = models.CharField(max_length=255, blank=True)
    level = models.CharField(max_length=20, choices=Level.choices, default=Level.BEGINNER)
    language = models.CharField(max_length=20, default='en')
    delivery_mode = models.CharField(
        max_length=20,
        choices=DeliveryMode.choices,
        default=DeliveryMode.INSTRUCTOR_LED,
    )
    desired_duration_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
    )
    learning_outcomes = models.JSONField(default=list, blank=True)
    prerequisites = models.TextField(blank=True)
    required_tools = models.TextField(blank=True)
    constraints = models.TextField(blank=True)
    author_notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-updated_at', '-created_at')

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if not isinstance(self.learning_outcomes, list):
            raise ValidationError({'learning_outcomes': 'Learning outcomes must be a list.'})


class CurriculumVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        APPROVED = 'approved', 'Approved'
        SUPERSEDED = 'superseded', 'Superseded'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='curriculum_versions')
    source_version = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='derived_versions',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='created_curriculum_versions',
    )
    version_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    course_description = models.TextField(blank=True)
    overall_learning_outcomes = models.JSONField(default=list, blank=True)
    prerequisites = models.TextField(blank=True)
    suggested_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    calculated_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    duration_estimate_explanation = models.TextField(blank=True)
    revision_instruction = models.TextField(blank=True)
    change_summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-version_number',)
        constraints = [
            models.UniqueConstraint(
                fields=('course', 'version_number'),
                name='unique_curriculum_version_number_per_course',
            ),
            models.UniqueConstraint(
                fields=('course',),
                condition=Q(status='approved'),
                name='one_approved_curriculum_per_course',
            ),
        ]

    def __str__(self):
        return f'{self.course} — curriculum v{self.version_number}'

    def clean(self):
        super().clean()
        if self.created_by_id and self.course_id and self.created_by_id != self.course.owner_id:
            raise ValidationError({'created_by': 'The curriculum creator must own the course.'})
        if self.source_version_id and self.course_id and self.source_version.course_id != self.course_id:
            raise ValidationError({'source_version': 'The source version must belong to this course.'})
        if not isinstance(self.overall_learning_outcomes, list):
            raise ValidationError({'overall_learning_outcomes': 'Overall learning outcomes must be a list.'})
        if (
            self.suggested_duration_minutes
            and self.calculated_duration_minutes
            and self.suggested_duration_minutes != self.calculated_duration_minutes
        ):
            raise ValidationError(
                {'suggested_duration_minutes': 'The proposed duration must equal the calculated lesson total.'}
            )


class CourseSection(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    curriculum_version = models.ForeignKey(
        CurriculumVersion,
        on_delete=models.CASCADE,
        related_name='sections',
    )
    title = models.CharField(max_length=255)
    summary = models.TextField(blank=True)
    learning_outcomes = models.JSONField(default=list, blank=True)
    duration_minutes = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    position = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ('position', 'pk')
        constraints = [
            models.UniqueConstraint(
                fields=('curriculum_version', 'position'),
                name='unique_section_position_per_curriculum',
            )
        ]

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if not isinstance(self.learning_outcomes, list):
            raise ValidationError({'learning_outcomes': 'Learning outcomes must be a list.'})


class Lesson(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        QUEUED = 'queued', 'Queued for generation'
        GENERATING = 'generating', 'Generating'
        READY = 'ready', 'Ready for review'
        APPROVED = 'approved', 'Approved'
        FAILED = 'failed', 'Generation failed'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    section = models.ForeignKey(CourseSection, on_delete=models.CASCADE, related_name='lessons')
    title = models.CharField(max_length=255)
    objectives = models.JSONField(default=list, blank=True)
    outline = models.TextField(blank=True)
    duration_minutes = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    position = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('position', 'pk')
        constraints = [
            models.UniqueConstraint(
                fields=('section', 'position'),
                name='unique_lesson_position_per_section',
            )
        ]

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if not isinstance(self.objectives, list):
            raise ValidationError({'objectives': 'Objectives must be a list.'})


class LessonRevision(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='revisions')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='created_lesson_revisions',
    )
    revision_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    content_markdown = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    change_summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-revision_number',)
        constraints = [
            models.UniqueConstraint(
                fields=('lesson', 'revision_number'),
                name='unique_lesson_revision_number',
            )
        ]

    def __str__(self):
        return f'{self.lesson} — revision {self.revision_number}'

    def clean(self):
        super().clean()
        if not isinstance(self.metadata, dict):
            raise ValidationError({'metadata': 'Metadata must be an object.'})
        if self.created_by_id and self.lesson_id:
            owner_id = self.lesson.section.curriculum_version.course.owner_id
            if self.created_by_id != owner_id:
                raise ValidationError({'created_by': 'The revision creator must own the course.'})


class CourseProject(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    curriculum_version = models.OneToOneField(
        CurriculumVersion,
        on_delete=models.CASCADE,
        related_name='course_project',
    )
    title = models.CharField(max_length=255)
    description = models.TextField()
    deliverables = models.JSONField(default=list, blank=True)
    evaluation_criteria = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'course project'
        verbose_name_plural = 'course projects'

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        errors = {}
        if not isinstance(self.deliverables, list):
            errors['deliverables'] = 'Deliverables must be a list.'
        if not isinstance(self.evaluation_criteria, list):
            errors['evaluation_criteria'] = 'Evaluation criteria must be a list.'
        if errors:
            raise ValidationError(errors)
