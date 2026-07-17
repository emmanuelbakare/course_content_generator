from django.urls import path

from .action_views import (
    BatchLessonGenerationView,
    CurriculumCancelView,
    CurriculumRetryView,
    LessonCancelView,
    LessonGenerationView,
    LessonRetryView,
)
from .metrics_views import OperationalMetricsView
from .settings_views import GenerationSettingsView, ProviderModelsPartialView
from .views import GenerationJobStatusView

app_name = 'generation'

urlpatterns = [
    path('settings/', GenerationSettingsView.as_view(), name='settings'),
    path('metrics/', OperationalMetricsView.as_view(), name='operational-metrics'),
    path('settings/providers/<int:provider_id>/models/', ProviderModelsPartialView.as_view(), name='provider-models'),
    path('lessons/<uuid:lesson_id>/generate/', LessonGenerationView.as_view(), name='lesson-generate'),
    path(
        'courses/<uuid:course_id>/lessons/batch-generate/',
        BatchLessonGenerationView.as_view(),
        name='lesson-batch-generate',
    ),
    path('jobs/<uuid:job_id>/retry/', LessonRetryView.as_view(), name='lesson-retry'),
    path('jobs/<uuid:job_id>/cancel/', LessonCancelView.as_view(), name='lesson-cancel'),
    path('curriculum-jobs/<uuid:job_id>/retry/', CurriculumRetryView.as_view(), name='curriculum-retry'),
    path('curriculum-jobs/<uuid:job_id>/cancel/', CurriculumCancelView.as_view(), name='curriculum-cancel'),
    path('jobs/<uuid:job_id>/', GenerationJobStatusView.as_view(), name='job-status'),
]
