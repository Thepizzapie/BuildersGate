<!-- Small and focused, please. See CONTRIBUTING.md. -->

## What and why

<!-- The reasoning, not the diff. What was wrong, and why this is the fix. -->

## Verification

- [ ] `python -m pytest -m "not slow" -q` passes locally
- [ ] New behaviour has a test
- [ ] If this touches `frontend/`, `npm run build` was run there and the
      regenerated `src/bgate_ui/static/` is in the same commit
- [ ] If this touches anything the wheel ships (`src/bgate_ui/static/`,
      `src/templates/`), the `wheel-smoke` CI job still passes
- [ ] Docs updated where they would otherwise become false

Platform tested on:
