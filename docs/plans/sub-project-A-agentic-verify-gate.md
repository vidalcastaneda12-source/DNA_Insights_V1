# Sub Project A — Agentic Verify-Gate (`/verify-and-merge`)

**Status:** Design approved (brainstorming), ready for an implementation plan.
**Date:** 2026-06-24.
**Source:** Brainstorming session on bolstering `/scope-run`.
**Scope:** This is **Sub Project A** of a four-part enhancement effort (A–D, below). A
must ship before its dependents reuse the gate model it defines.

---

## 1. Context — where this fits

`/scope-run` can't *right-size* or *de-friction* the back half of a change's life.
A brainstorm decomposed the enhancement work into four sub-projects so each gets its own
spec → plan → build cycle:

| Sub-project | Contains | Status |
|---|---|---|
| **A · Agentic verify-gate** *(this doc)* | Gate-2 redesign + dry-run pre-flight + Claude-driven squash-merge & close | **Designed** |
| B · Right-sizing | Fast-follow loop + scope-split intake | Deferred (reuses A's gate model) |
| C · Learning & budget | Predicted-vs-actual calibration + token/context budget + resumability | Deferred |
| D · Hardening | Fidelity-gap wiring + agent retry/partial-failure + observability | Deferred |

**The problem A solves.** Today Gate 2 requires VSC-User to *independently* run
`verification.md` and merge. The owner wants Claude to run the full verification, present
the results for approval, then squash-merge and run the close steps — turning VSC-User from
**independent runner** into **approver-on-evidence**.

**The principle being consciously relaxed.** `CLAUDE.md` and `verification.md` currently
call the operator's *independent* run "not negotiable" — it's the safeguard against
selective test runs, test mutation, and number-interpretation slippage. A relaxes it. This
is a deliberate, owner-approved trade for a **solo, personal-use** project, and the design
below preserves ~80% of the safeguard's value by approving on **raw evidence**, not a
narrative summary. Adopting A therefore includes a **conscious rewrite of
`verification.md`'s Purpose section** from "independent run" to "evidence-gated approval."

> **Full-independence revert path** (kept in the design as a one-line fallback): VSC-User
> runs `./scripts/verify.sh` + the anchor queries personally while Claude preps the
> expected-values sheet and does only the merge/close. Restores the original safeguard
> without rebuilding anything.

---

## 2. Locked decisions (from the brainstorm)

| # | Decision | Choice |
|---|---|---|
| 1 | Trust model | **Evidence-gated hand-over** — Claude runs everything, but you approve on raw `verify.sh` output + an expected-vs-actual anchor table + test delta, not a narrative summary. |
| 2 | Packaging | **Standalone `/verify-and-merge` command**, composed by `/scope-run` Stage 4–5. Reusable on any branch/PR, testable in isolation, keeps `scope-run` lean. |
| 3 | Failure mode (mechanical reds) | **Bounded auto-fix + disclose** — loop the fix-first cycle up to **N = 2**, re-verify, come back only with GREEN (disclosing what was fixed) or "couldn't fix after N." |
| 4 | Always-hard-stop constants | An **anchor ≠ its predicted value**, a **weakened/removed assertion**, or a **schema-rule violation** *always* hard-stops and escalates — never auto-"fixed." |

**Feasibility (confirmed in-repo):** `scripts/verify.sh` already runs the canonical suite
(`uv sync · pytest · ruff check · ruff format --check · mypy --strict backend/src · genome
docs check`); the live `data/genome.duckdb` (~5.2 GB, full imputed corpus) and `app.db` are
present, so the **real-data anchor checks are runnable here**, not just the dev-loop;
`verification.md` is structured by change-class (core → schema → pipeline/anchor → specific
gate sections) with capture commands and bedrock anchor tables.

---

## 3. Design

### A. What it is

A standalone `/verify-and-merge` command (a markdown skill under `.claude/commands/`) that
runs the **back half** of a change's life: **verify → evidence → your approval →
squash-merge → close.** It is the evidence-gated replacement for `/scope-run`'s Stage 4
handoff + Gate 2 + Stage 5 close, and `/scope-run` Stage 4–5 simply calls it. It runs on
the **model-driven path** (the lead session executes `bash`/`gh` directly) — no dependency
on the JS-workflow runtime (that is Sub Project D).

**Two invariants:**
- It **never merges without a typed human approval.**
- It is **fail-closed** — any unresolved red, any anchor deviation, or any unverifiable
  anchor means **no merge is offered.**

### B. Thin skill + testable core

To keep the logic under `mypy --strict` and unit-tested, the substance lives in a small
Python module and the skill stays thin:

- **`genome.verify_gate`** (Python, unit-tested) — the *check-set assembler* (change-class
  → which checks + which anchors apply), the *verdict logic* (the GREEN/BLOCKED truth
  table), and the *evidence-package formatter*.
- **`.claude/commands/verify-and-merge.md`** (the skill) — orchestrates: runs
  `verify.sh`, runs the anchor-capture queries, calls the core for assembly/verdict/format,
  presents the evidence, handles approval, runs the `gh` merge + the close steps.

### C. The pipeline (data flow)

1. **Preflight — scope the verification.** From `git diff --name-only main..HEAD`, infer
   `change_class` (touches `backend/`? `ddl/`·`docs/schemas/` = schema? a pipeline dir? an
   annotation loader?). From that + `verification.md`, assemble the **applicable check
   set** and the **anchor expectation sheet** — each applicable anchor paired with its
   **expected value**: the *predicted* value from Stage-3 anchors-to-watch when invoked by
   `/scope-run` (so a deliberate re-lock shows its *new* expected value), else the current
   bedrock value (referenced by pointer, e.g. CLAUDE.md "Real-data observations"). Each
   anchor is tagged "expected stable" or "expected to re-lock → X."
2. **Dev-loop, verbatim.** Run `./scripts/verify.sh`; capture the full output.
3. **Bounded auto-fix (mechanical reds only).** On a mechanical red
   (pytest/ruff/mypy/format/docs-check), route back through the fix-first cycle, re-run
   `verify.sh`, **up to N = 2**. Every fix is **recorded for disclosure**. Still red after
   N → BLOCKED.
4. **Real-data anchor captures.** For each applicable anchor, run its exact
   `verification.md` capture query against the live `data/genome.duckdb`; build the
   **expected-vs-actual table** (`anchor | expected | actual | ✓/✗`).
5. **Integrity checks.** Test-count delta (before/after); scan the `backend/tests/` diff
   for weakened / skipped / removed assertions; CHANGELOG `[Unreleased]` entry present;
   `genome docs check` clean; no `GATE-FILL`/`TODO` survivors in durable docs.
6. **Verdict + evidence package.** **GREEN** iff dev-loop green (possibly post auto-fix) ∧
   every anchor actual = expected ∧ no weakened/removed test ∧ CHANGELOG present ∧ docs
   clean. The package presented to the human = verdict line · raw `verify.sh` tail · **any
   auto-fixes disclosed** (with the re-verify result) · the anchor table (or an explicit
   "N/A — no real-data anchors apply to this change-class") · test delta + integrity flags
   · files changed + PR link.
7. **Human approval gate.** BLOCKED → present the blocker + route, **no merge offered**,
   stop. GREEN → ask explicitly ("Approve squash-merge of PR #NN? reply `merge`"), wait.
8. **Squash-merge (on approval only).** `gh pr merge <PR> --squash` with a clean title/body
   and the repo's commit trailers.
9. **Close ("the rest of the steps").** Re-lock the **confirmed** anchor values into
   `CLAUDE.md` / `verification.md` / the finding's bedrock table; flip the ROADMAP slot;
   append/flip the `DEC-NNNN` ledger rows (insert-then-flip, never in-place edit);
   `genome docs check` must exit 0 — landing as a **fast-follow doc PR** for review (see
   Decision in §4). Then `repo-sweep` files the residual backlog, and the merged branch is
   cleaned up (local + remote).

### D. Error / edge handling (fail-closed everywhere)

- **🚨 Schema-changed PR — the safety landmine.** Real-data re-verification of a schema
  change requires `rm -rf data/ && genome init` + re-ingest — which would **destroy the
  ~5.2 GB corpus and cost ~30 min**. The gate therefore **never auto-rebuilds**: on a
  schema change it flags "real-data anchors require a rebuild + re-ingest (~X min) —
  confirm, or I'll mark those anchors manually-deferred." An auto-`rm -rf data/` is
  explicitly forbidden.
- **Live DB absent / stale** → anchors unverifiable → BLOCKED ("rebuild, or run anchors
  manually"). Never fabricate or guess an anchor value.
- **Anchor ≠ expected** → hard-stop + escalate, presenting both readings (an intended
  re-lock with a wrong prediction vs. a regression). The human decides.
- **PR not mergeable / conflict** → BLOCKED, report, never force.
- **Partial verification** (some anchors run, some can't) → never report GREEN; report what
  ran and what didn't.

### E. Testing

- Table-driven unit tests for the **check-set assembler** (docs-only → dev-loop only;
  schema → +rebuild flag; pipeline → +merge anchors; annotation → +index anchors).
- Unit tests for the **verdict truth table** (green dev-loop + matching anchors → GREEN; one
  anchor off → BLOCKED; weakened test → BLOCKED; CHANGELOG missing → BLOCKED).
- Snapshot test the **evidence-package formatter** (anchor table + disclosure section).
- A **`--dry-run`** that prints the `gh`/merge/close commands without executing, so tests
  never actually merge; live-DB queries mocked with recorded outputs / a small fixture DB
  (realistic fixtures per finding-013).
- One integration smoke on a throwaway branch (open PR → `verify-and-merge --dry-run` →
  assert evidence-package shape + "would merge").

---

## 4. Resolved defaults

1. **Re-lock landing → separate fast-follow doc PR.** The post-merge anchor re-lock lands
   as its own reviewable doc PR (matches the existing `close.js` / `knowledge-curator`
   convention) rather than being folded into the squashed feature PR — keeps the feature PR
   clean and the re-lock reviewable. (A natural first customer for Sub Project B's
   fast-follow loop later.)
2. **Standalone `change_class` inference → from diff dirs.** When run outside `/scope-run`,
   derive the change-class from which directories the diff touches, rather than requiring a
   manifest.
3. **Schema-rebuild safety → never auto-rebuild / confirm-first.** Non-negotiable default,
   not a fork. The ~5.2 GB DB is never destroyed without an explicit "yes."

---

## 5. Out of scope (for A)

- Sub Projects **B / C / D** — fast-follow loop, scope-split, cross-run learning, token/
  context budgeting, resumability, fidelity-gap wiring, observability. A only defines the
  gate model B will reuse.
- Wiring into the **deterministic JS workflows** — A targets the model-driven path; the JS
  `implement-review.js` / `close.js` integration is Sub Project D's concern.
- Auto-approval or auto-merge of any kind — the human approval token remains required.

---

## 6. Next step

Move to an implementation plan (the `writing-plans` skill): break A into ordered tasks —
the `genome.verify_gate` core (assembler, verdict, formatter) test-first, the
`verify-and-merge.md` skill, the `verification.md` Purpose-section rewrite, and the
`/scope-run` Stage 4–5 hand-off — with the dev-loop and the `--dry-run` integration smoke
as the verification.
