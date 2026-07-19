import json

from django import forms
from django.core.exceptions import ValidationError

from .models import Course
from .services import LessonSpec, ProjectSpec, SectionSpec


class CourseCreateForm(forms.ModelForm):
    learning_outcomes_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 4, 'placeholder': 'One learning outcome per line'}),
        help_text='Enter one learning outcome per line.',
    )

    class Meta:
        model = Course
        fields = (
            'title', 'topic', 'target_audience', 'starting_level', 'target_completion_level',
            'language', 'delivery_mode',
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        progression_choices = [
            choice for choice in Course.Level.choices if choice[0] in Course.PROGRESSION_LEVELS
        ]
        self.fields['starting_level'].choices = progression_choices
        self.fields['starting_level'].required = True
        self.fields['starting_level'].initial = Course.Level.BEGINNER
        self.fields['starting_level'].help_text = 'The knowledge level learners have when they begin.'
        self.fields['target_completion_level'].choices = progression_choices
        self.fields['target_completion_level'].required = True
        self.fields['target_completion_level'].initial = Course.Level.BEGINNER
        self.fields['target_completion_level'].help_text = (
            'The knowledge level learners should reach by the end of the course.'
        )

    def clean(self):
        cleaned_data = super().clean()
        starting_level = cleaned_data.get('starting_level')
        target_level = cleaned_data.get('target_completion_level')
        if (
            starting_level in Course.LEVEL_RANKS
            and target_level in Course.LEVEL_RANKS
            and Course.LEVEL_RANKS[target_level] < Course.LEVEL_RANKS[starting_level]
        ):
            self.add_error(
                'target_completion_level',
                'The target completion level cannot be below the starting level.',
            )
        return cleaned_data

    def save(self, commit=True):
        course = super().save(commit=False)
        course.learning_outcomes = self.cleaned_data['learning_outcomes_text']
        course.level = (
            course.starting_level
            if course.starting_level == course.target_completion_level
            else Course.Level.MIXED
        )
        if commit:
            course.save()
        return course


class CurriculumRevisionForm(forms.Form):
    course_description = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))
    overall_learning_outcomes_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 4}),
        help_text='Enter one overall curriculum outcome per line.',
    )
    prerequisites = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))
    suggested_duration_minutes = forms.IntegerField(required=False, min_value=1)
    duration_estimate_explanation = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))
    project_json = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 12, 'spellcheck': 'false'}),
        help_text=(
            'Optional JSON object with title, description, deliverables, and evaluation_criteria. '
            'Saving creates a project on the new curriculum version only.'
        ),
    )
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

    def clean_overall_learning_outcomes_text(self):
        return [
            line.strip()
            for line in self.cleaned_data['overall_learning_outcomes_text'].splitlines()
            if line.strip()
        ]

    def clean_project_json(self):
        value = self.cleaned_data['project_json'].strip()
        if not value:
            return None
        try:
            project = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValidationError(f'Enter valid project JSON: {exc.msg}.') from exc
        if not isinstance(project, dict):
            raise ValidationError('Project JSON must be an object.')
        try:
            return ProjectSpec(
                title=_required_text(project, 'title', 'Project'),
                description=_required_text(project, 'description', 'Project'),
                deliverables=_string_list(project.get('deliverables', []), 'deliverables'),
                evaluation_criteria=_string_list(
                    project.get('evaluation_criteria', []), 'evaluation_criteria'
                ),
            )
        except (TypeError, KeyError, ValueError) as exc:
            raise ValidationError(str(exc)) from exc

    @property
    def project(self):
        return self.cleaned_data['project_json']


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


class LessonRevisionForm(forms.Form):
    content_markdown = forms.CharField(widget=forms.Textarea(attrs={'rows': 24, 'spellcheck': 'false'}))
    change_summary = forms.CharField(required=False, max_length=500)
