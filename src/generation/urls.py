from django.urls import path

from .action_views import LessonCancelView, LessonGenerationView, LessonRetryView
from .settings_views import GenerationSettingsView, ProviderModelsPartialView
from .views import GenerationJobStatusView

app_name = 'generation'

urlpatterns = [
    path('settings/', GenerationSettingsView.as_view(), name='settings'),
    path('settings/providers/<int:provider_id>/models/', ProviderModelsPartialView.as_view(), name='provider-models'),
    path('lessons/<uuid:lesson_id>/generate/', LessonGenerationView.as_view(), name='lesson-generate'),
    path('jobs/<uuid:job_id>/retry/', LessonRetryView.as_view(), name='lesson-retry'),
    path('jobs/<uuid:job_id>/cancel/', LessonCancelView.as_view(), name='lesson-cancel'),
    path('jobs/<uuid:job_id>/', GenerationJobStatusView.as_view(), name='job-status'),
]
