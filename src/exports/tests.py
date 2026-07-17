import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from docx import Document
from pypdf import PdfReader

from courses.factories import (
    CourseFactory,
    CourseSectionFactory,
    CurriculumVersionFactory,
    LessonFactory,
    LessonRevisionFactory,
    UserFactory,
)
from courses.models import Course, CurriculumVersion

from .models import ExportJob
from .services import ExportConfigurationError, enqueue_course_export, process_export_job


class ExportTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.media_root = tempfile.mkdtemp()
        self.media_settings = override_settings(MEDIA_ROOT=self.media_root)
        self.media_settings.enable()
        self.owner = UserFactory()
        self.course = CourseFactory(owner=self.owner, status=Course.Status.APPROVED, title='Practical Django')
        self.curriculum = CurriculumVersionFactory(
            course=self.course,
            created_by=self.owner,
            status=CurriculumVersion.Status.APPROVED,
            course_description='A concise course for working developers.',
        )
        section = CourseSectionFactory(curriculum_version=self.curriculum, title='Foundations')
        lesson = LessonFactory(section=section, title='Models and migrations')
        LessonRevisionFactory(
            lesson=lesson,
            created_by=self.owner,
            content_markdown='# Build a model\n\nUse migrations carefully.\n\n- Create the model\n- Run the migration',
        )

    def tearDown(self):
        self.media_settings.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)
        super().tearDown()

    def create_and_process(self, export_format):
        job = ExportJob.objects.create(
            course=self.course,
            curriculum_version=self.curriculum,
            requested_by=self.owner,
            export_format=export_format,
        )
        return process_export_job(job.public_id)

    def test_markdown_export_contains_course_and_lesson_content(self):
        job = self.create_and_process(ExportJob.Format.MARKDOWN)

        self.assertEqual(job.status, ExportJob.Status.SUCCEEDED)
        export_file = job.export_file
        content = Path(export_file.file.path).read_text(encoding='utf-8')
        self.assertIn('# Practical Django', content)
        self.assertIn('## Foundations', content)
        self.assertIn('Use migrations carefully.', content)
        self.assertTrue(export_file.file.name.startswith(f'private_exports/{self.owner.pk}/'))

    def test_docx_export_is_a_readable_word_document(self):
        job = self.create_and_process(ExportJob.Format.DOCX)

        document = Document(job.export_file.file.path)
        text = '\n'.join(paragraph.text for paragraph in document.paragraphs)
        self.assertIn('Practical Django', text)
        self.assertIn('Models and migrations', text)
        self.assertIn('Use migrations carefully.', text)
        self.assertEqual(job.export_file.content_type, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')

    def test_pdf_export_is_a_readable_pdf(self):
        job = self.create_and_process(ExportJob.Format.PDF)

        reader = PdfReader(job.export_file.file.path)
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
        self.assertGreater(len(reader.pages), 0)
        self.assertIn('Practical Django', text)
        self.assertIn('Models and migrations', text)
        self.assertEqual(job.export_file.content_type, 'application/pdf')

    @patch('exports.tasks.run_export_job.delay')
    def test_enqueue_uses_approved_snapshot_and_background_task(self, delay):
        job = enqueue_course_export(
            course_id=self.course.pk,
            requested_by=self.owner,
            export_format=ExportJob.Format.PDF,
        )

        self.assertEqual(job.curriculum_version, self.curriculum)
        delay.assert_called_once_with(str(job.public_id))

    def test_unapproved_curriculum_cannot_be_exported(self):
        self.curriculum.status = CurriculumVersion.Status.DRAFT
        self.curriculum.save(update_fields=('status',))

        with self.assertRaises(ExportConfigurationError):
            enqueue_course_export(
                course_id=self.course.pk,
                requested_by=self.owner,
                export_format=ExportJob.Format.MARKDOWN,
            )

    def test_download_and_status_require_requester_ownership(self):
        job = self.create_and_process(ExportJob.Format.MARKDOWN)
        self.client.force_login(self.owner)

        status = self.client.get(reverse('exports:job-status', args=[job.public_id]))
        download = self.client.get(reverse('exports:download', args=[job.public_id]))
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()['download_url'], reverse('exports:download', args=[job.public_id]))
        self.assertEqual(download.status_code, 200)
        self.assertIn('attachment;', download['Content-Disposition'])
        self.assertIn(b'Practical Django', b''.join(download.streaming_content))

        self.client.force_login(UserFactory())
        self.assertEqual(self.client.get(reverse('exports:job-status', args=[job.public_id])).status_code, 404)
        self.assertEqual(self.client.get(reverse('exports:download', args=[job.public_id])).status_code, 404)

    @patch('exports.tasks.run_export_job.delay')
    def test_create_view_rejects_a_course_owned_by_someone_else(self, delay):
        self.client.force_login(UserFactory())

        response = self.client.post(
            reverse('exports:create', args=[self.course.public_id]), {'export_format': 'pdf'}
        )

        self.assertEqual(response.status_code, 404)
        delay.assert_not_called()
