from django.contrib import admin

from .models import GenerationAttempt, GenerationJob, GenerationSettings, LLMModel, LLMProvider


class LLMModelInline(admin.TabularInline):
    model = LLMModel
    extra = 0
    fields = ('identifier', 'display_name', 'enabled', 'default_temperature', 'display_order')
    ordering = ('display_order',)


@admin.register(LLMProvider)
class LLMProviderAdmin(admin.ModelAdmin):
    list_display = ('name', 'adapter_type', 'enabled', 'is_api_key_configured', 'display_order')
    list_filter = ('adapter_type', 'enabled')
    search_fields = ('name', 'api_key_environment_variable')
    readonly_fields = ('public_id', 'is_api_key_configured', 'created_at', 'updated_at')
    inlines = (LLMModelInline,)


@admin.register(LLMModel)
class LLMModelAdmin(admin.ModelAdmin):
    list_display = ('identifier', 'provider', 'enabled', 'default_temperature', 'default_max_output_tokens')
    list_filter = ('enabled', 'provider')
    search_fields = ('identifier', 'display_name')
    readonly_fields = ('public_id', 'created_at', 'updated_at')


@admin.register(GenerationSettings)
class GenerationSettingsAdmin(admin.ModelAdmin):
    list_display = ('default_provider', 'default_model', 'max_output_tokens', 'updated_at')

    def has_add_permission(self, request):
        return not GenerationSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class GenerationAttemptInline(admin.TabularInline):
    model = GenerationAttempt
    extra = 0
    can_delete = False
    readonly_fields = (
        'public_id', 'provider', 'llm_model', 'attempt_number', 'status',
        'prompt_template_version', 'request_metadata', 'response_metadata',
        'input_tokens', 'output_tokens', 'estimated_cost', 'error_code',
        'error_message', 'started_at', 'completed_at',
    )


@admin.register(GenerationJob)
class GenerationJobAdmin(admin.ModelAdmin):
    list_display = ('public_id', 'course', 'job_type', 'status', 'provider', 'llm_model', 'created_at')
    list_filter = ('job_type', 'status', 'provider')
    search_fields = ('course__title', 'public_id')
    readonly_fields = (
        'public_id', 'created_at', 'started_at', 'completed_at', 'cancellation_requested_at',
    )
    inlines = (GenerationAttemptInline,)


@admin.register(GenerationAttempt)
class GenerationAttemptAdmin(admin.ModelAdmin):
    list_display = ('job', 'attempt_number', 'status', 'provider', 'llm_model', 'started_at')
    list_filter = ('status', 'provider')
    search_fields = ('job__public_id',)
    readonly_fields = ('public_id', 'started_at')
