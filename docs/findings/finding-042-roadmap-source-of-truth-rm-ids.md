---
type: decision
status: active
actors: [VSC-User, ClaudeCodeDevelopment]
date: 2026-06-29
supersedes: []
superseded_by: []
---
# Finding 042 — ROADMAP.md as the single source of truth for scope (`RM-` ids + capture-forward + `genome roadmap check`)

The scope analogue of [`finding-036`](finding-036-decision-tracking-ledger.md): where 036 made
`MEMORY.md` the single source of truth for **decisions**, this makes `ROADMAP.md` the single
source of truth for **scope** — every unit of planned/deferred work, addressable by a stable id
and enforced by a fail-closed gate.

## Context — the scope leak

A 2026-06-29 three-agent read-only audit (code + all 41 findings + the `MEMORY.md` ledger +
`docs/plans/*-deferred-followups.md` + runbooks + schemas + CHANGELOG + `.claude/`) surfaced
**~22 pieces of deferred/incomplete work that lived only in a finding, a plan doc, a code
comment, or a runbook and had never been promoted to ROADMAP.** Examples: the GWAS loader's
permanently-NULL `effect_size_unit`/`ancestry` columns; finding-030's recommended-not-applied
`prepare-chrx` perf fix (a whole finding with no ROADMAP slot); the verify-gate `change_class`
re-derivation hardening (the lone unchecked item in a plan doc); the two operator-only
required-status-check toggles. Scope was therefore scattered and ROADMAP was **not** actually
authoritative — the same leak class 036 closed for decisions, one level up.

A second, smaller gap: ROADMAP's only stable identifiers were the pre-Phase-6 "PR N" sequence
labels (PR 1–14). Phases 1–10's task bullets and the sub-project items had **no ids at all**, so
a finding or handoff could not cite a specific line item, and "PR N" renumbers under insertion.

## Decision

1. **ROADMAP.md is the single source of truth for all scope.** Newly-identified deferred or
   incomplete work MUST be added to ROADMAP (with an id) in the same change that records it; a
   finding / `MEMORY.md` / CHANGELOG / runbook may *describe* the work but must **back-reference
   the `RM-` id** rather than be its sole record. (Capture-forward — the analogue of "every
   decision lands a `DEC-NNNN` row".)
2. **Every trackable line item carries a frozen `RM-<7 hex>` id.** Form: `RM-` + the first 7
   chars of `sha1(<stable-kebab-slug>)`, lowercase. The `RM-` prefix disambiguates from the many
   real git short-SHAs cited throughout the findings/CHANGELOG and matches the repo's
   `DEC-####` / `finding-###` prefix culture; the hex honors the "git-commit-like" request. The
   id is **assigned once and frozen** — the written value is authoritative, so a later slug edit
   never re-derives it. A "trackable line item" is a **column-0** `- [ ]` / `- [x]` checklist
   item under a phase/sequence/sub-project section. Indented sub-bullets, `Status:`/`Verification:`
   lines, prose, and the machine-managed `<!-- B2-SUBSCOPES -->` region are exempt.
3. **"PR N" labels are retained as a secondary alias** (`RM-xxxxxxx (PR 8) — …`), so the ~69
   existing "PR N" references across ROADMAP / `MEMORY.md` / `scope-dispatcher` / CLAUDE.md /
   commands stay valid with zero drift.
4. **A fail-closed `genome roadmap check` gate** (DB-free, config-free) enforces the convention,
   mirroring `genome docs check` (finding-036) and `genome workflows check`
   ([`finding-034`](finding-034-agent-team-plan-phase.md) / DEC-0122): every column-0 checklist
   item has a well-formed id, all ids are unique, and every `RM-…` token cited in
   `docs/findings/` / `MEMORY.md` / `CHANGELOG.md` resolves to an id present in ROADMAP
   (referential integrity / dangling-ref catch). The `<!-- B2-SUBSCOPES -->` region is exempt
   (transient, writer-owned).

## Why it matters — decision #7's "single current set" for scope

This is the same intent as locked decision #7 (no torn state; one authoritative current set) and
finding-036 (decisions), applied to scope: there is exactly **one** place that answers "what work
is outstanding," and the gate fail-closes rather than letting scope silently re-fork into a
finding or a plan doc. The `RM-` id is the stable handle that lets a finding, a handoff, or the
`knowledge-curator` point *into* that authoritative set instead of re-describing the work.

## As executed — a 3-PR sequence

- **PR A** (this finding's landing PR) — ROADMAP restructure: frozen `RM-` ids on every line item
  (PR-N kept as alias) + the 22 audit items promoted into their phase/section homes (new
  `## Cross-cutting backlog (2026-06-29 audit)` section + targeted Phase 6/7/10 + sub-project
  inserts) + this finding + `DEC-0125`.
- **PR B** — the `genome roadmap check` fail-closed gate (`backend/src/genome/roadmap/` + Typer
  subcommand + `node`-free tests + a `roadmap-gate` CI workflow), tracked as RM item
  `roadmap-check-gate`.
- **PR C** — capture-forward convention propagation: a CLAUDE.md Conventions rule + updates to the
  work-capturing skills/agents (`new-finding`, `handoff`, `scope-run`/close, `knowledge-curator`,
  `repo-sweep`, `scope-dispatcher`) so newly-identified work routes into ROADMAP with an id.

## Follow-up

- The audit's full untracked-item list and the items it **filtered out as already-done/tracked**
  (`remote_tabix` helper extraction exists; `refresh-index` perf is Phase 10; `vcf_export`
  hom-only prepare superseded) live in the PR-A ROADMAP backlog section + the PR description.
- Teaching `scope_split`'s `roadmap_writer`/`formatter` to mint `RM-` ids for auto-written
  sub-scope slots (so the `<!-- B2-SUBSCOPES -->` region can drop its gate exemption) is itself a
  tracked RM item (`scope-split-writer-rm-ids`), dogfooding the convention.
- The two operator-only required-status-check toggles (the `docs-check` and `workflows-gate`
  Actions, and — when PR B lands — `roadmap-gate`) are recorded as RM items but can only be
  performed by a repo admin in branch-protection settings.
