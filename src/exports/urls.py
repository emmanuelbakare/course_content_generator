from django.urls import path

from .views import CourseExportCreateView, ExportDownloadView, ExportJobStatusView

app_name = 'exports'

urlpatterns = [
    path('courses/<uuid:course_id>/', CourseExportCreateView.as_view(), name='create'),
    path('jobs/<uuid:job_id>/', ExportJobStatusView.as_view(), name='job-status'),
    path('jobs/<uuid:job_id>/download/', ExportDownloadView.as_view(), name='download'),
]
