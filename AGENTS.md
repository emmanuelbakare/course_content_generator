# AGENTS.md

## Project map

- `src/config/`: settings, URL root, Celery bootstrap, structured logging.
- `src/accounts/`: authentication shell and owner-scoped dashboard.
- `src/courses/`: course/curriculum/lesson domain, authoring views, Markdown rendering.
- `src/generation/`: provider configuration, adapters, job orchestration, Celery tasks.
- `src/exports/`: private background exports and protected download endpoints.

## Required engineering rules

1. Preserve `src/` as the Django project root.
2. Keep course data owner-scoped. Query objects through `course__owner=request.user` (or the equivalent) in every user-facing view and status/download endpoint.
3. API keys belong only in environment variables or the deployment secret manager. Never add a key field/form, return a key in JSON, or log one.
4. Do not call an LLM from views. Views create jobs through service boundaries; Celery tasks execute them.
5. Curriculum versions and lesson revisions are immutable. Create a new record instead of mutating approved/history content.
6. Exports are private. Store files under `private_exports/` and stream them only after authorization; never add a public media route.
7. Render Markdown through `courses.rendering.render_safe_markdown`; do not mark user/LLM HTML safe directly.
8. Update `user_manual.md` whenever a user-visible feature, workflow, status, permission, export behavior, or administrator procedure changes.

## Verification

From repository root, use the workspace virtual environment:

```powershell
.\venvi\Scripts\python.exe -m ruff check src
.\venvi\Scripts\python.exe -m mypy src
.\venvi\Scripts\python.exe src\manage.py makemigrations --check --dry-run
.\venvi\Scripts\python.exe -m pytest src --cov --cov-report=term-missing
```

For production setting changes, run `python manage.py check --deploy` with non-debug environment values. Use mocked/fake adapters in tests; do not make real provider calls.

Before a production release, run `python manage.py production_readiness` with production environment values after `collectstatic`. It must remain read-only with respect to provider APIs: validate configuration and connectivity only, never send an LLM request, and never print secrets or connection URLs.

## Editing and delivery

- Use `apply_patch` for source and documentation edits.
- Do not overwrite user changes or reset the worktree.
- Add migrations for model changes and test authorization paths for new endpoints.
- Run `collectstatic --noinput` before a production release; only production uses manifest static storage.
