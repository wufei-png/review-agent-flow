# Repository Guidelines

## Project Structure & Module Organization

Runtime code lives in `src/review_agent_flow/`; keep orchestration concerns separated by boundary (for example, `web.py`, `worker.py`, `store.py`, and `gitlab_client.py`). Tests mirror those modules under `tests/` as `test_<module>.py`. Treat `docs/spec/` as the executable specification, `docs/adr/` as architectural decisions, and `CONTEXT.md` as the canonical domain vocabulary. Deployment examples live in `deploy/`, while `examples/reviewflow.yml` demonstrates project policy.

## Build, Test, and Development Commands

Use Python 3.11 or newer:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
ruff check .
ruff format --check .
python -m review_agent_flow --help
```

The editable install provides the `review-agent-flow` CLI. `pytest` runs the configured quiet suite in `tests/`; `ruff check .` enforces static style rules and `ruff format --check .` enforces the repository-wide format baseline (Ruff 0.16.1, pinned in the dev extra). For a host-native smoke flow, follow `docs/quickstart-host-native.md` rather than inventing credentials or runtime defaults.

## Coding Style & Naming Conventions

Use four-space indentation, Python 3.11 syntax, type hints on public boundaries, and a 100-character line limit. Prefer small, explicit modules and dataclasses or typed contracts for persisted/external data. Name functions and modules `snake_case`, classes `PascalCase`, and constants `UPPER_SNAKE_CASE`. Preserve exact identity terms such as `ReviewKey`, `PublicationPlanV1`, and `RESULT_PATH`; update `CONTEXT.md`, specs, and ADRs when changing domain or architectural contracts.

## Testing Guidelines

Write pytest tests named `test_<behavior>` in the corresponding `tests/test_<module>.py`. Cover success, rejection, retry, crash-window, and idempotency behavior for storage or external-effect changes. There is no configured coverage percentage gate; regression tests and contract-boundary assertions are expected. Local tests mock integrations and do not prove live GitLab, notification, Agent-provider, systemd, or backup/restore behavior—state any unperformed evidence clearly.

## Security & Configuration

Never commit GitLab tokens, webhook secrets, operator credentials, SQLite data, or canonical publication-plan backups. Start from `deploy/*.example`, keep Agent environments allowlisted, and preserve the rule that ReviewFlow—not the Agent—owns GitLab Notes, commits, branches, merge requests, and notifications.

## Commit & Pull Request Guidelines

Recent history primarily uses Conventional Commit prefixes (`feat:`, `fix:`, `refactor:`); keep subjects imperative and scoped to one cohesive change. Pull requests should explain the behavioral or security contract, link relevant issues/ADRs, list verification commands, call out schema or operational impact, and distinguish automated tests from live-runtime evidence.
