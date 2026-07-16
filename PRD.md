# Product Requirements Document: Course Content Generator

## Document summary

| Item | Value |
| --- | --- |
| Product | Course Content Generator |
| Status | Draft |
| Primary users | Instructors, training teams, course authors |
| Product type | AI-assisted web authoring application |
| MVP platform | Django web application |

## 1. Product vision

Course Content Generator enables educators to turn a course idea into a structured, editable, instructor-ready course. It helps authors plan a curriculum that fits an intended duration, approve the plan, and generate detailed lesson materials without losing editorial control.

The product should make course creation faster while keeping the instructor responsible for accuracy, pedagogy, and final publication.

## 2. Problem statement

Creating a high-quality course involves several time-consuming tasks: scoping the topic, sequencing lessons, estimating teaching time, writing explanations and examples, preparing activities and assessments, and organizing materials for delivery. General-purpose AI tools can help with individual requests, but they do not reliably preserve course structure, track revisions, fit a duration, or provide a usable workspace for refining a complete course.

Authors need one workflow that converts a brief into an approved curriculum and detailed lesson content, while making generation progress, source material, revisions, and model configuration visible and controllable.

## 3. Goals

- Reduce the time needed to create a first course draft.
- Help authors create curricula appropriate to a stated audience, level, and duration.
- Produce coherent lesson packages that an instructor can adapt and teach.
- Give users explicit approval and editing control between planning and detailed generation.
- Make long-running AI generation reliable, observable, and resumable.
- Support several LLM providers without exposing API keys to end users.

## 4. Non-goals for MVP

- Automatically publishing courses to an LMS or marketplace.
- Replacing instructor review or guaranteeing factual correctness.
- Collaborative real-time editing and team workflows.
- Generating video lessons, slides, or images as finished media assets.
- File-based research/RAG, plagiarism detection, billing, and subscription management.
- Supporting public anonymous course creation.

## 5. Personas

### Independent instructor — Ada

Ada teaches practical technology courses. She knows the topic but spends many hours transforming her expertise into a coherent multi-session course. She needs a plan she can edit, detailed teaching notes, examples, exercises, and downloadable materials.

### Learning and development author — Tunde

Tunde designs internal training for a company. He must fit material into a fixed workshop length and tailor it to an audience with known prerequisites. He needs predictable duration allocation, consistent structure, and an audit trail of changes.

### Application administrator — Sam

Sam maintains the application. He configures approved LLM providers and models and wants to know whether a provider is ready without viewing or storing secrets in the product database.

## 6. User journeys

### Journey A: Create and approve a curriculum

1. An authenticated author creates a course brief with a topic, audience, level, learning outcomes, and optional duration.
2. The application generates a proposed curriculum with sections, lessons, duration allocation, prerequisites, and a suggested capstone project.
3. If no duration was entered, the application proposes one and explains the estimate.
4. The author reviews the plan and either accepts it, manually edits it, or requests a focused revision such as “reduce this to a four-hour workshop.”
5. The application stores the accepted curriculum as a versioned plan.

### Journey B: Generate and refine a lesson

1. From the course workspace, the author selects a lesson in the curriculum sidebar.
2. The author requests generation for that lesson or a set of selected lessons.
3. The application shows queued/running/completed/failed progress and allows retry or cancellation.
4. The generated lesson includes objectives, an instructor flow, explanations, examples, activities, assessments, and relevant project work.
5. The author edits the content, saves a revision, or requests a targeted revision without overwriting the previous version.

### Journey C: Configure generation

1. An administrator opens Generation Settings.
2. The administrator manages enabled providers and their available models.
3. The administrator selects defaults and safe generation limits.
4. The page reports whether the server has the provider key configured, but never reveals or accepts an API key in the normal UI.

### Journey D: Export course materials

1. The author selects an approved course and export format.
2. The application creates a background export job.
3. When complete, the author downloads a private Markdown, DOCX, or PDF export.

## 7. MVP requirements

### 7.1 Accounts and access

- Users must sign in to create or access courses.
- Each course, curriculum version, lesson, generation job, and export belongs to one user.
- Users can access only their own data.
- Administrative settings are restricted to staff users.

### 7.2 Course creation

The course brief must support:

- Course title/topic (required).
- Target audience and experience level.
- Learning outcomes.
- Desired duration in minutes, optional.
- Delivery format: instructor-led, self-paced, or workshop.
- Prerequisites, required tools, technology/framework constraints, and author notes.

The system must validate required inputs and create a draft course before starting generation.

### 7.3 Curriculum planning and review

- Generate an ordered curriculum proposal from a valid course brief.
- Include course summary, outcomes, prerequisites, sections, lessons, lesson objectives, and duration allocations.
- Honor a stated duration where feasible. When no duration is supplied, show the proposed duration.
- Show the calculated total and section/lesson duration breakdown.
- Let authors accept, manually edit, add, remove, and reorder sections or lessons.
- Let authors regenerate a plan or submit a focused revision instruction.
- Preserve prior approved and generated curriculum versions.

