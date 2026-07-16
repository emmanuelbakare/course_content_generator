"""Public orchestration boundary used by course authoring views.

Celery task implementation belongs to the generation-orchestration phase. Views
must call only this function and must not import provider adapters or task code.
"""


def enqueue_curriculum_job(course_id, revision_instruction=None):
    """Queue curriculum planning work for a course.

    This temporary boundary intentionally does not perform generation. The future
    task implementation will preserve this signature and return a GenerationJob.
    """
    return None
