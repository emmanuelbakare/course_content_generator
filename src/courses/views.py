import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, OuterRef, Prefetch, Q, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, TemplateView

from generation.models import GenerationAttempt, GenerationJob
from generation.previews import parse_curriculum_preview
from generation.services import (
    GenerationConfigurationError,
    GenerationDispatchError,
    enqueue_curriculum_job,
    estimate_content_duration_minutes,
    recover_stale_generation_jobs,
    request_job_cancellation,
)

from .forms import CourseCreateForm, CurriculumRevisionForm, LessonRevisionForm
from .models import Course, CurriculumVersion, Lesson, LessonRevision
from .rendering import render_safe_markdown
from .services import (
    approve_curriculum_version,
    create_curriculum_revision,
    create_lesson_revision,
    restore_curriculum_version,
    restore_lesson_revision,
)


class OwnedCourseQuerysetMixin(LoginRequiredMixin):
    def get_course_queryset(self):
        return Course.objects.filter(owner=self.request.user)

    def get_course(self):
        return get_object_or_404(self.get_course_queryset(), public_id=self.kwargs['course_id'])


class CourseListView(LoginRequiredMixin, ListView):
    template_name = 'courses/course_list.html'
    context_object_name = 'courses'

    def get_queryset(self):
        approved_versions = CurriculumVersion.objects.filter(
            course=OuterRef('pk'),
            status=CurriculumVersion.Status.APPROVED,
        )
        latest_versions = CurriculumVersion.objects.filter(course=OuterRef('pk')).order_by(
            '-version_number'
        )
        return (
            Course.objects.filter(owner=self.request.user)
            .annotate(
                curriculum_version_count=Count('curriculum_versions', distinct=True),
                approved_lesson_total=Count(
                    'curriculum_versions__sections__lessons',
                    filter=Q(curriculum_versions__status=CurriculumVersion.Status.APPROVED),
                    distinct=True,
                ),
                completed_lesson_count=Count(
                    'curriculum_versions__sections__lessons',
                    filter=Q(
                        curriculum_versions__status=CurriculumVersion.Status.APPROVED,
                        curriculum_versions__sections__lessons__status__in=(
                            Lesson.Status.READY,
                            Lesson.Status.APPROVED,
                        ),
                    ),
                    distinct=True,
                ),
                active_duration_minutes=Subquery(
                    approved_versions.values('calculated_duration_minutes')[:1]
                ),
                proposed_duration_minutes=Subquery(
                    latest_versions.values('suggested_duration_minutes')[:1]
                ),
            )
        )


class CourseCreateView(LoginRequiredMixin, CreateView):
    template_name = 'courses/course_form.html'
    form_class = CourseCreateForm

    def form_valid(self, form):
        course = form.save(commit=False)
        course.owner = self.request.user
        course.status = Course.Status.PLANNING
        course.full_clean()
        course.save()
        try:
            enqueue_curriculum_job(course.pk)
        except GenerationConfigurationError:
            messages.warning(self.request, 'Course created. Configure an enabled default model before queuing curriculum planning.')
        except GenerationDispatchError:
            messages.warning(
                self.request,
                'Course created, but curriculum planning could not be queued. Check Redis and the Celery worker, then retry.',
            )
        else:
            messages.success(self.request, 'Course created. Curriculum planning has been queued.')
        return redirect('courses:detail', course_id=course.public_id)


class CourseDetailView(OwnedCourseQuerysetMixin, DetailView):
    template_name = 'courses/course_detail.html'
    context_object_name = 'course'

    def get_object(self, queryset=None):
        return self.get_course()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['curriculum_versions'] = self.object.curriculum_versions.prefetch_related(
            'sections__lessons', 'course_project'
        )
        generation_jobs = list(self.object.generation_jobs.filter(
            job_type=GenerationJob.JobType.CURRICULUM
        ).prefetch_related(
            Prefetch(
                'attempts',
                queryset=GenerationAttempt.objects.order_by('-attempt_number'),
                to_attr='activity_attempts',
            )
        )[:5])
        for job in generation_jobs:
            for attempt in job.activity_attempts:
                attempt.curriculum_preview = parse_curriculum_preview(attempt.response_preview)
        context['generation_jobs'] = generation_jobs
        context['export_jobs'] = self.object.export_jobs.select_related('export_file')[:5]
        return context


