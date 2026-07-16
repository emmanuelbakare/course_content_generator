from django.urls import path

from .views import GenerationJobStatusView

app_name = 'generation'

urlpatterns = [
    path('jobs/<uuid:job_id>/', GenerationJobStatusView.as_view(), name='job-status'),
]
