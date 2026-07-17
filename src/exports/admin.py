from django.contrib import admin

from .models import ExportFile, ExportJob


@admin.register(ExportJob)
class ExportJobAdmin(admin.ModelAdmin):
    list_display = ('public_id', 'course', 'requested_by', 'export_format', 'status', 'created_at')
    list_filter = ('export_format', 'status')
    search_fields = ('course__title', 'requested_by__username')
    readonly_fields = ('public_id', 'created_at', 'started_at', 'completed_at')


@admin.register(ExportFile)
class ExportFileAdmin(admin.ModelAdmin):
    list_display = ('original_filename', 'owner', 'content_type', 'size_bytes', 'created_at')
    search_fields = ('original_filename', 'owner__username', 'job__course__title')
    readonly_fields = ('public_id', 'created_at', 'size_bytes')
