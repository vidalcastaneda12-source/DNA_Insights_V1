---
type: decision
status: active
actors: [VSC-User, ClaudeCodeDevelopment]
date: 2026-06-25
supersedes: []
superseded_by: []
---
# Agentic verify-and-merge gate — evidence-gated approval (Sub Project A)

## Status

**Active (2026-06-25).** Adopted at Gate 1 for Sub Project A. This is the durable
provenance anchor for the `genome.verify_gate` core, the `/verify-and-merge` skill, the
two-row merge audit, and the four ledger rows `DEC-0087 … DEC-0090`. The synthesized plan
artifact is transient (plans get pruned — see `DEC-0084`); this finding is where the design
rationale lives.

## What changed

Human Gate 2 (the merge gate) historically required **VSC-User** to run
`docs/runbooks/verification.md` independently, confirm the real-data anchors, and squash-
merge. Sub Project A adds an owner-approved **evidence-gated** path: Claude runs the full
verification protocol plus the anchor captures, presents the **raw** evidence to the
operator, takes a typed approval token, and then performs the squash-merge + close.

The relaxation is deliberately narrow. The independence principle is preserved for:

1. **The human-GATE-2 fallback that still exists** — the operator may always run the
   protocol independently and merge by hand; the runbook keeps a one-line full-independence
   revert path.
2. **The general agent-team design** — every other stage still ends at an out-of-loop human
   gate (`finding-034`); only the merge step gained an evidence-gated alternative.

## Why a false-GREEN is the danger, and the response

The risk of letting the change-producing loop also clear its own merge gate is a
**false-GREEN**: a clean-looking signal that does not reflect the underlying truth
(selective test runs, test mutation, real-data-drift accepted on the model's say-so). The
response is to push every **decidable** check into a fail-closed, unit-tested core and make
the skill faithful plumbing whose only gate is "the core exited non-zero → stop".

### The fail-closed core (`genome.verify_gate`)

- A three-valued **`Verdict`** (`GREEN` / `BLOCKED` / `UNKNOWN`) with precedence
  **UNKNOWN dominates BLOCKED dominates GREEN** — an undecidable signal can never be
  reported as a pass, and a decided failure can never be masked by one.
- A **`StepStatus`** parser keyed on the process **exit code** (`0` → PASS, positive → FAIL,
  `None` → UNKNOWN), never a stdout substring — a step that prints `FAILED` but exits `0` is
  a PASS.
- Frozen evidence records whose **every flag defaults to its non-affirmative value**
  (`IntegrityFlags`, `rebuild_pending=True`): a package built with no arguments is maximally
  un-GREEN.
- `reduce_verdict` returns GREEN **iff** every step PASS, every non-deferred anchor matches,
  CHANGELOG present, docs check clean, no weakened/removed test, no surviving gate-fill
  sentinel, the test count is decided and non-decreasing, and no schema rebuild is pending.
  A change class with no applicable real-data anchors (the N/A path — this PR's own run)
  emits the literal sentinel `N/A — no real-data anchors apply to this change-class`, never
  a fabricated number.
- The core **imports no `genome.db`** (a clean-subprocess guard locks the boundary), so it
  runs on a fresh checkout. The serialization seam is flat: `verify-gate assemble` builds the
  package from flat primitive CLI args (the skill never assembles nested JSON);
  `verify-gate verdict` / `format` read the resulting `evidence.json`.
- **Completeness is enforced at every boundary, not only `assemble`.** Because the skill gates
  on `verify-gate verdict`'s exit code, `verdict` (and `format`) re-derive completeness from
  the package's `change_class` before reducing / rendering — every required dev-loop step or
  real-data anchor the package omits is injected as `UNKNOWN`, a `deferred` flag on a
  non-rebuild class is unmasked, and `rebuild_pending` is re-forced for a schema class. So a
  hand-crafted or bypassed `evidence.json` fed straight to `verdict` cannot read GREEN; the
  derivation is idempotent on a correctly-assembled package.
- **`change_class` is a TRUSTED input.** Completeness is derived *from* the declared
  `change_class`, but the class itself is not independently verifiable by the DB-free core —
  the skill derives it from the `git diff` of the change, and the CLI cannot re-confirm it
  without the repo/DB. An under-declaration (e.g. a `pipeline` change declared `core` to dodge
  the merge anchors) is therefore not caught by the core. The backstops are: the formatter
  prints `change_class` prominently in the raw evidence block, and the **typed `merge`-token
  human review** is where a mis-declaration is meant to be caught — the operator reads the
  declared class against the change before approving.

### The skill (`.claude/commands/verify-and-merge.md`)

Faithful plumbing: preflight git diff → change class → assemble; run `scripts/verify.sh`,
capturing each step's exit code; a **bounded** mechanical auto-fix (N=2, always-hard-stops
never auto-fixed); capture the real-data anchors (a DB that is absent or stale yields
`actual=None` → UNKNOWN, never fabricated; a schema diff defers the anchors, never
`rm -rf data/` on its own); the integrity scan; then `genome verify-gate verdict` — a
non-zero exit stops the run, no merge. Only on GREEN does it take the typed `merge` token,
write the intent audit row, re-check `gh pr view --json mergeable` (TOCTOU), `gh pr merge
--squash`, and write the result audit row. The re-lock of confirmed anchors is delegated to
the Stage-5 knowledge-curator as a fast-follow doc change.

## The two-row merge audit (`external_client.write_merge_audit`)

The squash-merge is an external call: `gh pr merge` contacts `api.github.com`, so per
decision #9 it is audited with `external_call=1`. The audit mirrors the HTTP client's
intent/result pattern: an **intent** row is written before `gh pr merge` and a **result**
row after, both sharing one stable `sha256` of the merge identity
(`{pr, head_sha, base, squash}`). `action_type='write'` because the `audit_log` enum has no
`'merge'`; `resource_type='pull_request'`, `external_endpoint='github'`. The request body is
**never stored** — only its hash. The audit **records and proceeds**: it never gates the
merge, and a failed result-row write is observable (it propagates) but does not roll back the
intent row or un-merge — so a crash between the two writes still leaves the intent row,
proving a merge was attempted.

## Jobs-table carve-out

The verify-gate is **meta-tooling** (a developer-loop / CI gate), not a data pipeline task,
so it is not routed through the jobs table. The "all long-running tasks go through the jobs
table" convention governs analytical pipeline work that produces user-facing data; the merge
gate produces no such data and runs in the developer/CI context.

## Provenance reversal note

`DEC-0087` relaxes a safeguard that the runbook framed as "not optional and is not
negotiable" via a pure append (no in-place flip of a prior row), following the
pure-append-reversal precedent set by `DEC-0086`. The relaxation is additive — it introduces
an alternative evidence-gated path while preserving the independent human run as a standing
fallback — so no prior ledger row is superseded.

## Follow-on

- [`finding-038`](finding-038-fast-follow-drain-loop.md) (Sub Project B — the fast-follow
  drain loop) reuses this gate as its per-batch merge backstop and extends the
  `/verify-and-merge` close step with a fast-follow drain auto-offer.
