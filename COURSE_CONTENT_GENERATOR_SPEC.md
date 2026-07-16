# Course Content Generator — Product and Technical Specification

## 1. Purpose

Build a Django web application that helps instructors create, review, revise, and export complete, teachable course materials from a topic brief. A course is generated in stages so the instructor approves the curriculum before the application spends time and cost generating detailed lessons.

The application is a content-authoring tool, not an autonomous publishing system. All generated material remains editable and requires user review.

## 2. Users and primary outcome

**Instructor / course author:** creates a course brief, approves its curriculum, generates lessons, revises content, and exports instructor-ready materials.

**Administrator:** configures supported LLM providers and models, default generation settings, and operational limits.

The primary outcome is an approved course containing a structured curriculum and detailed, usable instructor materials for every lesson.

## 3. Scope and release plan

### MVP

- Authentication and per-user course ownership.
- Course creation from a topic, audience, level, learning goals, preferred duration, and optional constraints/source notes.
- AI-generated curriculum proposal with suggested duration when none is supplied.
- Curriculum review: accept, regenerate, expand, reduce, and manually edit/reorder sections and lessons.
- On-demand lesson generation with instructor notes, explanations, examples, exercises, assessment questions, and a course project.
- A course workspace with a left curriculum navigation panel and an editable lesson detail pane.
- Resumable background generation with visible progress, errors, retry, and cancellation.
- Provider/model settings and server-side API-key configuration via environment variables.
- Markdown, DOCX, and PDF exports.

### Later releases (not MVP)

- Collaborative editing, comments, and role-based teams.
- LMS integrations (SCORM, Canvas, Moodle, etc.).
- RAG/document ingestion with citations and source management.
- Image generation, slide generation, and automatic video narration.
- Cost budgets/quotas and subscription billing.
- Multiple languages and localization.

## 4. Key product decisions

1. **Generate in stages.** First generate a curriculum, then obtain approval, then generate individual lessons. Do not generate an entire course in one request.
2. **Duration is a planning constraint.** A specified duration is allocated across lessons. If absent, the system proposes duration and shows its assumptions.
3. **Generation has a bounded continuation policy.** “No limit” is unsafe and cannot be guaranteed by an LLM. Each generation request has configurable per-step output, continuation, elapsed-time, and cost limits. The system continues only while a structured completeness check reports missing required sections; it then flags content for review instead of looping indefinitely.
4. **Structured data is the source of truth.** The model must return validated JSON for plans and lesson metadata. Rich editable lesson content is stored as sanitized Markdown (with optional structured blocks) and rendered safely.
5. **Provider configuration is server-owned.** API keys are read from environment variables or a secret manager, never entered into normal application forms, returned to browsers, stored in the database, or written to logs.

## 5. Functional requirements

### 5.1 Course brief and curriculum planning

The course-creation form must capture:

- title/topic (required), target audience, level, and language;
- intended learning outcomes;
- desired total duration in minutes (optional);
- delivery mode, such as instructor-led, self-paced, or workshop;
- constraints: prerequisites, tools, framework/version, regional context, and tone;
- optional author notes and source material references.

On submission, create a draft course and a `curriculum` generation job. The proposed curriculum must contain:

- course description, prerequisites, learning outcomes, assumed delivery mode, and total duration;
- ordered sections, each with outcomes and allocated duration;
- ordered lessons within each section, each with title, objectives, duration, and outline;
- a coherent capstone/project recommendation when appropriate;
- duration totals that match the chosen/proposed course duration within a configurable tolerance.

If the user supplies a duration, the planner must honor it or clearly explain an infeasible constraint. If no duration is supplied, it must propose one and explain the basis.

### 5.2 Curriculum review and revision

The user can accept the curriculum or request a targeted change: regenerate, reduce scope, expand scope, change the duration, change audience/level, or provide free-text instructions. Manual create/edit/delete/reorder of sections and lessons is supported.

Each accepted curriculum revision is versioned. Generating a new plan never silently overwrites an approved plan or lesson content. A user can compare versions and restore an earlier one.

### 5.3 Lesson content

For each lesson, generate an instructor-ready lesson package:

- learning objectives, duration, prerequisites, materials/tools, and preparation;
- timed teaching flow with instructor script/notes;
- concepts and explanations, worked examples, and code where relevant;
- activities, learner prompts, expected answers/outcomes, and common misconceptions;
- assessment/check-for-understanding questions with answers/rubric;
- diagrams or diagram specifications where useful;
- accessibility notes and references/source attribution when external sources are supplied;
- recommended relationship to the section and course capstone.

The author can generate one lesson, a selected set, or all ungenerated lessons. Existing content is not replaced without an explicit replace/revise action. The editor permits manual Markdown changes and stores a revision history.

