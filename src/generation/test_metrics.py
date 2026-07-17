from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from courses.services import (
    LessonSpec,
    SectionSpec,
    create_curriculum_revision,
    create_draft_course,
)
from exports.models import ExportJob

from .metrics import build_operational_metrics
from .models import GenerationJob, LLMModel, LLMProvider


@override_settings(OPERATIONS_METRICS_DAYS=30, OPERATIONS_STUCK_JOB_MINUTES=30)
class OperationalMetricsTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.staff = get_user_model().objects.create_user('metrics-staff', password='safe', is_staff=True)
        self.author = get_user_model().objects.create_user('metrics-author', password='safe')
        self.course = create_draft_course(self.author, title='Metrics course', topic='Measure operations.')
        self.provider = LLMProvider.objects.create(
            name='Metrics provider',
            adapter_type=LLMProvider.AdapterType.OPENAI,
            api_key_environment_variable='METRICS_API_KEY',
        )
        self.model = LLMModel.objects.create(provider=self.provider, identifier='metrics-model')
        self.sections = [
            SectionSpec(
                title='Metrics section', duration_minutes=30,
                lessons=[LessonSpec(title='Metrics lesson', duration_minutes=30)],
            )
        ]

    def generation_job(self, *, job_type, status, curriculum=None, lesson=None, error_code=''):
        return GenerationJob.objects.create(
            course=self.course,
            curriculum_version=curriculum,
            lesson=lesson,
            provider=self.provider,
            llm_model=self.model,
            job_type=job_type,
            status=status,
            error_code=error_code,
        )

    def test_metrics_calculate_rates_time_proxy_and_stuck_jobs(self):
        generated_and_edited = create_curriculum_revision(self.course, sections=self.sections)
        create_curriculum_revision(self.course, sections=self.sections, change_summary='Edited after review')
        unedited = create_curriculum_revision(self.course, sections=self.sections)
        lesson = generated_and_edited.sections.get().lessons.get()
        curriculum_success = self.generation_job(
            job_type=GenerationJob.JobType.CURRICULUM,
            status=GenerationJob.Status.SUCCEEDED,
            curriculum=generated_and_edited,
        )
        curriculum_failed = self.generation_job(
            job_type=GenerationJob.JobType.CURRICULUM,
            status=GenerationJob.Status.FAILED,
            curriculum=unedited,
        )
        excluded = self.generation_job(
            job_type=GenerationJob.JobType.CURRICULUM,
            status=GenerationJob.Status.FAILED,
            error_code='provider_configuration_error',
        )
        self.generation_job(
            job_type=GenerationJob.JobType.LESSON,
            status=GenerationJob.Status.SUCCEEDED,
            lesson=lesson,
        )
        self.generation_job(
            job_type=GenerationJob.JobType.LESSON,
            status=GenerationJob.Status.FAILED,
            lesson=lesson,
        )
        for status in (ExportJob.Status.SUCCEEDED, ExportJob.Status.FAILED, ExportJob.Status.CANCELLED):
            ExportJob.objects.create(
                course=self.course,
                curriculum_version=generated_and_edited,
                requested_by=self.author,
                export_format=ExportJob.Format.MARKDOWN,
                status=status,
            )
        approved_course = create_draft_course(self.author, title='Approved timing', topic='Timing.')
        approved = create_curriculum_revision(approved_course, sections=self.sections, approve=True)
        approved_course.created_at = self.now - timedelta(minutes=20)
        approved_course.save(update_fields=('created_at',))
        CurriculumVersion = type(approved)
        CurriculumVersion.objects.filter(pk=approved.pk).update(created_at=self.now - timedelta(minutes=10))
        stuck_generation = self.generation_job(
            job_type=GenerationJob.JobType.CURRICULUM,
            status=GenerationJob.Status.RUNNING,
        )
        stuck_export = ExportJob.objects.create(
            course=self.course,
            curriculum_version=generated_and_edited,
            requested_by=self.author,
            export_format=ExportJob.Format.PDF,
            status=ExportJob.Status.QUEUED,
        )
        old_time = self.now - timedelta(minutes=31)
        GenerationJob.objects.filter(pk=stuck_generation.pk).update(created_at=old_time)
        ExportJob.objects.filter(pk=stuck_export.pk).update(created_at=old_time)
        GenerationJob.objects.filter(
            pk__in=[curriculum_success.pk, curriculum_failed.pk, excluded.pk]
        ).update(created_at=self.now - timedelta(days=1))

        metrics = build_operational_metrics(now=self.now)

        self.assertEqual(metrics['curriculum_success'], {'succeeded': 1, 'total': 2, 'percent': 50.0})
        self.assertEqual(metrics['lesson_success'], {'succeeded': 1, 'total': 2, 'percent': 50.0})
        self.assertEqual(metrics['export_success'], {'succeeded': 1, 'total': 3, 'percent': 33.3})
        self.assertEqual(metrics['approval_or_edit'], {'accepted': 1, 'total': 1, 'percent': 100.0})
        self.assertEqual(metrics['median_approval_minutes'], 10.0)
        self.assertEqual(metrics['approval_duration_count'], 1)
        self.assertEqual(list(metrics['stuck_generation_jobs']), [stuck_generation])
        self.assertEqual(list(metrics['stuck_export_jobs']), [stuck_export])

    def test_metrics_page_is_staff_only_and_explains_authorization_monitoring(self):
        url = reverse('generation:operational-metrics')
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(url).status_code, 403)

        self.client.force_login(self.staff)
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Operational metrics')
        self.assertContains(response, 'Authorization monitoring')
        self.assertContains(response, 'zero successful cross-owner access attempts')
