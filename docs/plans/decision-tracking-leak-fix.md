# Decision Tracking Leak Fix

> **Status:** **Approved by VSC-User 2026-06-23** — the four escalations are resolved
> (see §0) and implementation is underway on branch `decision-tracking-leak-fix`.
> Produced by the `plan-phase.js` per-scope agent team (finding-034), revised once to
> fold in the pre-mortem + auditor blockers.
>
> **Scope id:** `decision-tracking-leak-fix` · **Risk tier:** 2 (escalated from 1 by open
> questions) · **Change class:** docs + `.claude` automation + backend tooling (Typer
> sub-app) + tests · **Real-data anchors applicable:** none (negative-control scope).
>
> **Origin:** "Option D" from a brainstorming session — close the repo's decision-tracking
> gap. The automation already references a central *MEMORY index*
> (`.claude/agents/knowledge-curator.md`, `finding-034`) that **does not exist** on disk;
> this plan realizes that dangling artifact and wires capture into the existing checkpoints.
>
> **User priority:** *comprehensiveness* of tracking — capture coverage + single-index
> retrieval + lifecycle — over minimal overhead.

---

## 0 · Resolved escalations (VSC-User, 2026-06-23) — implementation cleared

The four judgment calls below were escalated to VSC-User and **resolved on 2026-06-23**;
the answers are now locked spec. (Originally a ⛔ blocking-escalation section; preserved here
with resolutions for provenance. All four chose toward maximum comprehensiveness, consistent
with the locked "comprehensiveness over overhead" priority.)

