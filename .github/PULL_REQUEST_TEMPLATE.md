## What this PR does

<!-- One or two sentences. Link the issue: Closes #___ -->

## Phase / build-plan reference

<!-- e.g. Phase 3B — clickstream backfill demonstration -->

## Checklist

- [ ] Slim CI passing (lint, parse, build, tests, contracts)
- [ ] No secrets, credentials, or `.env` values in the diff
- [ ] New models have tests (schema and/or singular) — not just a passing build
- [ ] If this touches a gold model: contract still enforced, no silent schema change
- [ ] If this touches quarantine/replay logic: tested against the checked-in poison batch, not just happy-path data
- [ ] Docs updated if this changes an architectural decision (ADR in `docs/data_modeling_decisions.md`)
- [ ] Self-reviewed the full diff, not just the files I expected to change

## Notes for reviewer (future me, or an actual reviewer)

<!-- Anything non-obvious: a tradeoff made, an assumption baked in, a known limitation -->