class CourseDeleteView(OwnedCourseQuerysetMixin, View):
    """Confirm and permanently delete an owner-scoped course and its private data."""

    template_name = 'courses/course_confirm_delete.html'
    active_generation_statuses = (
        GenerationJob.Status.QUEUED,
        GenerationJob.Status.RUNNING,
        GenerationJob.Status.RETRYING,
    )
    active_export_statuses = ('queued', 'running')

    def get(self, request, course_id):
        return render(request, self.template_name, self._context(self.get_course()))

    def post(self, request, course_id):
        course = self.get_course()
        if request.POST.get('action') == 'cancel_active_work':
            return self._cancel_active_work(request, course)
        context = self._context(course)
        if context['active_generation_count'] or context['active_export_count']:
            messages.error(
                request,
                'This course cannot be deleted while background generation or export work is active.',
            )
            return render(request, self.template_name, context, status=409)
        course_title = course.title
        # Database cascade removes curricula, lessons, revisions, jobs, attempts,
        # export records, and their authorization metadata. File storage is separate,
        # so remove private export files only after the database commit succeeds.
        from exports.models import ExportFile, ExportJob

        private_files = [
            (export_file.file.storage, export_file.file.name)
            for export_file in ExportFile.objects.filter(job__course=course).exclude(file='')
        ]
        with transaction.atomic():
            # ExportJob protects its approved curriculum snapshot, so remove
            # terminal export records first. Their ExportFile records cascade.
            ExportJob.objects.filter(course=course).delete()
            course.delete()

            def delete_private_files():
                for storage, name in private_files:
                    storage.delete(name)

            transaction.on_commit(delete_private_files)
        messages.success(request, f'Course "{course_title}" and its private content were permanently deleted.')
        return redirect('courses:list')

    def _cancel_active_work(self, request, course):
        """Request safe cancellation before allowing permanent deletion."""
        active_generation_jobs = list(
            course.generation_jobs.filter(status__in=self.active_generation_statuses)
        )
        for job in active_generation_jobs:
            request_job_cancellation(job)

        from exports.services import cancel_export_job

        queued_exports = list(course.export_jobs.filter(status='queued'))
        for job in queued_exports:
            cancel_export_job(job, requested_by=request.user)

        if active_generation_jobs or queued_exports:
            messages.success(
                request,
                'Cancellation requested. This page will refresh until it is safe to permanently delete the course.',
            )
        if course.export_jobs.filter(status='running').exists():
            messages.warning(
                request,
                'A running export cannot be interrupted safely. This page will enable deletion after it finishes.',
            )
        return redirect('courses:delete', course_id=course.public_id)

    def _context(self, course):
        # A worker crash can otherwise leave a cancelled request marked running
        # forever and prevent the owner from deleting the course.
        recover_stale_generation_jobs(course=course)
        active_generation_jobs = course.generation_jobs.filter(
            status__in=self.active_generation_statuses
        )
        active_generation_count = active_generation_jobs.count()
        queued_export_count = course.export_jobs.filter(status='queued').count()
        running_export_count = course.export_jobs.filter(status='running').count()
        active_export_count = queued_export_count + running_export_count
        return {
            'course': course,
            'active_generation_count': active_generation_count,
            'active_export_count': active_export_count,
            'queued_export_count': queued_export_count,
            'running_export_count': running_export_count,
            'generation_cancellation_requested_count': active_generation_jobs.exclude(
                cancellation_requested_at__isnull=True
            ).count(),
            'active_generation_jobs': active_generation_jobs,
            'can_cancel_active_work': bool(active_generation_count or queued_export_count),
            'can_delete': not active_generation_count and not active_export_count,
            'checked_at': timezone.localtime(),
        }


