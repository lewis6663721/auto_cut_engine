# Development Guidelines

## Required Reading

Every development session should start by reading this file, then `README.md` and `CHANGELOG.md`.

## Documentation Rule

When changing project behavior, routes, UI, task flows, prompts, model providers, deployment, or data contracts, update the relevant documentation in the same change. At minimum, update `CHANGELOG.md`; update `README.md` or files under `docs/` when user-facing behavior or architecture changes.

## Prompt Engineering Rule

Prompts are production contracts, not casual text. Any prompt used for a model call must include:

- Role and task boundary.
- Input variables and their meaning.
- Non-negotiable constraints.
- Safety and compliance boundaries.
- Exact output schema, preferably strict JSON.
- Rules for unavailable, uncertain, or unsupported data.
- Parsing expectations, including "return JSON only" when the backend parses JSON.

Backend code must not blindly trust model output. It should validate required fields, clamp numeric ranges, recompute formula-based fields, and provide deterministic fallback output when parsing fails.

For scoring features, hard-code or deterministic rule engines own the base score. LLMs may explain, summarize, or provide bounded adjustments only when the prompt and backend validation explicitly allow it.

## UI Input Rule

Avoid making users manually type structured values that can be selected. Use proper controls for dates, times, numeric ranges, toggles, file types, and provider/model choices. Long-running tools must show progress, step names, and completion state.

## Local Development

Use `127.0.0.1:8000` for local app testing unless the user explicitly requests another port.