1. **Backfill scope (Task 6).** → **(c) Per-PR-history** — one DEC row per merged PR across
   all history (≈90+ rows; numbering reaches #93). Built as a **separable final pass** with an
   explicit declared-complete boundary marker (e.g. "backfilled through PR #93"). Arch-grain
   decisions that don't map cleanly to a single PR are still captured as `kind: architectural`
   rows. Nothing in the tooling (Tasks 0–5, 7–9) depends on the backfill being complete — Task 5
   bulk-apply gates only on a `genome docs check` dry-run passing.
2. **Closed status vocabulary.** → **`{active | superseded | reversed | deferred}`** (four
   lifecycle states), frozen before Task 3. The fifth candidate `tactical` is **not** a status —
   it moves to a separate **kind axis `{architectural | tactical}`** (the former `grain ∈ {arch,
   op}`, renamed), so a tactical decision still carries a full lifecycle status and can be
   recorded as superseded/reversed. The validator enforces **both** as closed sets.
3. **Command surface naming.** → **`genome docs {build-index, check}`** (confirmed).
4. **Supersession-edge source of truth.** → **Finding frontmatter is authoritative**; the
   ledger's cross-links are **derived** by `build-index`. The validator cross-resolves the
   finding-id ⇄ DEC-id spaces and hard-fails on divergence.

---

## 1 · Reading-list confirmation

Read and grounded against the live repo during planning:

- `CLAUDE.md` — five-actor model; locked decisions **#7** (supersession-over-update) and
  **#8** (provenance); conventions (structlog/no-`print`, `ruff --select=ALL
  --ignore=D,ANN101,ANN102`, `mypy --strict`, Typer-on-`genome`-CLI, tests under
  `backend/tests/`); "Things never to do".
- `docs/findings/finding-010-version-pointer-supersession-pattern.md` **§33–51, §89** — the
  decision-#7 rationale. **Load-bearing:** per-row `is_active` was *deprecated* in favor of
  a single-row version-pointer write inside a SQL transaction; the atomicity guarantee lives
  in the *transaction*, not the column tags. A markdown ledger has no transaction (see §3
  and the Task-2 design note).
- `docs/findings/finding-034-agent-team-plan-phase.md` — the agent team; the dangling
  `MEMORY` references this plan realizes.
- `docs/findings/README.md` — the findings-format authority (Task 8 updates it).
- `.claude/agents/knowledge-curator.md` (line ~22, "the MEMORY index"),
  `.claude/agents/repo-sweep.md` (lines ~36–37, the missing-CHANGELOG detector — precedent
  for the missing-DEC-row detector), `.claude/commands/handoff.md`,
  `.claude/commands/new-finding.md`, `.claude/commands/scope-run.md` — wiring targets, all
  present.
- `backend/src/genome/cli.py` (lines ~16–19, 42, 54–79) — the `app.add_typer` sub-app
  pattern **and** the module-scope DB imports that force the import-time-coupling fix
  (Task 3).
- `pyproject.toml` — **PyYAML is absent** (drives the Task-0 dependency decision).

**Verified repo facts the plan depends on:**

- Exactly **35** numbered findings (`finding-001 … finding-035`); `MEMORY.md` does **not**
  exist.
- **Status-line heterogeneity is worse than first assumed:** only ~7/35 findings carry any
  Status section, and they use **5+ distinct shapes** — `## Status` heading, `**Status:**`
  bold-prose (finding-035), `> **Status:**` blockquote (finding-008), `*Status:*`
  (finding-005), inline `Status:` — while **finding-001 and finding-020 have none**.
- **12 findings are dense with markdown pipe-tables** whose `|---|---|` separator rows and
  free-text `|`/backtick/`:` cells are a parser hazard (finding-020, finding-034 heaviest).
- **Legacy actor names are live** in tracked files: `VSC-Claude`, `VSC-ClaudeCodePlanning`,
  `VSC-ClaudeCode`, `AI-Claude` appear in `CHANGELOG.md`; `CLAUDE.md` documents
  `VSC-Claude → VSC-ClaudeCodeDevelopment`.

---

## 2 · Problem statement

The repo has **no single decision log**. Decisions scatter across CLAUDE.md locked-decisions,
35 findings (which mix empirical observations with reasoned decisions, with no
machine-readable type/status), ROADMAP, CHANGELOG, and git/PR history. Two classes leak
entirely:

- **Tactical/implementation decisions** (a threshold chosen, a deferral, an approach
  reversal) live only in PR bodies and chat.
- **Reversed/superseded decisions** are recorded nowhere systematically — discoverable only
  by prose archaeology (e.g. finding-035 `user_only` superseding finding-011 `three_way`;
  finding-029 M3-physical chrX superseding M1).

And the automation already *assumes* a ledger that was never built: `knowledge-curator`'s
Stage-5 contract says it "updates the MEMORY index," but no `MEMORY.md` exists — an
unsatisfiable, dangling contract.

**Goal — comprehensiveness on three dimensions:** **capture** (every decision lands a
parseable record), **retrieval** (one generated single-index surface), **lifecycle** (status
transitions are append-only insert-then-flip per decision #7, integrity-checked).

---

## 3 · Constraints

- **VSC-User resolutions (locked):** `MEMORY.md` at **repo root** (markdown, *not* a DB
  table → no `docs/schemas/` or `ddl/` change); **bulk-apply** frontmatter to **all 35**
  findings; the findings index is **generator-produced**; **full** retrospective backfill
  (definition pending — see blocking escalation #1).
- **Decision #7 — substrate-honest mapping (architecture-fit blocker).** The ledger is an
  **advisory, _validator-enforced_ analogue** of #7, **not** a transactional one. A markdown
  append has no transaction, no reader-isolation — so the no-torn-state / "never UPDATE
  active content" invariant is **relocated to the `genome docs check` gate**, which
  hard-fails on any violation (Task 3). Content columns of a DEC row are immutable; only
  `status`/`superseded_by` may transition, via insert-then-flip. `finding-010` is read
  before the schema is authored so we adopt the pattern's *intent*, not just the deprecated
  column names.
- **Decision #8 — provenance.** Every DEC row names actor(s) + a detail-link. Backfill
  provenance is **git/gh-verbatim** where recoverable; unrecoverable rows carry
  `provenance: unknown` (`GATE-FILL`) — **never guessed**.
- **Anchors are referenced, never copied (anchor-drift blocker).** DEC rows for real-data
  decisions **point to** the canonical value's home (`CLAUDE.md` obs #N / a finding's
  bedrock table); they do **not** transcribe the digits. `CLAUDE.md` remains the single
  source of truth for every imputation/index/consensus anchor. Tolerance-banded anchors
  (chrX yield ±~100, LOO 0.985–0.986) are never frozen as scalars.
- **Actor enum + legacy map.** `actors` is validated to the five canonical names; an explicit
  `legacy → canonical` map (`VSC-Claude → ClaudeCodeDevelopment`, `VSC-ClaudeCodePlanning →
  ClaudeCodePlanning`, …) is applied so existing history validates.
- **Code conventions.** Python 3.12+, structlog JSON (no `print`), `ruff --select=ALL
  --ignore=D,ANN101,ANN102`, `ruff format`, `mypy --strict backend/src`, Typer sub-app on
  the existing `genome` CLI (single console entry point — **not** a `scripts/` standalone),
  tests under `backend/tests/`. `genome docs` must carry **no DB import dependency** (Task 3
  lazy-import fix).
- **Format-doc change is deliberate.** `docs/findings/README.md` is updated to document the
  new frontmatter convention — the only sanctioned way to change the findings format.
- **Not a ROADMAP slot.** Stay within the parts below; no new phase.

---

## 4 · Implementation plan

Re-sequenced from the synthesized draft to fix the **ordering inversion** the auditors
flagged: the grammar and vocabulary decisions now precede the validator that enforces them.

**Task 0 — Dependency & grammar decision (do first).** Resolve PyYAML-vs-stdlib up front
(PyYAML is absent from `pyproject.toml`). Frontmatter is a constrained flat block; **default
to a minimal stdlib parser** to avoid a new dependency, with PyYAML-add as the documented
fallback **iff** quoting/escaping of `:`/backtick-laden finding titles proves brittle.
Record the choice in finding-036 + CHANGELOG. Decide explicitly that **frontmatter and the
ledger table are two distinct grammars** sharing one `DEC-NNNN` id-space (closes the
"one tolerant parser" conflation).

**Task 1 — Freeze the closed status vocabulary** (blocking escalation #2). The validator and
the bulk-apply both depend on it; it must be fixed before either.

**Task 2 — Author both schemas** (read `finding-010` first).
- *Ledger (`MEMORY.md`, repo root):* a single append-only markdown table, columns
  `DEC | kind | date | status | superseded_by | actors | provenance | decision | detail-link`.
  `kind ∈ {architectural, tactical}` (the former `grain`, per §0 resolution #2) carries the two
  granularities in one table; `status ∈ {active, superseded, reversed, deferred}` is the
  orthogonal lifecycle axis. The header documents the insert-then-flip rule **and** the
  advisory/validator-enforced framing, with a worked supersession example. **Free-text safety:** the `decision` column escapes or forbids raw
  `|`; the worked example must round-trip through its own parser (dogfood).
- *Frontmatter (atop each finding):* a `---`-fenced block — `type
  {observation|decision|both}`, `status` (closed vocab), `supersedes` / `superseded_by`
  (finding-id or DEC-id, typed), `actors`, `date`. **Frontmatter is the authoritative source
  of the supersession edge** (escalation #4); the ledger's cross-links are *derived*.

**Task 3 — Build the two parsers + the validator** (the enforcement core; no DB import).
- Frontmatter parser anchored on `^---\s*$` exactly, run only on the prepended block — never
  the body — so the `|---|` table-separator rows in 12 findings can't be mistaken for a
  fence. Tolerant of the **5 real Status shapes** + the "no Status" case (migration maps each
  legacy shape into the new block).
- Ledger-table parser with explicit column handling (free-text `|` escaped).
- **Validator hard-fails (this is where #7 lives) on:** (i) in-place content edit of a DEC
  row whose status is not latest; (ii) `>1` active row per `DEC-NNNN`; (iii) a `superseded`
  row lacking `superseded_by`; (iv) a `superseded_by`/`supersedes` pointer that doesn't
  resolve across the **finding-id ⇄ DEC-id** spaces (typed cross-resolution); (v) a
  non-canonical actor not covered by the legacy map; (vi) **any anchor-shaped number in
  `MEMORY.md` that doesn't match its cited CLAUDE.md/finding source** (the relocated negative
  control).
- **No DB dependency:** register `genome docs` via a lazy-import shim (or move cli.py's
  module-scope DB imports behind their command callbacks) so `python -c "import genome.docs"`
  pulls in no `genome.db` module and `genome docs check` runs on a fresh checkout with no
  DuckDB/SQLCipher built.

**Task 4 — `genome docs` Typer sub-app** under `backend/src/genome/docs/`, registered on the
`genome` CLI like `annotate`/`imputation`/`config`. Subcommands: `build-index` (regenerate
the findings index table into a marker block in `docs/findings/README.md`, **deriving** the
ledger cross-links from frontmatter) and `check` (the unified gate).

**Task 5 — Bulk-apply frontmatter to all 35 findings**, gated on a `genome docs check` dry
run exiting 0 first. Prepend above each existing `# Finding NNN` H1; **bodies byte-identical**
below. finding-036 dogfoods the new block.

**Task 6 — Retrospective backfill of `MEMORY.md`** — **BLOCKED on escalation #1** (the "full"
definition). When ratified: anchors **referenced not copied**; legacy actor map applied;
sources in priority order CHANGELOG → finding Status lines → git/gh verbatim;
`provenance: unknown` for unrecoverable rows; an explicit declared-complete boundary marker.

**Task 7 — Wire capture into the checkpoints.**
- `.claude/commands/handoff.md`: required "Decision rows" field — append/confirm DEC rows for
  this session's decisions (insert-then-flip on any reversal); state "None" explicitly if so.
- `.claude/commands/new-finding.md`: a `type: decision` finding must append/confirm its DEC
  row; new findings are born with the frontmatter block.
- `.claude/agents/knowledge-curator.md`: make the dangling "MEMORY index" reference concrete —
  at Stage 5, append/flip the gate-confirmed DEC rows under supersession, human-confirmed
  only, as a reviewable change (never a silent mutation).
- `.claude/agents/repo-sweep.md`: add a **missing-DEC-row** detector (a merged PR / a
  `status: superseded` or `type: decision` finding with no corresponding DEC row) modeled on
  the existing missing-CHANGELOG detector; add `MEMORY.md` to its Reads.

**Task 8 — Update `docs/findings/README.md`** to document the frontmatter convention (keys,
the closed vocab, the five-actor enforcement, an example) — the deliberate format-doc change.

**Task 9 — finding-036 + CHANGELOG + scope-run note.** finding-036 records the decision
(the leak, the resolved OQs, the ledger grammar + lifecycle, the validator contract, the
PyYAML/vocab choices) and is the first finding written *with* frontmatter; it gets its own
DEC row (the ledger records its own adoption). One CHANGELOG `[Unreleased]` entry with a PR
ref. One line in `.claude/commands/scope-run.md` Stage 5 noting the curator appends DEC rows
and repo-sweep runs the missing-DEC-row check.

---

## 5 · Tests (`backend/tests/`)

- **Parser tolerance** — fixtures for each of the **5 real Status shapes** + the "no Status"
  case (finding-001/020) + free-text `|` in a ledger cell + a `|---|`-separator-heavy body;
  every shape parses, none mis-fences.
- **Lifecycle (load-bearing)** — an in-place DEC content rewrite is **rejected**; the only
  accepted transition is insert-then-flip (new superseding row + back-pointer).
- **Integrity** — DEC ids unique + monotonic; `status ∈` closed vocab; **exactly one
  superseder** per superseded row; **no orphan** supersession; cross-resolution across the
  finding-id ⇄ DEC-id spaces.
- **Anchor-reference guard** — a DEC row transcribing a CLAUDE.md anchor number that doesn't
  match its cited source makes `check` exit non-zero (the relocated negative control).
- **Negative tests** (prove the gate is enforcement, not a rubber stamp) — duplicate-active
  DEC, in-place edit of a superseded row, dangling finding↔DEC cross-ref, non-canonical
  actor each exit non-zero.
- **Actor legacy map** — the existing `CHANGELOG.md` legacy names validate *via the map*; an
  unmapped novel name fails.
- **Provenance** — an unrecoverable backfill row is accepted only with `provenance: unknown`,
  never an empty/guessed source.
- **Retrieval idempotence** — `build-index` is **normalize-then-compare** stable (not raw
  byte-identical, to dodge table-padding/locale-collation fragility); a second run is a no-op.
- **Injected-gap CLI** — a decision-finding with no DEC row makes `genome docs check` exit 1.
- **No-DB-import** — importing `genome.docs` / running `genome docs check` pulls in no DB
  module and needs no built database.

**Must still pass:** full existing `pytest` suite (additive module; one `add_typer` line);
existing CLI/`--help` tests (membership-based, so the new sub-app won't break them);
`ruff check` · `ruff format --check` · `mypy --strict backend/src`.

---

## 6 · Verification

- **Single gate:** `genome docs check` exits 0 **only when all three dimensions hold** —
  CAPTURE (every finding has parseable frontmatter), RETRIEVAL (regenerated index matches
  committed under normalize-then-compare), LIFECYCLE (ids unique+monotonic, closed-vocab
  status, exactly-one-superseder, no orphan, cross-space pointers resolve). Prints the
  offending finding/row on failure.
- **Dev-loop green:** `pytest` · `ruff check` · `ruff format --check` · `mypy --strict
  backend/src`.
- **Negative control (extended):** **no `CLAUDE.md` "Real-data observations" number
  changes**, *and* `MEMORY.md` transcribes **no** CLAUDE.md anchor digits except
  validator-cross-checked references — closing the drift surface the original control was
  blind to.
- **No schema touch:** `git diff --name-only main..HEAD -- docs/schemas/ ddl/` is empty.

---

## 7 · Out of scope

- Any `docs/schemas/` or `ddl/` change; `MEMORY.md` is markdown, not a DB table; no
  `rm -rf data/` rebuild.
- Any change to CLAUDE.md real-data numbers or the data pipeline.
- A DB-backed decision store / migration system (markdown-first for a personal-use app).
- Re-litigating the backfill-scope definition inside the plan (escalated, not decided here).
- Rewriting finding **bodies** (frontmatter is prepended only).
- CI / git-hook / GitHub-Action enforcement of the validator (the repo-sweep detector +
  checkpoint prompts + the `genome docs check` gate are the enforcement surface; a pre-commit
  hook is deferred).
- A `scripts/` standalone generator; a second console entry point; any ledger UI.
- Modifying the `.claude/workflows/*.js` runtime (only the model-driven `scope-run.md`
  contract note changes).

---

## 8 · End-of-session handoff

At implementation session end, run `/handoff` — and, **dogfooding the new mechanism**, that
handoff emits the DEC rows for decisions made during implementation (e.g. the
PyYAML-vs-stdlib choice, the ratified status vocabulary). New branch from `main`, clean
dev-loop, commit + push, open PR carrying the CHANGELOG entry + finding-036.

---

## Appendix A · `plan-phase.js` run provenance

| Stage | Members | Outcome |
|---|---|---|
| 0 · Intake | `scope-dispatcher` | manifest; **risk_tier 2** (S=3→tier 1, +1 for open questions), `deep_T2=false`; 4 open questions → VSC-User |
| — | VSC-User | resolved OQ-1..4 toward **max comprehensiveness**: repo-root `MEMORY.md`; bulk-apply all 35; generator **CLI**; **full** retrospective backfill |
| 1 · Planners | `planner` ×4 (minimal-diff, gate-backward, risk-first, convention-purist) | 4 candidate 8-section plans (self-confidence 0.71–0.78) |
| 1 · Judges | `plan-judges` ×5 | **correctness → risk-first (0.85)**, **risk → risk-first (0.92)**, **locked_decision_fit → convention-purist (0.95)**, **scope_discipline → convention-purist (0.83)**, **verification → gate-backward (0.86)** |
| 1 · Synthesis | `plan-synthesizer` | risk-first skeleton + C4 constraints + C2 verification + C1 PyYAML graft; panel confidence 0.82 |
| 1.5 · Pre-mortem | `plan-premortem` ×2 (anchor-drift, schema-assumption) | both **revise** — anchor-copy drift; two-grammar conflation; ordering inversion; legacy-actor map; cross-id-space; idempotence fragility |
| 1 · Audit | `plan-auditor` ×2 (contract, architecture-fit) | both **revise**, 4 blockers each; architecture-fit: markdown substrate can't provide #7's transactional atomicity → relocate invariant to the validator gate |
| Revise cycle 1 | (collapsed) | findings were deterministic + convergent; folded into this plan rather than re-fanning. The one item no revision can settle — backfill "full" scope — is escalated above. |

## Appendix B · Revision log (blockers folded in)

1. **Anchor duplication / blind negative control** → DEC rows reference anchors by
   provenance pointer; validator cross-checks any transcribed number; negative control
   extended to `MEMORY.md`; tolerance-banded anchors never frozen as scalars. *(§3, Task 3,
   §6)*
2. **Ordering inversion** (validator before vocab/grammar decided) → re-sequenced: Task 0
   grammar → Task 1 vocab → Task 2 schemas → Task 3 validator. *(§4)*
3. **One "tolerant parser" conflating two grammars; 5 Status shapes not 3** → two distinct
   parsers sharing the DEC id-space; fixtures for all 5 shapes + free-text `|` + `|---|`
   bodies; fence anchored exactly, body never parsed as frontmatter. *(Task 0/2/3, §5)*
4. **Markdown substrate can't enforce #7's atomicity** → ledger explicitly scoped as
   advisory/validator-enforced; the invariant lives in the `genome docs check` gate with
   negative tests; `finding-010` added to the reading list. *(§1, §3, Task 2/3, §5)*
5. **Import-time DB coupling** → `genome docs` registered via lazy import; no-DB-import test.
   *(Task 3/4, §5)*
6. **Legacy actor names rejected** → explicit legacy→canonical map; existing-CHANGELOG-passes
   test. *(§3, Task 6, §5)*
7. **Dual id-spaces diverge** → frontmatter authoritative, ledger cross-links derived;
   validator cross-resolves and fails on divergence. *(§3, Task 2/4, escalation #4)*
