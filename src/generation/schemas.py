"""Structured LLM output contracts for generation orchestration."""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CurriculumSchemaModel(BaseModel):
    """Strict schema base so generated JSON has a predictable shape."""

    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')


class CurriculumLessonOutput(CurriculumSchemaModel):
    title: str = Field(min_length=1, max_length=255)
    duration_minutes: int = Field(gt=0)
    objectives: list[str] = Field(default_factory=list)
    outline: str = ''


class CurriculumSectionOutput(CurriculumSchemaModel):
    title: str = Field(min_length=1, max_length=255)
    duration_minutes: int = Field(gt=0)
    lessons: list[CurriculumLessonOutput] = Field(min_length=1)
    summary: str = ''
    learning_outcomes: list[str] = Field(default_factory=list)


class CourseProjectOutput(CurriculumSchemaModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    deliverables: list[str] = Field(default_factory=list)
    evaluation_criteria: list[str] = Field(default_factory=list)


class CurriculumOutput(CurriculumSchemaModel):
    course_description: str = ''
    overall_learning_outcomes: list[str] = Field(min_length=1)
    prerequisites: str = ''
    suggested_duration_minutes: int = Field(gt=0)
    duration_estimate_explanation: str = Field(min_length=1)
    sections: list[CurriculumSectionOutput] = Field(min_length=1)
    project: CourseProjectOutput | None = None

    @field_validator('course_description', 'prerequisites', 'duration_estimate_explanation', mode='before')
    @classmethod
    def normalize_line_list_to_text(cls, value):
        """Accept a conventional list of lines while persisting canonical text.

        Some providers naturally return prerequisites as a list.  It is safe to
        normalise a list made solely of strings; other invalid types remain a
        validation error rather than being silently coerced.
        """
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return '\n'.join(value)
        return value


class LessonPlanItem(BaseModel):
    """A named, explanatory item in an instructor lesson plan."""

    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)


class TimedTeachingFlowItem(LessonPlanItem):
    duration_minutes: int = Field(gt=0)


class LessonActivity(LessonPlanItem):
    expected_output: str = Field(min_length=1)


class LessonAssessment(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    check_for_understanding: str = Field(min_length=1)
    expected_answers_or_rubric: list[str] = Field(min_length=1)


class ProjectLinkage(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    project_title: str = Field(min_length=1, max_length=255)
    connection: str = Field(min_length=1)


class LessonOutput(BaseModel):
    """Complete, machine-validated source for one instructor-ready lesson."""

    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    objectives: list[str] = Field(min_length=1)
    expected_duration_minutes: int = Field(gt=0)
    preparation: list[str] = Field(min_length=1)
    materials: list[str] = Field(min_length=1)
    timed_teaching_flow: list[TimedTeachingFlowItem] = Field(min_length=1)
    concepts_explanations: list[LessonPlanItem] = Field(min_length=1)
    examples: list[LessonPlanItem] = Field(min_length=1)
    activities: list[LessonActivity] = Field(min_length=1)
    assessment: LessonAssessment
    common_misconceptions: list[LessonPlanItem] = Field(min_length=1)
    project_linkage: ProjectLinkage | None = None

    @model_validator(mode='after')
    def teaching_flow_matches_expected_duration(self):
        total = sum(step.duration_minutes for step in self.timed_teaching_flow)
        if total != self.expected_duration_minutes:
            raise ValueError('Timed teaching flow must total expected_duration_minutes.')
        return self
