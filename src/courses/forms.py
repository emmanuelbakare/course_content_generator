import json

from django import forms
from django.core.exceptions import ValidationError

from .models import Course
from .services import LessonSpec, SectionSpec


class CourseCreateForm(forms.ModelForm):
    learning_outcomes_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 4, 'placeholder': 'One learning outcome per line'}),
        help_text='Enter one learning outcome per line.',
    )

    class Meta:
        model = Course
        fields = (
            'title', 'topic', 'target_audience', 'level', 'language', 'delivery_mode',
            'desired_duration_minutes', 'prerequisites', 'required_tools', 'constraints',
            'author_notes',
        )
        widgets = {
            'topic': forms.Textarea(attrs={'rows': 4}),
            'prerequisites': forms.Textarea(attrs={'rows': 3}),
            'required_tools': forms.Textarea(attrs={'rows': 3}),
            'constraints': forms.Textarea(attrs={'rows': 3}),
            'author_notes': forms.Textarea(attrs={'rows': 3}),
        }

    def clean_learning_outcomes_text(self):
        return [line.strip() for line in self.cleaned_data['learning_outcomes_text'].splitlines() if line.strip()]

    def save(self, commit=True):
        course = super().save(commit=False)
        course.learning_outcomes = self.cleaned_data['learning_outcomes_text']
        if commit:
            course.save()
        return course


class CurriculumRevisionForm(forms.Form):
    course_description = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))
    suggested_duration_minutes = forms.IntegerField(required=False, min_value=1)
    change_summary = forms.CharField(required=False, max_length=500)
    sections_json = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 24, 'spellcheck': 'false'}),
        help_text='Edit the ordered JSON array of sections and lessons. Positions follow array order.',
    )

    def clean_sections_json(self):
        try:
            sections = json.loads(self.cleaned_data['sections_json'])
        except json.JSONDecodeError as exc:
            raise ValidationError(f'Enter valid JSON: {exc.msg}.') from exc
        if not isinstance(sections, list) or not sections:
            raise ValidationError('Enter a JSON array containing at least one section.')

        specs = []
        for section_index, section in enumerate(sections, start=1):
            if not isinstance(section, dict):
                raise ValidationError(f'Section {section_index} must be an object.')
            lessons = section.get('lessons')
            if not isinstance(lessons, list) or not lessons:
                raise ValidationError(f'Section {section_index} must contain at least one lesson.')
            try:
                title = _required_text(section, 'title', f'Section {section_index}')
                duration = _positive_integer(section, 'duration_minutes', f'Section {section_index}')
                learning_outcomes = _string_list(section.get('learning_outcomes', []), 'learning_outcomes')
                lesson_specs = [
                    LessonSpec(
                        title=_required_text(lesson, 'title', f'Lesson {lesson_index}'),
                        duration_minutes=_positive_integer(lesson, 'duration_minutes', f'Lesson {lesson_index}'),
                        objectives=_string_list(lesson.get('objectives', []), 'objectives'),
                        outline=str(lesson.get('outline', '')).strip(),
                    )
                    for lesson_index, lesson in enumerate(lessons, start=1)
                ]
            except (TypeError, KeyError, ValueError) as exc:
                raise ValidationError(str(exc)) from exc
            specs.append(
                SectionSpec(
                    title=title,
                    duration_minutes=duration,
                    lessons=lesson_specs,
                    summary=str(section.get('summary', '')).strip(),
                    learning_outcomes=learning_outcomes,
                )
            )
        return specs

    @property
    def sections(self):
        return self.cleaned_data['sections_json']


def _required_text(value, key, label):
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object.')
    text = value.get(key)
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f'{label} requires a non-empty {key}.')
    return text.strip()


def _positive_integer(value, key, label):
    number = value.get(key) if isinstance(value, dict) else None
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError(f'{label} requires a positive integer {key}.')
    return number


def _string_list(value, label):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f'{label} must be an array of strings.')
    return [item.strip() for item in value if item.strip()]