class CourseWorkspaceView(OwnedCourseQuerysetMixin, TemplateView):
    template_name = 'courses/workspace.html'

    def dispatch(self, request, *args, **kwargs):
        self.course = self.get_course()
        self.curriculum = self._get_curriculum()
        return super().dispatch(request, *args, **kwargs)

    def _get_curriculum(self):
        versions = self.course.curriculum_versions.prefetch_related(
            'sections__lessons__revisions',
            'sections__lessons__generation_jobs',
            'course_project',
        )
        return versions.filter(status=CurriculumVersion.Status.APPROVED).first() or versions.first()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        lessons = []
        if self.curriculum:
            for section in self.curriculum.sections.all():
                lessons.extend(section.lessons.all())
        latest_revisions = {}
        for lesson in lessons:
            revisions = list(lesson.revisions.all())
            revision = revisions[0] if revisions else None
            latest_revisions[lesson.pk] = revision
            estimate = (revision.metadata or {}).get('estimated_content_duration_minutes') if revision else None
            if revision and not isinstance(estimate, int):
                estimate = estimate_content_duration_minutes(revision.content_markdown)
            lesson.estimated_content_duration_minutes = estimate if isinstance(estimate, int) and estimate > 0 else None
        selected_lesson_id = self.request.GET.get('lesson')
        selected_lesson = next(
            (lesson for lesson in lessons if str(lesson.public_id) == selected_lesson_id),
            lessons[0] if lessons else None,
        )
        if selected_lesson_id and selected_lesson is None:
            raise Http404('Selected lesson is not part of this curriculum.')
        latest_revision = latest_revisions.get(selected_lesson.pk) if selected_lesson else None
        active_job = None
        if selected_lesson:
            active_job = next(
                (
                    job for job in selected_lesson.generation_jobs.all()
                    if job.status not in {'succeeded', 'failed', 'cancelled', 'needs_review'}
                ),
                None,
            )
        context.update(
            {
                'course': self.course,
                'curriculum': self.curriculum,
                'selected_lesson': selected_lesson,
                'latest_revision': latest_revision,
                'estimated_content_duration_minutes': (
                    selected_lesson.estimated_content_duration_minutes if selected_lesson else None
                ),
                'rendered_content': render_safe_markdown(latest_revision.content_markdown) if latest_revision else '',
                'revision_form': LessonRevisionForm(
                    initial={
                        'content_markdown': latest_revision.content_markdown if latest_revision else '',
                        'change_summary': 'Manual update',
                    }
                ) if selected_lesson else None,
                'active_job': active_job,
                'active_job_status_url': reverse('generation:job-status', args=[active_job.public_id]) if active_job else '',
                'terminal_lesson_jobs': [
                    job for job in (selected_lesson.generation_jobs.all() if selected_lesson else [])
                    if job.status in {'failed', 'cancelled', 'needs_review'}
                ],
            }
        )
        return context


class LessonRevisionCreateView(OwnedCourseQuerysetMixin, View):
    def post(self, request, course_id, lesson_id):
        course = self.get_course()
        lesson = get_object_or_404(
            Lesson.objects.select_related('section__curriculum_version__course'),
            public_id=lesson_id,
            section__curriculum_version__course=course,
        )
        form = LessonRevisionForm(request.POST)
        if form.is_valid():
            create_lesson_revision(
                lesson,
                created_by=request.user,
                content_markdown=form.cleaned_data['content_markdown'],
                change_summary=form.cleaned_data['change_summary'],
            )
            messages.success(request, 'Lesson revision saved.')
        else:
            messages.error(request, 'Lesson revision could not be saved.')
        return redirect(f'{reverse("courses:workspace", args=[course.public_id])}?lesson={lesson.public_id}')


