from django.urls import path

from .settings_views import GenerationSettingsView, ProviderModelsPartialView
from .views import GenerationJobStatusView

app_name = 'generation'

urlpatterns = [
    path('settings/', GenerationSettingsView.as_view(), name='settings'),
    path('settings/providers/<int:provider_id>/models/', ProviderModelsPartialView.as_view(), name='provider-models'),
    path('jobs/<uuid:job_id>/', GenerationJobStatusView.as_view(), name='job-status'),
]
