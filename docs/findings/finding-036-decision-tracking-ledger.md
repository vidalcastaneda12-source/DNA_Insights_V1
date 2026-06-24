---
type: decision
status: active
actors: [VSC-User, ClaudeCodeDevelopment]
date: 2026-06-23
supersedes: []
superseded_by: []
---
# Finding 036 — decision-tracking ledger (`MEMORY.md` + frontmatter + `genome docs`)

This is the first finding **born with frontmatter** — it dogfoods the convention it
establishes, and it records its own adoption in the ledger (DEC-0021).

## Context — the leak

The repo had **no single decision log**. Decisions scattered across CLAUDE.md
locked-decisions, 35 findings (which mixed empirical observations with reasoned decisions
under no machine-readable type/status), ROADMAP, CHANGELOG, and git/PR history. Two classes
leaked entirely: **tactical/implementation decisions** (a threshold chosen, a deferral, an
approach reversal) lived only in PR bodies and chat; **reversed/superseded decisions** were
discoverable only by prose archaeology (e.g. finding-035 `user_only` superseding finding-011
`three_way`). The agent automation already *assumed* a "MEMORY index" the
`knowledge-curator` updates — but no such file existed (a dangling, unsatisfiable contract).

## Decision

Adopt a **git-tracked `MEMORY.md` decision ledger** at the repo root + **per-finding
frontmatter**, generated/validated by a new **`genome docs`** Typer sub-app. Three dimensions:

- **Capture** — every finding carries a parseable `---`-fenced frontmatter block
  (`type {observation|decision|both}`, `status`, `actors`, `date`, `supersedes`/`superseded_by`);
  every decision lands a `DEC-NNNN` ledger row.
- **Retrieval** — `genome docs build-index` regenerates a single findings-index table inside a
  marker block in `docs/findings/README.md`, deriving cross-links from frontmatter.
- **Lifecycle** — status transitions are **append-then-flip** (insert a new row, flip the old
  to `superseded`/`reversed` with a back-pointer); content columns are immutable.

### Resolved design choices (VSC-User, 2026-06-23)

1. **Two orthogonal axes.** `status ∈ {active, superseded, reversed, deferred}` is lifecycle;
   `kind ∈ {architectural, tactical}` (the former *grain*) is granularity. `tactical` is a
   *kind*, **not** a status — so a tactical decision still carries a full lifecycle status and
   can be superseded/reversed.
2. **Frontmatter is authoritative** for the supersession edge; the ledger's cross-links are
   *derived* by `build-index`. The validator cross-resolves the finding-id ⇄ DEC-id spaces.
3. **Anchors are referenced, never copied.** A DEC `decision` cell points to the canonical
   home (`see CLAUDE.md obs #N` / a finding) and never transcribes a real-data anchor digit;
   `genome docs check` fails on a copied anchor. Tolerance-banded anchors are never frozen.
4. **Stdlib parser, no PyYAML.** Frontmatter is a flat, fixed key set, so a minimal stdlib
   parser avoids a new dependency (PyYAML-add is the documented fallback iff it proves brittle).
5. **Backfill is per-PR-history**, built as a separable final pass with a declared-complete
   boundary marker (this PR seeds the curated decision-finding rows; the per-PR bulk follows).

## Why it matters — decision #7 on a markdown substrate

A markdown table has no transaction, so this ledger is an **advisory, validator-enforced**
analogue of locked decision #7, not a transactional one. The no-torn-state /
never-UPDATE-active-content invariant is **relocated to the `genome docs check` gate**, which
hard-fails on an in-place content edit of an existing DEC row (diffed against the git baseline),
a duplicate/orphan/multi-superseder, a non-canonical actor, or a copied anchor. `finding-010`
was read first so the pattern's *intent* — not its deprecated column names — is what we adopt.

## Follow-up

- The **per-PR-history backfill** is **done** in this PR: `DEC-0022 … DEC-0084`, one row per
  merged PR (#19–#93, 63 PR-referenced commits in `main`'s lineage), the squash-merge subject
  git-verbatim as the decision. Declared-complete boundary in `MEMORY.md`. New PRs append the
  next row at the `/handoff` / Stage-5 checkpoint.
- **Landed** (DEC-0086, scope `docs-gate-enforcement`): the CI/pre-commit hook is now in place —
  a tracked git pre-commit hook (`scripts/git-hooks/pre-commit` + `scripts/install-hooks.sh`), a
  `genome docs check` step in `scripts/verify.sh`, and a `docs-check` GitHub Action on every PR
  (the repo's first CI). The `repo-sweep` missing-DEC-row detector + the checkpoint prompts remain
  as complementary surfaces.
- **Resolved:** the "fresh checkout with no SQLCipher built" goal is met — the `genome.cli`
  lazy-import refactor landed (DEC-0085), and `docs-gate-enforcement` additionally made the gate
  **config-free** (a logging-only `LoggingSettings` so `_configure_logging` no longer constructs
  the passphrase-bearing `Settings`), so `genome docs check` runs on a bare checkout with no
  SQLCipher built and no `APP_DB_PASSPHRASE`.