class LessonRevisionDetailView(OwnedCourseQuerysetMixin, TemplateView):
    template_name = 'courses/lesson_revision_detail.html'

    def dispatch(self, request, *args, **kwargs):
        self.course = self.get_course()
        self.lesson = get_object_or_404(
            Lesson.objects.select_related('section__curriculum_version__course'),
            public_id=kwargs['lesson_id'],
            section__curriculum_version__course=self.course,
        )
        self.revision = get_object_or_404(self.lesson.revisions, public_id=kwargs['revision_id'])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                'course': self.course,
                'lesson': self.lesson,
                'revision': self.revision,
                'rendered_content': render_safe_markdown(self.revision.content_markdown),
            }
        )
        return context


class LessonRevisionRestoreView(OwnedCourseQuerysetMixin, View):
    def post(self, request, course_id, lesson_id, revision_id):
        course = self.get_course()
        lesson = get_object_or_404(
            Lesson.objects.select_related('section__curriculum_version__course'),
            public_id=lesson_id,
            section__curriculum_version__course=course,
        )
        source = get_object_or_404(LessonRevision.objects.filter(lesson=lesson), public_id=revision_id)
        try:
            restored = restore_lesson_revision(source, restored_by=request.user)
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
        else:
            messages.success(request, f'Created revision {restored.revision_number} from revision {source.revision_number}.')
        return redirect(f'{reverse("courses:workspace", args=[course.public_id])}?lesson={lesson.public_id}')


class CurriculumReviewView(OwnedCourseQuerysetMixin, TemplateView):
    template_name = 'courses/curriculum_review.html'

    def dispatch(self, request, *args, **kwargs):
        self.course = self.get_course()
        self.curriculum = get_object_or_404(
            self.course.curriculum_versions.prefetch_related('sections__lessons'),
            public_id=kwargs['curriculum_id'],
        )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({'course': self.course, 'curriculum': self.curriculum})
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get('action')
        if action == 'approve':
            try:
                approve_curriculum_version(self.curriculum, approved_by=request.user)
            except ValidationError as exc:
                messages.error(request, '; '.join(exc.messages))
            else:
                messages.success(request, 'Curriculum version approved.')
            return redirect('courses:curriculum-review', course_id=self.course.public_id, curriculum_id=self.curriculum.public_id)

        if action == 'request_revision':
            instruction = request.POST.get('revision_instruction', '').strip()
            if not instruction:
                messages.error(request, 'Enter instructions for the curriculum revision.')
            else:
                self.course.status = Course.Status.PLANNING
                self.course.save(update_fields=('status', 'updated_at'))
                try:
                    enqueue_curriculum_job(
                        self.course.pk,
                        revision_instruction=instruction,
                        source_curriculum_version_id=self.curriculum.pk,
                    )
                except GenerationConfigurationError:
                    messages.warning(request, 'Configure an enabled default model before queuing the revision.')
                except GenerationDispatchError:
                    messages.warning(
                        request,
                        'Curriculum revision could not be queued. Check Redis and the Celery worker, then retry.',
                    )
                else:
                    messages.success(request, 'Curriculum revision has been queued.')
            return redirect('courses:curriculum-review', course_id=self.course.public_id, curriculum_id=self.curriculum.public_id)

        messages.error(request, 'Choose a valid curriculum action.')
        return redirect('courses:curriculum-review', course_id=self.course.public_id, curriculum_id=self.curriculum.public_id)


