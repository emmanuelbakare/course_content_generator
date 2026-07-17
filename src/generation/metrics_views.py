"""Staff-only read-only operational reporting."""

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import render
from django.views import View

from .metrics import build_operational_metrics


class OperationalMetricsView(LoginRequiredMixin, UserPassesTestMixin, View):
    raise_exception = True
    template_name = 'generation/operational_metrics.html'

    def test_func(self):
        return self.request.user.is_staff

    def get(self, request):
        return render(request, self.template_name, build_operational_metrics())
