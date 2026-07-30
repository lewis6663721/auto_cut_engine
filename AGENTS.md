# Agent Development Entry

Before changing this project, read:

1. `docs/DEVELOPMENT_GUIDELINES.md`
2. `README.md`
3. `CHANGELOG.md`

Project-specific rules:

- Prompt changes must define role, input variables, hard constraints, output schema, and parsing/error-handling expectations.
- Any feature or behavior update must also update project documentation and `CHANGELOG.md`.
- Prefer deterministic hard-code or open-source fallback logic for scoring, parsing, ASR, media processing, and other user-facing automation. Use LLMs for explanation or capabilities that cannot be reliably implemented locally.
- Keep local development on `127.0.0.1:8000` unless the user explicitly asks otherwise.