class CurriculumComparisonView(OwnedCourseQuerysetMixin, TemplateView):
    template_name = 'courses/curriculum_comparison.html'

    def dispatch(self, request, *args, **kwargs):
        self.course = self.get_course()
        self.versions = self.course.curriculum_versions.prefetch_related(
            'sections__lessons', 'course_project'
        )
        self.left, self.right = self._selected_versions()
        return super().dispatch(request, *args, **kwargs)

    def _selected_versions(self):
        left_id = self.request.GET.get('left')
        right_id = self.request.GET.get('right')
        versions = list(self.versions)
        if len(versions) < 2:
            raise Http404('At least two curriculum versions are required for comparison.')
        left = get_object_or_404(self.versions, public_id=left_id) if left_id else versions[0]
        right = get_object_or_404(self.versions, public_id=right_id) if right_id else versions[1]
        if left.pk == right.pk:
            raise Http404('Choose two different curriculum versions.')
        return left, right

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        left = _curriculum_comparison_payload(self.left)
        right = _curriculum_comparison_payload(self.right)
        context.update(
            {
                'course': self.course,
                'versions': self.versions,
                'left': left,
                'right': right,
                'planning_rows': [
                    _comparison_row('Summary', left['description'], right['description']),
                    _comparison_row('Proposed duration', left['suggested_duration'], right['suggested_duration']),
                    _comparison_row('Calculated duration', left['calculated_duration'], right['calculated_duration']),
                    _comparison_row('Estimate explanation', left['estimate'], right['estimate']),
                    _comparison_row('Overall outcomes', left['outcomes'], right['outcomes']),
                    _comparison_row('Prerequisites', left['prerequisites'], right['prerequisites']),
                    _comparison_row('Course project', left['project'], right['project']),
                ],
                'section_rows': _comparison_section_rows(left['sections'], right['sections']),
            }
        )
        return context


class CurriculumRestoreView(OwnedCourseQuerysetMixin, View):
    def post(self, request, course_id, curriculum_id):
        course = self.get_course()
        source = get_object_or_404(
            course.curriculum_versions.prefetch_related('sections__lessons', 'course_project'),
            public_id=curriculum_id,
        )
        try:
            restored = restore_curriculum_version(source, restored_by=request.user)
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
            return redirect('courses:curriculum-review', course_id=course.public_id, curriculum_id=source.public_id)
        messages.success(request, f'Created draft curriculum version {restored.version_number} from version {source.version_number}.')
        return redirect('courses:curriculum-review', course_id=course.public_id, curriculum_id=restored.public_id)


class ManualCurriculumRevisionView(OwnedCourseQuerysetMixin, TemplateView):
    template_name = 'courses/manual_curriculum_revision.html'

    def dispatch(self, request, *args, **kwargs):
        self.course = self.get_course()
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        latest = self.course.curriculum_versions.prefetch_related(
            'sections__lessons', 'course_project'
        ).first()
        initial = {'suggested_duration_minutes': self.course.desired_duration_minutes}
        if latest:
            initial.update(
                {
                    'course_description': latest.course_description,
                    'overall_learning_outcomes_text': '\n'.join(latest.overall_learning_outcomes),
                    'prerequisites': latest.prerequisites,
                    'duration_estimate_explanation': latest.duration_estimate_explanation,
                    'suggested_duration_minutes': latest.suggested_duration_minutes,
                    'sections_json': json.dumps(_curriculum_payload(latest), indent=2),
                }
            )
            if hasattr(latest, 'course_project'):
                initial['project_json'] = json.dumps(_project_payload(latest.course_project), indent=2)
        else:
            initial.update(
                {
                    'overall_learning_outcomes_text': '\n'.join(self.course.learning_outcomes),
                    'prerequisites': self.course.prerequisites,
                }
            )
            initial['sections_json'] = json.dumps(_example_payload(), indent=2)
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['course'] = self.course
        context['form'] = kwargs.get('form', CurriculumRevisionForm(initial=self.get_initial()))
        return context

    def get(self, request, *args, **kwargs):
        return self.render_to_response(self.get_context_data())

    def post(self, request, *args, **kwargs):
        form = CurriculumRevisionForm(request.POST)
        if form.is_valid():
            try:
                curriculum = create_curriculum_revision(
                    self.course,
                    created_by=request.user,
                    sections=form.sections,
                    course_description=form.cleaned_data['course_description'],
                    overall_learning_outcomes=form.cleaned_data['overall_learning_outcomes_text'],
                    prerequisites=form.cleaned_data['prerequisites'],
                    suggested_duration_minutes=form.cleaned_data['suggested_duration_minutes'],
                    duration_estimate_explanation=form.cleaned_data['duration_estimate_explanation'],
                    change_summary=form.cleaned_data['change_summary'],
                    project=form.project,
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, 'Manual curriculum revision created for review.')
                return redirect(
                    'courses:curriculum-review',
                    course_id=self.course.public_id,
                    curriculum_id=curriculum.public_id,
                )
        return self.render_to_response(self.get_context_data(form=form))


