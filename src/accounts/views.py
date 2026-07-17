from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView

from courses.models import Course
from exports.models import ExportJob
from generation.models import GenerationJob


class DashboardView(LoginRequiredMixin, TemplateView):
    """Authenticated activity dashboard scoped strictly to the signed-in author."""

    template_name = 'dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                'courses': Course.objects.filter(owner=self.request.user)[:6],
                'generation_jobs': GenerationJob.objects.filter(course__owner=self.request.user)[:6],
                'export_jobs': ExportJob.objects.filter(requested_by=self.request.user)[:6],
            }
        )
        return context
