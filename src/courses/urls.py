from django.urls import path

from .views import (
    CourseCreateView,
    CourseDetailView,
    CourseListView,
    CourseWorkspaceView,
    CurriculumReviewView,
    LessonRevisionCreateView,
    ManualCurriculumRevisionView,
)

app_name = 'courses'

urlpatterns = [
    path('', CourseListView.as_view(), name='list'),
    path('new/', CourseCreateView.as_view(), name='create'),
    path('<uuid:course_id>/workspace/', CourseWorkspaceView.as_view(), name='workspace'),
    path('<uuid:course_id>/workspace/lessons/<uuid:lesson_id>/edit/', LessonRevisionCreateView.as_view(), name='lesson-edit'),
    path('<uuid:course_id>/', CourseDetailView.as_view(), name='detail'),
    path('<uuid:course_id>/curriculum/new/', ManualCurriculumRevisionView.as_view(), name='manual-curriculum'),
    path(
        '<uuid:course_id>/curriculum/<uuid:curriculum_id>/',
        CurriculumReviewView.as_view(),
        name='curriculum-review',
    ),
]