Generated diagrams should use Mermaid when possible, because they are editable and safe to render. Image generation is deferred from MVP; image slots/specifications may be included in content for later fulfilment.

### 5.4 Course workspace and exports

The course list shows title, status, progress, updated time, and duration. The course workspace provides a curriculum tree/sidebar and a lesson detail/editor panel. It must expose generation state, revision actions, and retry/cancel controls.

Exports must include the approved curriculum, selected lesson content, and metadata. MVP export formats are Markdown, DOCX, and PDF. Export generation runs as a background job and creates a user-authorized downloadable file.

### 5.5 Generation settings

Adapt the useful provider/model pattern from the local Bible application:

- `LLMProvider`: display name, adapter type, API-key environment-variable name, optional base URL, enabled flag, and display order.
- `LLMModel`: provider, model identifier, display name, enabled flag, default temperature, and optional output-token setting.
- singleton `GenerationSettings`: default provider/model, default temperature, per-stage output/continuation limits, timeout, retry policy, and optional daily cost budget.

The settings page lets an administrator manage providers and models and select defaults. It must validate that a selected model belongs to its provider and display only whether a key is configured, never the key. Initial adapters should support OpenAI, Anthropic, Google GenAI, and OpenAI-compatible endpoints.

## 6. Data model

Implement these Django apps and primary entities:

| App | Entities | Responsibility |
| --- | --- | --- |
| `accounts` | User (or Django user initially) | Authentication and ownership. |
| `courses` | Course, CurriculumVersion, CourseSection, Lesson, LessonRevision, CourseProject | Authoring data and versioned curriculum. |
| `generation` | LLMProvider, LLMModel, GenerationSettings, GenerationJob, GenerationAttempt | LLM configuration, orchestration, progress, and audit metadata. |
| `exports` | ExportJob, ExportFile | Background export lifecycle and files. |

All course-facing entities require an owning user. Store lesson content as sanitized Markdown plus a JSON metadata field that conforms to a Pydantic schema. Use UUIDs for public-facing identifiers. Preserve the provider/model and prompt-template version used by every generation attempt, but never persist sensitive prompts or keys if they include secrets.

## 7. Generation architecture

Use Celery with Redis for long-running work. HTTP requests enqueue jobs and return promptly. The UI polls a job-status endpoint or uses server-sent events; it must not keep a Django request open for the full generation.

Generation pipeline:

1. Validate the course brief and selected provider/model.
2. Build a versioned stage prompt and call the provider through a provider-neutral adapter.
3. Parse and validate structured output with Pydantic.
4. Persist an immutable generation attempt and the resulting draft/revision atomically.
5. Run a deterministic completeness and duration check.
6. If a response was truncated or incomplete, request only the missing continuation, up to configured limits.
7. Mark the job `succeeded`, `failed`, `cancelled`, or `needs_review`, and publish progress events.

Job states: `queued`, `running`, `retrying`, `succeeded`, `failed`, `cancelled`, `needs_review`. Jobs must be idempotent, retry transient failures with exponential backoff, and support cooperative cancellation between model calls.

Do not use a generic “continue until enough” loop. Continuations must include the missing structured fields, a maximum count, and an explicit final failure/review state.

## 8. Quality, security, and operations

- Enforce authorization on every course, job, revision, export, and download.
- Use CSRF protection, secure cookies, allowed-host configuration, and production-safe Django settings.
- Sanitize generated Markdown/HTML; block unsafe URLs and scripts. Render Mermaid through a controlled client-side renderer or static image pipeline.
- Treat uploaded/reference material as a later feature unless file type, size, malware scanning, ownership, retention, and copyright policies are defined.
- Log job IDs, durations, provider/model, token/cost estimates, and errors without logging API keys or full sensitive user content.
- Add rate limits and per-user concurrency limits before public deployment.
- Test models, permissions, job state transitions, parsers, provider adapters (mocked), views, and export output. Run `ruff`, `mypy`, and pytest in CI.

## 9. Acceptance criteria for MVP

1. A signed-in user can create a course brief and receive a valid curriculum proposal.
2. Duration allocations match a supplied duration, or a proposed duration is displayed when omitted.
3. The user can revise/approve a curriculum without losing prior approved versions.
4. The user can generate, edit, regenerate, and view a lesson in the workspace.
5. A long generation survives page refresh, reports progress, retries safe transient failures, and never loops without a configured bound.
6. A user cannot access another user’s course or export.
7. An administrator can select an enabled provider/model; secrets stay outside the database and UI.
8. An approved course can be exported to Markdown, DOCX, and PDF.

