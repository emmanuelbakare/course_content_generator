"""Structured LLM output contracts for generation orchestration."""

from typing import Any

from pydantic import BaseModel, Field


class CurriculumLessonOutput(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    duration_minutes: int = Field(gt=0)
    objectives: list[str] = Field(default_factory=list)
    outline: str = ''


class CurriculumSectionOutput(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    duration_minutes: int = Field(gt=0)
    lessons: list[CurriculumLessonOutput] = Field(min_length=1)
    summary: str = ''
    learning_outcomes: list[str] = Field(default_factory=list)


class CourseProjectOutput(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    deliverables: list[str] = Field(default_factory=list)
    evaluation_criteria: list[str] = Field(default_factory=list)


class CurriculumOutput(BaseModel):
    course_description: str = ''
    suggested_duration_minutes: int | None = Field(default=None, gt=0)
    sections: list[CurriculumSectionOutput] = Field(min_length=1)
    project: CourseProjectOutput | None = None


class LessonOutput(BaseModel):
    content_markdown: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
