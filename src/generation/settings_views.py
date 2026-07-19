from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from .forms import GenerationSettingsForm, LLMModelForm, LLMProviderForm
from .models import GenerationSettings, LLMModel, LLMProvider


class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    raise_exception = True

    def test_func(self):
        return self.request.user.is_staff


class GenerationSettingsView(StaffRequiredMixin, View):
    template_name = 'generation/settings.html'
    tabs = ('generation', 'providers')

    def get(self, request):
        return render(request, self.template_name, self._context())

    def post(self, request):
        action = request.POST.get('action')
        if action == 'save_provider':
            return self._save_provider(request)
        if action == 'delete_provider':
            provider = get_object_or_404(LLMProvider, pk=request.POST.get('provider_id'))
            provider.delete()
            messages.success(request, 'Provider deleted.')
            return redirect('generation:settings')
        if action == 'save_model':
            return self._save_model(request)
        if action == 'delete_model':
            llm_model = get_object_or_404(LLMModel, pk=request.POST.get('model_id'))
            provider_id = llm_model.provider_id
            llm_model.delete()
            messages.success(request, 'Model deleted.')
            return redirect(f'{reverse("generation:settings")}?provider={provider_id}')
        if action == 'save_defaults':
            return self._save_defaults(request)
        if action == 'set_default_model':
            return self._set_default_model(request)
        messages.error(request, 'Choose a valid settings action.')
        return redirect('generation:settings')

    def _context(self, *, provider_form=None, model_form=None, settings_form=None, selected_provider=None):
        settings = GenerationSettings.get_solo()
        selected_provider = selected_provider or self._selected_provider(settings)
        editing_provider = self.request.GET.get('edit_provider')
        editing_model = self.request.GET.get('edit_model')
        if editing_provider and provider_form is None:
            provider_form = LLMProviderForm(
                instance=get_object_or_404(LLMProvider, pk=editing_provider)
            )
        if editing_model and model_form is None:
            editing_model_instance = get_object_or_404(LLMModel, pk=editing_model)
            selected_provider = editing_model_instance.provider
            model_form = LLMModelForm(instance=editing_model_instance, provider=selected_provider)
        return {
            'providers': LLMProvider.objects.prefetch_related('models').all(),
            'selected_provider': selected_provider,
            'provider_form': provider_form or LLMProviderForm(),
            'model_form': model_form or (LLMModelForm(provider=selected_provider) if selected_provider else None),
            'settings_form': settings_form or GenerationSettingsForm(
                instance=settings,
                selected_provider=settings.default_provider,
            ),
            'editing_provider': editing_provider,
            'editing_model': editing_model,
            'show_provider_form': bool(editing_provider or self.request.GET.get('new_provider')),
            'show_model_form': bool(editing_model or self.request.GET.get('new_model')),
            'active_tab': self._active_tab(),
        }

    def _active_tab(self):
        """Keep provider management visible for its existing query-string workflows."""
        requested_tab = self.request.GET.get('tab') or self.request.POST.get('tab')
        if requested_tab in self.tabs:
            return requested_tab
        if any(
            self.request.GET.get(parameter)
            for parameter in ('provider', 'new_provider', 'edit_provider', 'new_model', 'edit_model')
        ):
            return 'providers'
        if not GenerationSettings.get_solo().default_model_id and LLMProvider.objects.exists():
            return 'providers'
        return 'generation'

    def _selected_provider(self, settings):
        provider_id = self.request.GET.get('provider')
        if provider_id:
            provider = LLMProvider.objects.filter(pk=provider_id).first()
            if provider:
                return provider
        return settings.default_provider or LLMProvider.objects.first()

    def _save_provider(self, request):
        provider_id = request.POST.get('provider_id')
        instance = get_object_or_404(LLMProvider, pk=provider_id) if provider_id else None
        form = LLMProviderForm(request.POST, instance=instance)
        if form.is_valid():
            provider = form.save()
            messages.success(request, 'Provider saved.')
            return redirect(f'{reverse("generation:settings")}?provider={provider.pk}')
        return render(request, self.template_name, self._context(provider_form=form))

    def _save_model(self, request):
        provider = get_object_or_404(LLMProvider, pk=request.POST.get('provider_id'))
        model_id = request.POST.get('model_id')
        instance = get_object_or_404(LLMModel, pk=model_id, provider=provider) if model_id else None
        form = LLMModelForm(request.POST, instance=instance, provider=provider)
        if form.is_valid():
            form.save()
            messages.success(request, 'Model saved.')
            return redirect(f'{reverse("generation:settings")}?provider={provider.pk}')
        return render(request, self.template_name, self._context(model_form=form, selected_provider=provider))

    def _save_defaults(self, request):
        settings = GenerationSettings.get_solo()
        selected_provider = LLMProvider.objects.filter(pk=request.POST.get('default_provider')).first()
        form = GenerationSettingsForm(
            request.POST,
            instance=settings,
            selected_provider=selected_provider,
        )
        if form.is_valid():
            form.save()
            messages.success(request, 'Generation defaults saved.')
            return redirect(f'{reverse("generation:settings")}?tab=generation')
        return render(request, self.template_name, self._context(settings_form=form, selected_provider=selected_provider))

    def _set_default_model(self, request):
        llm_model = get_object_or_404(LLMModel, pk=request.POST.get('model_id'), enabled=True)
        if not llm_model.provider.enabled:
            messages.error(request, 'Enable the provider before selecting one of its models.')
        else:
            settings = GenerationSettings.get_solo()
            settings.default_provider = llm_model.provider
            settings.default_model = llm_model
            settings.save()
            messages.success(request, 'Default provider and model saved for course generation.')
        return redirect(f'{reverse("generation:settings")}?provider={llm_model.provider_id}')


class ProviderModelsPartialView(StaffRequiredMixin, View):
    template_name = 'generation/partials/provider_models.html'

    def get(self, request, provider_id):
        provider = get_object_or_404(LLMProvider, pk=provider_id, enabled=True)
        return render(
            request,
            self.template_name,
            {
                'selected_provider': provider,
                'models': provider.models.all(),
                'generation_settings': GenerationSettings.get_solo(),
            },
        )