## 10. Current repository assessment

The repository has a Django 6.0.7 project at `src/config` and requirements suitable for this direction (Celery, Redis, provider SDKs, Markdown sanitization, and document/PDF libraries). It does **not** yet contain Django apps, templates, static configuration, environment loading, Celery configuration, tests, or a generation implementation.

Before development, correct two setup issues:

- `.gitignore` currently contains accidental PowerShell here-string markers (`@"` and `"@`) and does not ignore the existing `venvi/` virtual environment. Replace it with a valid ignore file and add `venvi/`.
- `SECRET_KEY` is committed directly in `src/config/settings.py`; move it and other deployment settings to environment variables before deployment.

## 11. Parallel development prompt groups

Each group below is a self-contained implementation prompt. Groups in the same wave can run concurrently once their stated shared contract is agreed. Do not have two agents edit the same files.

### Wave 0 — shared contract (complete first)

**Prompt 0: Bootstrap architecture**

> Inspect the existing Django 6 project. Add environment-based settings, a `.env.example`, valid `.gitignore`, base template/static/media settings, and a Celery application configuration. Create empty `courses`, `generation`, and `exports` apps and register them. Do not implement business models yet. Preserve the existing project layout under `src`. Add a README section with local setup commands and verify `python manage.py check`.

### Wave 1 — run concurrently after Wave 0

**Prompt 1A: Course domain and admin**

> Implement the `courses` app data model from the specification: Course, CurriculumVersion, CourseSection, Lesson, LessonRevision, and CourseProject. Use UUID public IDs, user ownership, ordering fields, statuses, validation, Django admin, migrations, factories, and model tests. Define clean service-layer APIs for creating a draft course and creating immutable curriculum revisions. Do not build views or call an LLM.

**Prompt 1B: Generation configuration and provider adapters**

> Implement the `generation` app configuration models: LLMProvider, LLMModel, singleton GenerationSettings, GenerationJob, and GenerationAttempt. Add administration, migrations, validation, environment-only key lookup, and provider-neutral adapter interfaces for OpenAI, Anthropic, Google GenAI, and OpenAI-compatible APIs. Include mocked unit tests. Do not build course views or Celery tasks yet.

**Prompt 1C: Application shell and authentication**

> Build the authenticated application shell using Django templates and django-htmx: login/logout, a responsive base layout, navigation, messages, empty dashboard, and a reusable job-status UI component. Configure URLs and template conventions. Do not create course models, provider models, or generation tasks.

### Wave 2 — run concurrently after Wave 1 interfaces are merged

**Prompt 2A: Curriculum planning workflow**

> Build course creation, course list, curriculum review, and manual curriculum editing/reordering views for the existing `courses` models. Enforce ownership. Integrate only with a documented `enqueue_curriculum_job(course_id, revision_instruction=None)` service boundary; do not write LLM adapter internals. Add integration tests for the authoring workflow.

**Prompt 2B: Background generation orchestration**

> Build Celery tasks and service orchestration for curriculum and lesson generation using existing generation adapters and course service APIs. Validate Pydantic schemas, persist job/attempt state, implement bounded continuations, retries, cancellation, and a JSON status endpoint. Use mocked adapters in tests. Do not build HTML authoring pages.

**Prompt 2C: Settings UI**

> Build an administrator-only generation settings page modeled on the local Bible app’s provider/model selection flow. Support provider/model CRUD, enabled/default selection, model filtering by provider, and a safe “key configured/missing” indicator. Exclude all API-key entry/display. Add permission and validation tests.

### Wave 3 — run concurrently after Wave 2

**Prompt 3A: Lesson workspace and revisions**

> Build the course workspace: curriculum sidebar, lesson detail pane, safe Markdown rendering, lesson editing, revision history, and buttons to generate/revise/retry/cancel individual lessons through existing job endpoints. Enforce ownership and add end-to-end view tests.

**Prompt 3B: Exports**

> Implement the `exports` app and background export jobs for approved course content in Markdown, DOCX, and PDF. Use user-owned private files, job status, download authorization, and tests that inspect generated files. Do not change generation orchestration.

**Prompt 3C: Reliability and delivery**

> Add project-wide lint/type/test configuration, CI workflow, structured logging/redaction, security settings, error templates, and deployment documentation for Django + Celery + Redis. Verify the full test suite and `python manage.py check --deploy` with production environment values.

### Wave 4 — integration hardening (after Waves 1–3)

**Prompt 4: Integrate and verify**

> Integrate the completed Django apps. Resolve interface mismatches without changing product scope. Run migrations, test the full course lifecycle using a fake LLM provider, confirm ownership isolation and safe secret handling, and document any remaining production configuration required.

