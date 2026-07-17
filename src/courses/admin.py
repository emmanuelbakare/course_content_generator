from django.contrib import admin

from .models import Course, CourseProject, CourseSection, CurriculumVersion, Lesson, LessonRevision


class CourseSectionInline(admin.TabularInline):
    model = CourseSection
    extra = 0
    fields = ('position', 'title', 'duration_minutes')
    ordering = ('position',)


class LessonInline(admin.TabularInline):
    model = Lesson
    extra = 0
    fields = ('position', 'title', 'duration_minutes', 'status')
    ordering = ('position',)


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ('title', 'owner', 'level', 'desired_duration_minutes', 'status', 'updated_at')
    list_filter = ('status', 'level', 'delivery_mode')
    search_fields = ('title', 'topic', 'owner__username')
    readonly_fields = ('public_id', 'created_at', 'updated_at')


@admin.register(CurriculumVersion)
class CurriculumVersionAdmin(admin.ModelAdmin):
    list_display = ('course', 'version_number', 'status', 'suggested_duration_minutes', 'calculated_duration_minutes', 'created_by', 'created_at')
    list_filter = ('status',)
    search_fields = ('course__title',)
    readonly_fields = ('public_id', 'calculated_duration_minutes', 'created_at')
    inlines = (CourseSectionInline,)


@admin.register(CourseSection)
class CourseSectionAdmin(admin.ModelAdmin):
    list_display = ('title', 'curriculum_version', 'position', 'duration_minutes')
    list_filter = ('curriculum_version__status',)
    search_fields = ('title', 'curriculum_version__course__title')
    ordering = ('curriculum_version', 'position')
    inlines = (LessonInline,)


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ('title', 'section', 'position', 'duration_minutes', 'status', 'updated_at')
    list_filter = ('status',)
    search_fields = ('title', 'section__curriculum_version__course__title')
    readonly_fields = ('public_id', 'created_at', 'updated_at')


@admin.register(LessonRevision)
class LessonRevisionAdmin(admin.ModelAdmin):
    list_display = ('lesson', 'revision_number', 'created_by', 'created_at')
    search_fields = ('lesson__title',)
    readonly_fields = ('public_id', 'created_at')


@admin.register(CourseProject)
class CourseProjectAdmin(admin.ModelAdmin):
    list_display = ('title', 'curriculum_version', 'updated_at')
    search_fields = ('title', 'curriculum_version__course__title')
    readonly_fields = ('public_id', 'created_at', 'updated_at')
