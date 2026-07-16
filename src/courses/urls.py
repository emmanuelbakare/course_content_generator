from django.urls import path

from .views import CourseCreateView, CourseDetailView, CourseListView, CurriculumReviewView, ManualCurriculumRevisionView

app_name = 'courses'

urlpatterns = [
    path('', CourseListView.as_view(), name='list'),
    path('new/', CourseCreateView.as_view(), name='create'),
    path('<uuid:course_id>/', CourseDetailView.as_view(), name='detail'),
    path('<uuid:course_id>/curriculum/new/', ManualCurriculumRevisionView.as_view(), name='manual-curriculum'),
    path(
        '<uuid:course_id>/curriculum/<uuid:curriculum_id>/',
        CurriculumReviewView.as_view(),
        name='curriculum-review',
    ),
]