def _curriculum_payload(curriculum):
    return [
        {
            'title': section.title,
            'summary': section.summary,
            'learning_outcomes': section.learning_outcomes,
            'duration_minutes': section.duration_minutes,
            'lessons': [
                {
                    'title': lesson.title,
                    'objectives': lesson.objectives,
                    'outline': lesson.outline,
                    'duration_minutes': lesson.duration_minutes,
                }
                for lesson in section.lessons.all()
            ],
        }
        for section in curriculum.sections.all()
    ]


def _example_payload():
    return [
        {
            'title': 'Course introduction',
            'summary': 'Introduce the topic, audience, and goals.',
            'learning_outcomes': ['Describe the course goals.'],
            'duration_minutes': 30,
            'lessons': [
                {
                    'title': 'Welcome and overview',
                    'objectives': ['Describe the course goals.'],
                    'outline': 'Introduce the course and learner expectations.',
                    'duration_minutes': 30,
                }
            ],
        }
    ]


def _project_payload(project):
    return {
        'title': project.title,
        'description': project.description,
        'deliverables': project.deliverables,
        'evaluation_criteria': project.evaluation_criteria,
    }


def _curriculum_comparison_payload(curriculum):
    project = None
    if hasattr(curriculum, 'course_project'):
        project = _project_payload(curriculum.course_project)
    return {
        'version': curriculum,
        'description': curriculum.course_description,
        'suggested_duration': curriculum.suggested_duration_minutes,
        'calculated_duration': curriculum.calculated_duration_minutes,
        'estimate': curriculum.duration_estimate_explanation,
        'outcomes': list(curriculum.overall_learning_outcomes),
        'prerequisites': curriculum.prerequisites,
        'project': project,
        'sections': [
            {
                'position': section.position,
                'title': section.title,
                'summary': section.summary,
                'duration': section.duration_minutes,
                'outcomes': list(section.learning_outcomes),
                'lessons': [
                    {
                        'position': lesson.position,
                        'title': lesson.title,
                        'duration': lesson.duration_minutes,
                        'objectives': list(lesson.objectives),
                        'outline': lesson.outline,
                    }
                    for lesson in section.lessons.all()
                ],
            }
            for section in curriculum.sections.all()
        ],
    }


def _comparison_row(label, left, right):
    return {'label': label, 'left': left, 'right': right, 'different': left != right}


def _comparison_section_rows(left_sections, right_sections):
    left_by_position = {section['position']: section for section in left_sections}
    right_by_position = {section['position']: section for section in right_sections}
    rows = []
    for position in sorted(set(left_by_position) | set(right_by_position)):
        left = left_by_position.get(position)
        right = right_by_position.get(position)
        lesson_positions = set()
        if left:
            lesson_positions.update(lesson['position'] for lesson in left['lessons'])
        if right:
            lesson_positions.update(lesson['position'] for lesson in right['lessons'])
        lessons = []
        left_lessons = {lesson['position']: lesson for lesson in left['lessons']} if left else {}
        right_lessons = {lesson['position']: lesson for lesson in right['lessons']} if right else {}
        for lesson_position in sorted(lesson_positions):
            left_lesson = left_lessons.get(lesson_position)
            right_lesson = right_lessons.get(lesson_position)
            lessons.append(
                {
                    'position': lesson_position,
                    'left': left_lesson,
                    'right': right_lesson,
                    'different': left_lesson != right_lesson,
                }
            )
        rows.append({'position': position, 'left': left, 'right': right, 'lessons': lessons, 'different': left != right})
    return rows
