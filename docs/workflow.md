# Engineering Workflow

This project follows a trunk-based, PR-gated workflow — the same shape a small platform team would run, scaled to a solo contributor.

## Branching

- `main` is protected: no direct pushes, no force-pushes, PR required to merge.
- Feature branches: `feat/<short-description>` (e.g. `feat/erp-ingestion`, `feat/identity-resolution`)
- Fix branches: `fix/<short-description>`
- Docs-only branches: `docs/<short-description>`
- CI/infra branches: `ci/<short-description>`, `infra/<short-description>`
- One branch per logical unit of work — a full build-plan phase is usually 2–4 branches/PRs, not one, so each PR stays reviewable.

## Commit Messages

Conventional, layer-prefixed (established convention from Projects 1 and 2):
`feat:`, `fix:`, `ci:`, `infra:`, `docs:`, `test:`, `release:`

## Pull Requests

Every PR:
1. Opens against `main` from a feature branch.
2. Triggers CI automatically (`.github/workflows/ci.yml`) — today that's SQL lint (`sqlfluff`) and `dbt parse` against a placeholder profile, nothing more. See "CI today vs. Slim CI (Phase 8)" below for what's not live yet.
3. Gets a self-review pass using the PR template checklist before merge — solo project, but the checklist stays honest: it's there to catch what a second reviewer would catch, not to be rubber-stamped.
4. Only merges once every CI check is green. No exceptions, no "I'll fix it after merge."

## CI today vs. Slim CI (Phase 8)

What's live now, in `.github/workflows/ci.yml`:
- `sqlfluff lint dbt/models` (jinja templater, snowflake dialect — no live warehouse needed).
- `dbt parse --project-dir dbt --profiles-dir ~/.dbt` against a placeholder `profiles.yml` generated inline in the workflow. `dbt parse` only needs the adapter to load, not a real connection, so no Snowflake credentials or GitHub Actions secrets are involved at this phase.

What Phase 8 adds (not implemented yet — don't infer any of this from the repo as it stands):
- `dbt build --select state:modified+ --defer` against real Snowflake credentials (via GitHub Actions secrets), so PRs only build what changed.
- dbt tests and contract enforcement running as part of that build.
- PR-scoped ephemeral schemas, keyed by PR number via a custom `generate_schema_name` macro — this macro does not exist in the repo yet.
- Artifact upload (`manifest.json`/`run_results.json`) so the next PR's CI run can defer to it.

## Issue Tracking

Each build-plan phase gets broken into GitHub Issues (one issue per PR-sized unit of work), tracked on a GitHub Project board with columns: `Backlog` → `In Progress` → `In Review` → `Done`. PRs reference the issue they close (`Closes #12`) so the board updates automatically on merge.

## Environment Promotion (ties to Phase 8, not yet implemented)

```
feat/* branch → PR → Slim CI → merge to main → auto-deploy to STAGE_ANALYTICS
                                                        ↓
                                          tagged release (v1.x) → GitHub Environments
                                          approval gate → PROD_ANALYTICS
```

This whole flow — including "Slim CI" as pictured above — is the Phase 8 target state. Today, CI is lint + parse only (see above), and there is no auto-deploy, no `STAGE_ANALYTICS`/`PROD_ANALYTICS` promotion, and no approval gate wired up.

## Rollback

Documented in `docs/data_modeling_decisions.md` (Phase 8): Snowflake Time Travel to a pre-deploy timestamp, or re-running `dbt build` against a prior git tag's manifest. A bad PROD promotion is not a "start over" event.

## Why this matters for the portfolio narrative

This isn't process for its own sake — it's the honest answer to "how did you actually build this," and it's the one thing a script-dump repo can't show: that the *process* used to build a reconciliation platform was itself disciplined, not just the end-state architecture.
