# CLAUDE.md

Project-level behavioral guidelines for Claude, Codex, and other coding agents working on AutoCut Engine.

Before changing this project, read:

1. `AGENTS.md`
2. `docs/DEVELOPMENT_GUIDELINES.md`
3. `README.md`
4. `CHANGELOG.md`

These guidelines reduce common LLM coding mistakes. They bias toward caution over speed; use judgment for trivial tasks.

## 1. Think Before Coding

Do not assume silently or hide confusion.

- State assumptions explicitly when they affect implementation.
- If multiple interpretations exist, surface the tradeoff before editing.
- If a simpler approach solves the request, say so.
- If something is unclear and risky, stop and ask.

## 2. Simplicity First

Write the minimum code that solves the real request.

- Do not add features beyond what was asked.
- Do not add abstractions for single-use code.
- Do not add configurability that was not requested.
- Avoid defensive branches for impossible scenarios.
- If an implementation is much larger than the problem, simplify it.

## 3. Surgical Changes

Touch only what the task requires.

- Do not improve adjacent code, comments, formatting, or naming unless needed.
- Do not refactor unrelated code.
- Match the existing style and local patterns.
- Mention unrelated dead code or risks instead of deleting them.
- Remove imports, variables, or functions made unused by your own changes.

Every changed line should trace back to the user's request.

## 4. Goal-Driven Execution

Turn work into verifiable goals and loop until verified.

- For validation changes, add or update invalid-input tests.
- For bug fixes, reproduce the bug with a focused check when practical.
- For refactors, verify behavior before and after when practical.
- For multi-step tasks, state a short plan with the expected verification.

These guidelines are working when diffs stay small, rewrites are rare, and questions happen before implementation mistakes.
