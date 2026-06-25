Run the agentic verify-and-merge gate (Sub Project A, `finding-037`) for one pushed
branch / open PR: run the full verification protocol, capture the real-data anchors,
present the **raw** evidence, take a typed approval, then squash-merge and close.
Argument: a PR number (e.g. `6`), or the current branch's PR if omitted.

This is the **evidence-gated** alternative to Human Gate 2's manual run of
`docs/runbooks/verification.md`. It is faithful plumbing around a fail-closed,
unit-tested core (`genome.verify_gate`): the whole gate is "the core exited non-zero →
stop". The independent human run remains a standing fallback — this skill never removes
it; it adds an owner-approved path where Claude performs the merge.

## Two invariants (read first)

1. **Never merge without a typed approval token.** The squash-merge happens only after
   the operator types the `merge` token in response to the presented evidence. No token,
   no merge — full stop.
2. **Fail closed.** Every decidable check lives in `genome.verify_gate`; this skill only
   reaches the merge step if `genome verify-gate verdict` exited `0` (GREEN). A `BLOCKED`
   or `UNKNOWN` verdict stops the run with no merge, no squash, no `gh`, no `rm`.

## Steps

1. **Preflight.** `git diff --name-only` against the merge base → derive the
   `change_class` (core / schema / pipeline / annotation; multi-label is fine). Assemble
   the check set conceptually via the change class.
2. **Run the protocol and emit one `--step` per canonical dev-loop label.** The six canonical
   `scripts/verify.sh` labels are the single source of truth (`assemble_check_set` requires
   exactly this set): `uv sync`, `pytest`, `ruff check`, `ruff format --check`,
   `mypy --strict backend/src`, `genome docs check`. Pass `assemble` one
   `--step <label>:<exit-code>` for **each** of the six — `:0` (PASS) for each step that
   passed, the failing step's real non-zero code (FAIL), and any step that never ran
   `UNKNOWN`. Status is by **exit code, not stdout text**. Because `verify.sh` runs under
   `set -euo pipefail` it **aborts at the first failing step** and prints `FAILED at <label>`:
   from a single aborted run, mark the labelled step FAIL and every later (never-run) step
   UNKNOWN. If you need a precise status for *all six* (e.g. to see whether a later step would
   also fail), run the steps individually and record each one's exit code. Omitting a label is
   not an option — a missing required step is injected as `UNKNOWN` by the core and blocks the
   gate, so emit all six.
3. **Bounded auto-fix (N=2, mechanical only).** A formatting-only or trivially-mechanical
   failure may be auto-fixed and the step re-run, at most twice. Anything that is an
   always-hard-stop (a real test failure, a type error, a schema-rebuild need, a logic
   change) is **never** auto-fixed — it stops the run and goes back to the operator.
4. **Anchor captures.** For a pipeline / annotation change, capture the real-data anchors
   named in `docs/runbooks/verification.md` (the `genome merge` / `refresh-index` columns).
   A DB that is **absent or stale** yields `actual=None` → the verdict is `UNKNOWN`; never
   fabricate a number. A **schema** diff marks its anchors deferred and sets the rebuild
   flag — do **not** `rm -rf data/` on the gate's own initiative (that is the operator's
   call; confirm first).
5. **Integrity scan.** Compute the test-count delta (before vs after), scan the diff for a
   weakened/removed assertion, confirm a `[Unreleased]` CHANGELOG entry, confirm
   `genome docs check` exited `0`, and grep for a surviving fill-placeholder sentinel in the
   durable docs.
6. **Assemble + reduce.** Pass the captured flat values to `genome verify-gate assemble`
   (the skill passes only flat strings; the CLI builds the package and writes
   `evidence.json`), then run `genome verify-gate verdict --package evidence.json`. A
   **non-zero exit stops the run** — present the blocking reason and return to step 1 after
   the operator resolves it. No merge.
7. **Present + approve.** On GREEN, run `genome verify-gate format --package evidence.json`
   and present that raw block. Ask the operator to type the `merge` token. No token → stop.
8. **Audited squash-merge.** Write the intent audit row
   (`write_merge_audit(phase='intent', …)`); re-check `gh pr view <pr> --json mergeable`
   (TOCTOU — the branch may have drifted since the verify run); `gh pr merge <pr>
   --squash`; write the result audit row (`phase='result'`, `status='success'`/`'failure'`).
   The audit records and proceeds — it never gates the merge.
9. **Close.** Delete the merged branch. **Delegate** the re-lock of the operator-confirmed
   anchors into `CLAUDE.md` / `verification.md` / the finding's bedrock table to the
   Stage-5 `knowledge-curator` as a fast-follow **reviewable doc change** (human-confirmed
   numbers only, never a direct push). After close, auto-scan the residual backlog and
   **offer a `/fast-follow` drain-loop scan** of it (Sub Project B, `finding-038`) — this is
   an **offer only**: it never drains, never merges, and never acts without the
   `/fast-follow` triage-approval touchpoint (touchpoint 1). Distinct from the
   knowledge-curator doc re-lock above, which is a separate "fast-follow" sense.

## Temporal note

This skill governs **future** scopes. The Sub-A PR that introduces it lands through the
**existing** Human Gate 2 (the operator merges it by hand) — the verify-gate dog-foods on
the next scope, not on its own introduction.
