"""Safe presentation helpers for owner-visible, unapproved model output."""

import json

from pydantic import ValidationError

from .schemas import CurriculumOutput


def parse_curriculum_preview(response_text: str) -> CurriculumOutput | None:
    """Return a validated curriculum draft suitable for template rendering.

    Invalid or partial model output deliberately remains unformatted so callers
    can present it as escaped raw text for troubleshooting instead.
    """
    text = (response_text or '').strip()
    if text.startswith('```') and text.endswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    try:
        return CurriculumOutput.model_validate(json.loads(text))
    except (json.JSONDecodeError, TypeError, ValidationError):
        return None
