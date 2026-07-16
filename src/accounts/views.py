from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


class DashboardView(LoginRequiredMixin, TemplateView):
    """Authenticated landing page; course-specific content follows in a later phase."""

    template_name = 'dashboard.html'
