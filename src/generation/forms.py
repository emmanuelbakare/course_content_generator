from django import forms
from django.core.exceptions import ValidationError

from .models import GenerationSettings, LLMModel, LLMProvider


PROVIDER_KEY_ENVIRONMENT_VARIABLES = {
    LLMProvider.AdapterType.OPENAI: 'OPENAI_API_KEY',
    LLMProvider.AdapterType.ANTHROPIC: 'ANTHROPIC_API_KEY',
    LLMProvider.AdapterType.GOOGLE_GENAI: 'GOOGLE_API_KEY',
    LLMProvider.AdapterType.OPENAI_COMPATIBLE: 'OPENAI_COMPATIBLE_API_KEY',
}


class LLMProviderForm(forms.ModelForm):
    """Provider form deliberately excludes API-key and key-variable entry."""

    class Meta:
        model = LLMProvider
        fields = ('name', 'adapter_type', 'base_url', 'enabled', 'display_order')

    def clean(self):
        cleaned_data = super().clean()
        adapter_type = cleaned_data.get('adapter_type')
        if adapter_type and (
            not self.instance.pk or self.initial.get('adapter_type') != adapter_type
        ):
            # ModelForm runs model validation before save(), so derive the
            # server-only variable name here without exposing it as a form field.
            self.instance.api_key_environment_variable = PROVIDER_KEY_ENVIRONMENT_VARIABLES[adapter_type]
        return cleaned_data

    def save(self, commit=True):
        provider = super().save(commit=False)
        adapter_changed = self.initial.get('adapter_type') != provider.adapter_type
        if not provider.pk or adapter_changed:
            provider.api_key_environment_variable = PROVIDER_KEY_ENVIRONMENT_VARIABLES[provider.adapter_type]
        provider.full_clean()
        if commit:
            provider.save()
        return provider


class LLMModelForm(forms.ModelForm):
    class Meta:
        model = LLMModel
        fields = (
            'identifier', 'display_name', 'enabled', 'default_temperature',
            'default_max_output_tokens', 'display_order',
        )

    def __init__(self, *args, provider, **kwargs):
        self.provider = provider
        super().__init__(*args, **kwargs)

    def save(self, commit=True):
        llm_model = super().save(commit=False)
        llm_model.provider = self.provider
        llm_model.full_clean()
        if commit:
            llm_model.save()
        return llm_model


class GenerationSettingsForm(forms.ModelForm):
    class Meta:
        model = GenerationSettings
        fields = (
            'default_provider', 'default_model', 'default_temperature',
            'max_output_tokens', 'max_continuations', 'request_timeout_seconds',
            'max_retries', 'daily_cost_budget',
        )

    def __init__(self, *args, selected_provider=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['default_provider'].queryset = LLMProvider.objects.filter(enabled=True)
        provider = selected_provider or self.instance.default_provider
        self.fields['default_model'].queryset = LLMModel.objects.filter(
            enabled=True,
            provider=provider,
        ) if provider else LLMModel.objects.none()

    def clean(self):
        cleaned_data = super().clean()
        provider = cleaned_data.get('default_provider')
        model = cleaned_data.get('default_model')
        if model and provider and model.provider_id != provider.pk:
            self.add_error('default_model', 'Choose a model belonging to the selected provider.')
        return cleaned_data

    def save(self, commit=True):
        settings = super().save(commit=False)
        settings.full_clean()
        if commit:
            settings.save()
        return settings