### 7.4 Lesson authoring

- Generate lessons on demand, individually or in a selected batch.
- Each lesson must include objectives, expected duration, preparation, a timed teaching flow, concepts/explanations, examples, activities, assessment/check-for-understanding, expected answers or rubric, and course-project linkage where relevant.
- Store generated lesson content in an editable, safely rendered format.
- Allow manual edits, targeted AI revisions, and revision history.
- Do not overwrite existing content unless the author explicitly chooses replace/revise.

### 7.5 Course workspace

- Provide a course list with title, status, completion progress, duration, and last update.
- Provide a workspace with curriculum tree/sidebar navigation and a lesson detail/editor area.
- Clearly show draft, approved, generating, failed, and completed states.
- Provide generation controls and actionable failure messages.

### 7.6 Generation reliability

- Run generation and exports in background jobs.
- Persist job status across browser refreshes.
- Support queued, running, retrying, succeeded, failed, cancelled, and needs-review states.
- Retry transient provider failures using a bounded retry policy.
- Support cancelling work between model calls.
- Use bounded output continuations to recover missing required content. The system must stop and request review when limits are reached; it must never generate indefinitely.

### 7.7 Provider and model settings

- Support a configurable provider and model catalog.
- Provide adapters for OpenAI, Anthropic, Google GenAI, and OpenAI-compatible APIs.
- Allow staff users to enable/disable providers and models, select defaults, and configure safe parameters such as temperature, timeout, retry count, and continuation limit.
- Read provider secrets only from server environment variables or a secret manager.
- Display configuration status without exposing secrets.

### 7.8 Export

- Allow authors to export approved course content as Markdown, DOCX, and PDF.
- Export jobs must run in the background and be private to the course owner.
- Exported output must retain curriculum order, lesson titles, and generated/manual content.

## 8. Content and quality requirements

- The planner must produce structured, schema-validated course data.
- The system must check that allocated lesson durations approximately total the stated or proposed duration.
- Lesson generation must be aware of the course, section, lesson objectives, and accepted curriculum context.
- Generated code and diagrams must be presented as editable content. Mermaid is the preferred MVP diagram format.
- Generated HTML/Markdown must be sanitized before rendering.
- Authors must be reminded to review content for correctness, licensing, accessibility, and suitability for their learners.

## 9. Success metrics

Track these after MVP release:

| Metric | Initial target |
| --- | --- |
| Curriculum generation success rate | 95% or higher, excluding invalid inputs/configuration |
| Curriculum approval or edit rate | 70% of generated curricula are accepted or edited rather than abandoned |
| Median time from course brief to approved curriculum | Under 10 minutes |
| Lesson generation completion rate | 95% or higher |
| Export success rate | 98% or higher |
| Unauthorized-resource access | Zero successful access attempts |
| Generation jobs left indefinitely running | Zero; all jobs reach a terminal state |

The product should also capture qualitative feedback on usefulness, factual accuracy, duration fit, and lesson teachability.

## 10. Product risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Inaccurate or unsuitable AI content | Require review, retain editability, show guidance, and support regeneration. |
| Course scope does not fit duration | Make duration a planning constraint, validate totals, and explain infeasible requests. |
| Expensive or endless generation | Use staged generation, per-stage limits, cost/timeout controls, and bounded continuations. |
| Provider failures | Use background jobs, retries, clear error state, and multiple configurable providers. |
| Sensitive data leakage | Keep API keys out of the database/UI/logs and enforce ownership checks. |
| Unsafe generated markup | Sanitize rendered content and constrain diagram rendering. |

## 11. Dependencies and assumptions

- Django provides authentication, authorization, persistence, and server-rendered UI.
- Redis and Celery are available for background jobs.
- An administrator configures at least one enabled model and its server-side API key before users generate content.
- The author owns or is authorized to use any source notes they provide.
- External model responses may be slow, incomplete, or temporarily unavailable; the UI must accommodate this.

## 12. Release criteria

MVP is ready for release when:

1. A user can create a brief, generate/review a duration-aware curriculum, and retain curriculum revisions.
2. A user can generate/edit lessons in the course workspace and see durable job progress.
3. An administrator can configure a provider/model without exposing secrets.
4. A user can export an approved course in all three MVP formats.
5. Authorization, job-state, schema-validation, provider-mocking, and export tests pass.
6. Production settings, error reporting, logging redaction, and deployment instructions are documented.

## 13. Open decisions

- Which user roles beyond author and administrator are required for the first production release?
- Should the product enforce per-user usage quotas/cost budgets in MVP, or only record estimated usage?
- Which course domains, languages, and accessibility standards should be prioritized?
- Should instructors be able to attach source documents in the first post-MVP release, and what copyright/privacy policy will govern them?
- What visual design system and branding should the initial interface use?

