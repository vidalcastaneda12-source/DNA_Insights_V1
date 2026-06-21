Run a pre-PR readiness checklist against the change on this branch and report what's
green, what's missing, and what blocks opening (or merging) the PR.

This is the in-loop dry-run of the merge-gate contract — it does **not** replace VSC-User's
independent `docs/runbooks/verification.md` run; it makes sure nothing obvious is missing
before the human spends attention. Report status; fix only the trivial, unambiguous gaps
(a missing CHANGELOG line) and escalate the rest.

## Gather facts from git/gh, not memory

- Branch + base: `git rev-parse --abbrev-ref HEAD`; the branch this targets.
- Diff: `git diff --name-only main..HEAD` and the per-file diff.
- Open PR, if any: `gh pr view --json number,url,isDraft,title --jq .`.

## The checklist

1. **Dev-loop green** (CLAUDE.md "How to run") — for any Python change, confirm:
   `pytest` · `ruff check` · `ruff format --check` · `mypy --strict backend/src`. Report
   each pass/fail with the first actionable error. (No Python changed → state that the
   Python checks are unaffected; do not claim a run you didn't do.)
2. **Tests for behavior** — every behavior change has a covering test; anchors in §6 have
   regression-anchor tests; predicted surprises have guard tests.
3. **CHANGELOG** — a `[Unreleased]` entry exists if behavior / schema / deps / build
   changed (run `/changelog` to add it if missing — that's a trivial fix you may apply).
4. **Schema discipline** — if `docs/schemas/`|`ddl/` changed, it was a deliberate,
   documented change with the rebuild protocol followed; `notes_fts` intact. Otherwise no
   schema file is touched.
5. **Privacy** — no raw export / `data/` staged; no genome PHI in logs; external calls go
   through the audited client; no stored payload body; no embedded secret.
6. **Provenance / supersession** — durable rows carry provenance; no UPDATE of active
   insight/evidence; supersession used where required.
7. **Anchors-to-watch** — if anchors are at risk, the list with **expected values** is
   ready for the gate (from `regression-hunter` / the pre-mortem).
8. **No survivors** — no `GATE-FILL`, `TODO`, or debug `print()` left in the diff.
9. **Commit hygiene** — commits are by explicit path (no bulk `git add -A`); messages are
   descriptive; the branch is the designated feature branch, not `main`.

## Output

A crisp checklist report: each item ✅ / ⚠️ / ❌ with the evidence, a **verdict**
(`ready-to-open` / `ready-to-merge-pending-gate` / `blocked`), and the specific blockers.
Apply only trivial, unambiguous fixes (e.g. add the CHANGELOG line); escalate anything
requiring judgment.

## Done when

Every checklist item has a status with evidence, the verdict is stated, and the blockers
(if any) are named with what each needs.
