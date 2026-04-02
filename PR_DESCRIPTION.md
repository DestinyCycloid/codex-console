# PR Description

## Summary

- add async account-management task routes for token refresh, token validation, subscription checks, and overview refresh
- add a dedicated Codex Auth workbench with batch audit, repair, generate, and export flows
- keep the three existing batch action buttons stable in idle state and document their hover help behavior
- fix local CodeRabbit review findings around domain-slot cleanup, DB rollback/session scope, mailbox binding, and review-doc secret handling

## User-Facing Changes

- the accounts page now exposes a separate `Codex Auth` entry button that opens a dedicated workbench modal
- the accounts table includes a `Codex Auth` state column
- Codex Auth workbench actions now support:
  - batch audit
  - batch repair
  - batch artifact generation
  - batch ZIP export
- async account operations now report task progress through dedicated task endpoints

## Verification

```bash
python3 -m py_compile src/web/routes/accounts.py src/web/routes/payment.py src/core/openai/codex_auth_workbench.py
node --check static/js/accounts.js
uv run python -m pytest -q tests/test_codex_auth_workbench.py tests/test_security_and_task_routes.py
```

Result:

```text
12 passed in 6.15s
```

## Real Dev Evidence

- isolated dev container: `codex-console-codex-auth-dev`
- dev web URL: `http://127.0.0.1:16668`
- copied 4 abnormal accounts into `data-dev` only: `53`, `64`, `65`, `71`
- batch audit result: `1 repairable`, `3 blocked by add-phone`
- batch repair result: account `53` repaired successfully; `64`, `65`, `71` remained blocked
- batch export returned a standard managed `auth.json` ZIP containing only the repaired account artifact

## Local CodeRabbit

- first pass produced actionable findings on:
  - domain-slot cleanup
  - pause timeout handling
  - SQLAlchemy rollback/session reuse
  - mailbox-to-service binding
  - review doc secret exposure
- all findings were fixed locally
- second pass result: `0 comments`
