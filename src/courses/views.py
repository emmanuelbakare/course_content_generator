import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, TemplateView

from generation.services import GenerationConfigurationError, enqueue_curriculum_job

from .forms import CourseCreateForm, CurriculumRevisionForm, LessonRevisionForm
from .models import Course, CurriculumVersion, Lesson
from .rendering import render_safe_markdown
from .services import approve_curriculum_version, create_curriculum_revision, create_lesson_revision


class OwnedCourseQuerysetMixin(LoginRequiredMixin):
    def get_course_queryset(self):
        return Course.objects.filter(owner=self.request.user)

    def get_course(self):
        return get_object_or_404(self.get_course_queryset(), public_id=self.kwargs['course_id'])


class CourseListView(LoginRequiredMixin, ListView):
    template_name = 'courses/course_list.html'
    context_object_name = 'courses'

    def get_queryset(self):
        return Course.objects.filter(owner=self.request.user).prefetch_related('curriculum_versions')


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
        context['curriculum_versions'] = self.object.curriculum_versions.prefetch_related('sections__lessons')
        return context


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
        )
        return versions.filter(status=CurriculumVersion.Status.APPROVED).first() or versions.first()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        lessons = []
        if self.curriculum:
            for section in self.curriculum.sections.all():
                lessons.extend(section.lessons.all())
        selected_lesson_id = self.request.GET.get('lesson')
        selected_lesson = next(
            (lesson for lesson in lessons if str(lesson.public_id) == selected_lesson_id),
            lessons[0] if lessons else None,
        )
        if selected_lesson_id and selected_lesson is None:
            raise Http404('Selected lesson is not part of this curriculum.')
        latest_revision = selected_lesson.revisions.first() if selected_lesson else None
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
                    enqueue_curriculum_job(self.course.pk, revision_instruction=instruction)
                except GenerationConfigurationError:
                    messages.warning(request, 'Configure an enabled default model before queuing the revision.')
                else:
                    messages.success(request, 'Curriculum revision has been queued.')
            return redirect('courses:curriculum-review', course_id=self.course.public_id, curriculum_id=self.curriculum.public_id)

        messages.error(request, 'Choose a valid curriculum action.')
        return redirect('courses:curriculum-review', course_id=self.course.public_id, curriculum_id=self.curriculum.public_id)


class ManualCurriculumRevisionView(OwnedCourseQuerysetMixin, TemplateView):
    template_name = 'courses/manual_curriculum_revision.html'

    def dispatch(self, request, *args, **kwargs):
        self.course = self.get_course()
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        latest = self.course.curriculum_versions.prefetch_related('sections__lessons').first()
        initial = {'suggested_duration_minutes': self.course.desired_duration_minutes}
        if latest:
            initial.update(
                {
                    'course_description': latest.course_description,
                    'sections_json': json.dumps(_curriculum_payload(latest), indent=2),
                }
            )
        else:
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
                    suggested_duration_minutes=form.cleaned_data['suggested_duration_minutes'],
                    change_summary=form.cleaned_data['change_summary'],
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
