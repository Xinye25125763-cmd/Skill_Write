# Skill Forge workbench integration

Read this only when a task will use the local Skill Forge service. The driver skill remains usable without the service.

## What the workbench currently does

Skill Forge is a local FastAPI/SQLite/Worker application. Its generator and evaluator are distinct from the host's native skill loader. A programmatic candidate-versus-baseline run can provide execution and assertion evidence, but it cannot prove that Codex selected the skill or loaded it in a real conversation.

The workbench supports requirement briefs, draft skill files, immutable hashed versions, execution traces, suites, candidate/baseline evaluation, and delivery receipts. Check the current OpenAPI schema and implementation before using any route because the source evolves.

## Local bridge

`app/generation/skill_bridge.py` exposes `forge_client.submit(request, base_url, token, project_id)`. It requires a non-empty `request_text` and posts the request to:

```text
POST {base_url}/api/projects/{project_id}/generation-flows
```

The bridge sends `x-session-token` as a request header and never puts it into the request body. Keep the token out of prompts, reports, generated files, shell history, and user-facing output. The normal local base URL is `http://127.0.0.1:8710`, but use the actual address printed by the user's running instance rather than assuming the port.

The bridge does not create a project, import a brief, poll a job, or install a generated skill. Those actions require their own verified routes or user interaction. Do not imply otherwise.

## Result interpretation

- Read the flow state and checkpoint/receipt from the actual API result; do not infer completion from a queued job.
- A candidate receipt is not an accepted delivery. Inspect each required verification dimension and its evidence.
- A successful executor evaluation does not substitute for a host check. Verify native selection/loading separately in the target host when that is part of the acceptance criteria.
- Preserve all skill files and user-authored documentation when packaging. Treat the package manifest and receipt as system evidence, not replacements for user content.

