"""Factory Boy factories for the course domain."""

import factory
from django.contrib.auth import get_user_model

from .models import Course, CourseSection, CurriculumVersion, Lesson, LessonRevision


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = get_user_model()
        django_get_or_create = ('username',)

    username = factory.Sequence(lambda number: f'author{number}')
    email = factory.LazyAttribute(lambda user: f'{user.username}@example.com')


class CourseFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Course

    owner = factory.SubFactory(UserFactory)
    title = factory.Sequence(lambda number: f'Course {number}')
    topic = 'Practical course authoring'


class CurriculumVersionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CurriculumVersion

    course = factory.SubFactory(CourseFactory)
    created_by = factory.SelfAttribute('course.owner')
    version_number = factory.Sequence(lambda number: number + 1)


class CourseSectionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CourseSection

    curriculum_version = factory.SubFactory(CurriculumVersionFactory)
    title = factory.Sequence(lambda number: f'Section {number}')
    duration_minutes = 60
    position = factory.Sequence(lambda number: number + 1)


class LessonFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Lesson

    section = factory.SubFactory(CourseSectionFactory)
    title = factory.Sequence(lambda number: f'Lesson {number}')
    duration_minutes = 60
    position = factory.Sequence(lambda number: number + 1)


class LessonRevisionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LessonRevision

    lesson = factory.SubFactory(LessonFactory)
    created_by = factory.LazyAttribute(
        lambda revision: revision.lesson.section.curriculum_version.course.owner
    )
    revision_number = factory.Sequence(lambda number: number + 1)
    content_markdown = '# Lesson content'
