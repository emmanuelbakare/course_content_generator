import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, TemplateView

from generation.services import GenerationConfigurationError, enqueue_curriculum_job

from .forms import CourseCreateForm, CurriculumRevisionForm
from .models import Course, CurriculumVersion
from .services import approve_curriculum_version, create_curriculum_revision


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
