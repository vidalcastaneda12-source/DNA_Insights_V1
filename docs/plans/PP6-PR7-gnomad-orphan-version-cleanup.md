# gnomAD Orphan Version-Row Cleanup (PR 7) — Re-scoped: Moot Against the Live DB

> **Status:** Plan — produced by the `plan-phase.js` per-scope agent team (finding-034),
> Stages 0–1, Tier 2, **single cycle**. Auditor verdict **ready** (0 blockers); pre-mortem
> **proceed**; panel confidence **0.88**. Three *minor* verification-precision findings were
> folded in (Appendix B). **Not implemented.** This document is the pre-gate package for
> VSC-User approval. No code was written and no DB state was changed.
>
> **Scope id:** `PR-7` · **Risk tier:** 2 (escalated from 1 by an open question — see
> Appendix A) · **Change class:** `data-backfill` → **resolves to docs-only** on the moot
> path · **Real-data anchors applicable:** none (negative-control scope; the disposition's
> correctness *is* that nothing moves).
>
> **Origin:** ROADMAP "Pre-Phase-6 sequence" PR 7 — finding-015 §12 **Option C**: a one-off
> `DELETE` of zero-reference orphan gnomAD `annotation_source_versions` rows (originally
> framed as `source_version_id IN (6, 7, 8, 10)`). The ROADMAP slot and the finding-015
> Amendment both flag that id-list as **stale and dangerous** post-rebuild and instruct:
> *re-derive the real zero-row orphan set against the live DB first; PR 7 may be moot.*
>
> **Headline result:** the re-derivation was run (read-only) and **independently re-run by
> two separate agents** against the live `data/genome.duckdb`. The zero-row gnomAD orphan
> set is **empty**. **PR 7 is moot as a data mutation**; the stale `IN (6, 7, 8, 10)` DELETE
> would destroy the active + superseded gnomAD builds. The remaining defect is **documentary**.

---

## 🚦 Decision required from VSC-User before implementation

The empirical fact is **settled and certain**: *execute no `DELETE`.* All four candidate
planners, the synthesizer, and both auditors agree, each having (or independently
re-confirming) the live probe. The only thing the agents cannot settle — because it is a
roadmap/provenance judgment call outside the code — is the **disposition *form*** (OQ-2).
The implementer **HALTs at plan step 1** until you rule.

**OQ-2 — how should the moot PR-7 slot be closed?**

- **(a) Close-as-moot** — flip the ROADMAP checkbox to `[x]`, add a CHANGELOG entry, and
  cross-reference + inline-mark finding-015 §12. *(Lightest; records the probe; closes the
  §12 freshness gap.)*
- **(b) (a) plus a docs-only Option-A note** in `docs/runbooks/annotations.md` recording the
  live no-orphan state. *(Subsumes (a); the convention-purist angle's preference.)*
- **(c) Defer entirely to PR 9** (the general orphan-cleanup procedure), leaving the
  checkbox `[ ]`. *(If chosen, steps 3–5 collapse to a single deferral note.)*

**Synthesis recommendation: (a)+(b) combined** — the minimal-diff path that both closes the
slot and repairs the dangling §12 hazard. The plan below implements (a)+(b); it does **not**
pick unilaterally.

**Already resolved (informational, re-confirm at the gate if you wish):**

- **OQ-1 (the probe)** — *resolved read-only.* The live zero-row-orphan query returns `[]`.
  You may re-run it independently at gate time (the runbook's out-of-loop spirit). Either
  way: **no DELETE.**
- **OQ-3 (contingency)** — *does not fire on the current DB.* Only relevant if the
  execution-time re-probe **unexpectedly** returns rows (Branch B, step 6); it then requires
  per-id live re-verification + an `archive/` snapshot before any write.
- **Scope confirmation** — if your actual intent is to prune the **data-bearing** superseded
  `id=8` (4,467,370 rows) for disk reclamation, that is **not** PR 7 — it is **PR 9**
  (finding-010 #14). Confirm `id=8` retention is deferred, not silently folded here.

---

## 1 · Reading-list confirmation

Read and grounded against the live repo + live DB during planning:

- **`ROADMAP.md`** — PR-7 slot (lines 149–160) + the post-PR-C ⚠️ re-scope note; status
  lines 5 + 89; the PR-8 (161–163) and **PR-9** (164–166) boundaries (the general
  orphan-cleanup slot PR 7 must not absorb).
- **`CLAUDE.md`** — locked decisions **#7** (supersession-over-update / version-pointer) and
  **#8** (provenance everywhere); "Things never to do" #1 (schema immutability) and #3
  (gnomAD filter floor); real-data **obs #4** (post-chrX `user_only` re-lock: gnomad
  `source_version_id=10` active / `id=8` superseded-with-data; index counts) and **obs #3**
  (`variants_master` 3,160,364).
- **`docs/runbooks/annotations.md`** — §5.5 gnomAD `user_only` filter, active
  `source_version_id=10`, locked `rows_loaded` 4,568,802 / `match_rate` 0.9957; the
  per-source `_cleanup_orphan_version_row` convention shown for clinvar/gwas/pharmgkb/cpic/pgs.
- **`docs/findings/finding-015-gnomad-v10-audit-trail-anomaly.md`** (full) — §2–9 orphan
  analysis, §10–13 Options A/B/C, the §12 `IN (6, 7, 8, 10)` DELETE (lines 144–149), §13
  Option-A recommendation, and the **Amendment** (lines 161–177) marking the id-specific
  cleanup **stale/dangerous** post-PR-C and PR-7 likely moot.
- **`docs/findings/finding-010-version-pointer-supersession-pattern.md`** — the version-pointer
  canonical pattern + atomicity-by-construction; follow-up **#14** distinguishing PR-7's
  one-off from the PR-9 **general** periodic procedure.
- **`docs/findings/finding-035-gnomad-filter-set-consumer-audit.md`** — `user_only` adoption
  (2026-06-21); the PR-C reload created `source_version_id=10` (active) and demoted `id=8`
  to superseded-with-data — the change that invalidated finding-015's pre-rebuild id list.
- **`docs/findings/finding-034-agent-team-plan-phase.md`** — risk-tier scoring; the PR-7
  back-test row (C=2, B=1, P=1, S=4, tier=1) + the open-question escalation clause → tier 2.
- **`backend/src/genome/annotate/loaders/gnomad.py`** — `load()` pipeline;
  `_cleanup_orphan_version_row` (lines 752–789); the **post-loop orphan-cleanup guard**
  (line ~1546) — the finding-015 **Option-B hardening already shipped (PR #53)**.
- **`backend/src/genome/annotate/source_versions.py`** — `insert_source_version`
  (always-allocates a fresh id per refresh), `get_current_version` (reads via the
  `annotation_sources` pointer), `KNOWN_SOURCE_DBS` (incl. `gnomad`).
- **`backend/src/genome/annotate/cli.py`** — the `annotate_app` surface (`refresh` /
  `refresh-index` / `refresh-aliases` / `canonicalize-variants` / `align-tier3-consensus` /
  `collapse-duplicate-variants` / `seed-genes`); **no** `list-orphan-versions` command
  exists; the `canonicalize` `archive/`-snapshot convention.
- **`backend/tests/test_loaders_gnomad.py`** — the orphan-cleanup suite (lines 969–1192):
  `test_cleanup_orphan_when_chromosomes_run_yields_zero_rows`,
  `test_cleanup_orphan_when_first_chrom_fails_before_any_insert`,
  `test_resume_does_not_cleanup_preexisting_in_flight_row`,
  `test_successful_full_run_does_not_trigger_cleanup`.
- **`backend/tests/test_annotate_source_versions.py`** — `insert_source_version` /
  `get_current_version` coverage.
- **`ddl/group_2_annotations.sql`** — `annotation_source_versions` PK;
  `annotation_sources.current_source_version_id` **NOT NULL REFERENCES** (deleting the active
  target is a hard FK violation); 14 `source_version_id` FKs across group-2 tables.
- **`CHANGELOG.md`** — `[Unreleased]` already documents the ⚠️ guard on the `IN (6,7,8,10)`
  SQL; the PR-7 closure is not yet recorded.
- **LIVE-DB READ-ONLY PROBE** of `data/genome.duckdb` (5.5 GB, mtime **2026-06-23 10:58:30**)
  — the synthesizer and **both** auditors independently ran OQ-1; see §2.

---

## 2 · Problem statement

ROADMAP PR-7 (from finding-015 §12, Option C) carries the literal
`DELETE FROM annotation_source_versions WHERE source_db='gnomad' AND source_version_id IN (6, 7, 8, 10)`.
That SQL was written against a **pre-rebuild** DB (v6/v7/v8/v10 inert orphans, v9 active).
The DB has since been rebuilt and **PR-C (#85)** re-ran the gnomAD `user_only` reload, so the
id list is **stale**.

**The live DB was probed read-only (OQ-1) — and independently re-probed by two separate
auditors.** All three agree:

| gnomad `source_version_id` | version | `record_count` | actual `gnomad_frequencies` rows | status |
|---|---|---:|---:|---|
| 8 | 4.1.1 | 4,467,370 | **4,467,370** (matches) | superseded (pre-chrX `user_only` build) — **data-bearing** |
| 10 | 4.1.1 | 4,568,802 | **4,568,802** (matches) | **ACTIVE** (`annotation_sources.current_source_version_id=10`) |

- `annotation_sources` gnomad pointer = **10**. There is **no** v6 / v7 / v9.
- The exact OQ-1 zero-row-orphan query *(not the active pointer **and** not referenced by any
  `gnomad_frequencies` row)* returns **`[]` — zero rows.**

**Consequences:**

1. There is **no FK-safe orphan to delete.** PR-7's scoped action is empty.
2. The stale `IN (6, 7, 8, 10)` DELETE would erase **both** the active version (`id=10`,
   referenced by `annotation_sources` — a hard NOT-NULL FK violation / dangling pointer)
   **and** the superseded build (`id=8`, referenced by 4.47M `gnomad_frequencies` rows — mass
   data loss). The FK-safety argument finding-015 §12 relied on is invalidated for these ids.
3. Future-orphan **prevention** already shipped: `_cleanup_orphan_version_row`
   (`gnomad.py:752`), wired at the post-loop guard (line ~1546) — finding-015 **Option B**,
   **PR #53** — so a future `--chromosomes` / failed run self-prunes.

This exactly confirms CLAUDE.md obs #4 and the finding-015 Amendment. **PR-7 is moot.** The
only remaining defect is **documentary**: (1) finding-015 §12 still prints the bare dangerous
DELETE with **no inline** strikethrough/cross-reference (the Amendment warns only at the
end of the file — a §12-first reader could re-propose the data-loss DELETE); (2) ROADMAP PR-7
is still `[ ]` open when the live evidence closes it; (3) CHANGELOG documents the ⚠️ guard but
not the closure-as-moot with its probe evidence. The disposition **form** is escalated (OQ-2).

---

## 3 · Constraints

- **Decision #7 (supersession / version-pointer).** A `DELETE` of an
  `annotation_source_versions` row is FK-safe **only** for a row that is (a) **not** named by
  `annotation_sources.current_source_version_id` **and** (b) has **zero** referencing
  `gnomad_frequencies` rows. The live probe proves **no** such gnomad row exists, so the only
  decision-#7-respecting action is **to delete nothing**. Never touch the active pointer row
  (`id=10`) or the data-referenced row (`id=8`). The active-pointer FK is **NOT NULL**, so
  deleting the active target is a hard FK violation, not silent loss.
- **Decision #8 (provenance).** `annotation_source_versions` is the provenance spine for every
  `gnomad_frequencies` row (`id=8` → 4.47M superseded rows kept for history per finding-010
  #6/#14; `id=10` → 4.57M active). Positively: closing PR-7 must itself leave a provenance
  trail — the probe result + disposition recorded in durable docs **supersession-style** (a
  reviewable doc change, not a silent edit). finding-015 §12 is **annotated inline, not
  deleted**, to preserve the audit trail.
- **Things never to do #1 (schema immutability).** No `docs/schemas/` or `ddl/` file is
  touched; no schema change is raised or needed.
- **Things never to do #3 (gnomAD filter floor).** Not engaged — no reload, no filter-set
  change; the active `user_only` filter (finding-035) is untouched.
- **No-refactor zone.** The shipped Option-B hardening (`_cleanup_orphan_version_row`
  `gnomad.py:752-789` + the post-loop guard, PR #53) is correct and live — **do not** refactor
  or "improve" it. `source_versions.py`, `cli.py`, and the four orphan tests are unmodified.
- **Anchor-immutability (negative control).** This slot runs no `refresh-index`, no `merge`,
  no reload, and on the moot path **no DB write at all** — so obs #4 index counts
  (`gnomad_matches` 3,054,426 / `row_count` 3,077,001 / `clinvar_matches` 61,926 /
  `gwas_matches` 66,742 / `pharmgkb_matches` 1,737 / `is_rare` 173,689 / `is_ultrarare`
  109,013) and obs #3 `variants_master` 3,160,364 **cannot move**. Their invariance is the
  proof of correctness.
- **Phase boundary.** PR-7 is the gnomAD-specific zero-row-orphan one-off only. The **general**
  periodic orphan-row cleanup (all loaders, all superseded ids incl. the data-bearing `id=8`
  and `variant_aliases` orphans) + its runbook entry is **PR-9** (finding-010 #14). Phase-6
  entry is **not** gated on PR-7.
- **Read-only-actor boundary (ClaudeCodePlanning).** The OQ-1/OQ-3 probes are pure read-only
  `SELECT`s (allowed plan-time inspection); any `DELETE`/`UPDATE` is forbidden to the planning
  actor and — per the verified live result — forbidden outright on the moot path.

---

## 4 · Implementation plan

*Moot path = steps 1–5 (docs only, no code, no DB write). Step 6 is a fenced contingency that
does **not** fire on the current DB.*

**Step 1 — ESCALATION GATE FIRST (HALT for OQ-2).** Present the live probe result (§2) and the
recommendation **(a)+(b)**. The disposition **form** is a roadmap-level judgment call —
**STOP here** for VSC-User's choice. Steps 2–5 assume disposition (a)/(b); they write **no
code and no DELETE**. *Files: none.*

**Step 2 — Re-run the OQ-1 probe READ-ONLY at execution time (staleness gate).** The DB is
live and could change between plan and execution.

```sql
SELECT source_version_id, version, record_count
FROM annotation_source_versions
WHERE source_db='gnomad'
  AND source_version_id NOT IN (SELECT COALESCE(current_source_version_id,-1)
                                FROM annotation_sources WHERE source_db='gnomad')
  AND source_version_id NOT IN (SELECT DISTINCT source_version_id FROM gnomad_frequencies)
ORDER BY source_version_id;
```

**GATE:** if it returns `[]` (expected per §2 + obs #4), proceed with the docs-only closure
and execute **no DELETE**. If it **unexpectedly** returns rows, **STOP and escalate** — this
docs-only plan no longer applies; re-plan via the Branch-B contingency (step 6, fires OQ-3).
*Files: `data/genome.duckdb` (read-only SELECT; not modified).*

**Step 2a — (folded, Appendix B-3) Re-read the live `record_count` tuple at WRITE time.** Any
literal transcribed into docs (steps 3–5) must come from this live read, **not** from this
plan's text:

```sql
SELECT source_version_id, record_count FROM annotation_source_versions WHERE source_db='gnomad';
```

Assert it equals `{(8, 4467370), (10, 4568802)}` before committing any doc. This closes the
only residual stale-literal exposure (the same defect class PR-7 itself exists to fix).
*Files: `data/genome.duckdb` (read-only).*

**Step 3 — Strike-correct finding-015 §12 inline (closes the freshness flag).** Edit
`docs/findings/finding-015-gnomad-v10-audit-trail-anomaly.md` §12 (lines 144–149): add an
**inline** marker **carrying a distinctive sentinel token** *(folded, Appendix B-1/B-2)* at
the head of item 12, immediately adjacent to (and/or as strikethrough on) the
`DELETE … IN (6, 7, 8, 10)` block — e.g.:

> **`[SUPERSEDED · STALE · DO-NOT-RUN · PR7-MOOT-2026-06-23 — see Amendment below: the live
> DB has NO zero-row orphans; id=8 and id=10 BOTH carry data]`**

Do **not** delete the §12 body (preserve the audit trail per decision #8) — annotate it. Then
append a one-paragraph closing note to the existing Amendment (after line 177) recording the
probe verbatim: *"PR-7 probe (read-only, 2026-06-23): the zero-row-orphan query returns 0
rows; gnomad `annotation_source_versions` = {8 (4,467,370 rows, superseded-with-data), 10
(4,568,802 rows, active)}; both ids carry `gnomad_frequencies` data. PR-7 closed as moot — no
FK-safe orphan exists."* (If disposition (b) is chosen, this note also serves as the Option-A
docs sentence.) *Files: `docs/findings/finding-015-gnomad-v10-audit-trail-anomaly.md`.*

**Step 4 — Flip the ROADMAP PR-7 checkbox and rewrite the slot as closed-as-moot.** Edit
`ROADMAP.md` line 149 `[ ] **PR 7**` → `[x] **PR 7**`; rewrite the slot body (149–160) to the
empirical disposition (live probe = 0 orphans; only `id=8` superseded-with-data + `id=10`
active; no v6/v7/v9; the stale DELETE would have erased active + superseded → no DELETE run;
Option-B prevention already shipped in PR #53; the general superseded-row cleanup incl. `id=8`
is PR-9). Fold the existing ⚠️ note content into the closure rationale; keep the finding-015
Amendment + obs #4 cross-references. Advance the running-status lines (5 and 89) to **PR-7
closed, PR-8 next**. Do **not** alter the PR-8/PR-9 slots. *Files: `ROADMAP.md`.*

**Step 5 — Add a CHANGELOG `[Unreleased]` entry (closure with probe evidence).** One-to-two
sentences per convention, PR ref at commit time, naming the `{8, 10}` inventory, the "no
DELETE executed" fact, and that Option-B prevention already shipped (PR #53) while the general
cleanup remains PR-9. **Append** — do not delete the existing ⚠️-guard entry (the historical
record this closure supersedes). *Files: `CHANGELOG.md`.*

**Step 6 — BRANCH B (contingency; NOT applicable on the current DB).** Documented because the
risk-first angle plans the failure mode. Fires **only** if the step-2 re-probe unexpectedly
returns rows (contradicting obs #4 — e.g. a `kill -9` mid-load creating a partial-run orphan
the Option-B guard cannot undo). Then: **STOP and re-escalate OQ-3**; reconcile *why* the live
state diverged first; for **each** candidate id — (1) re-verify `SELECT COUNT(*) FROM
gnomad_frequencies WHERE source_version_id=<id>` returns 0 (the OQ-3 invariant, verified live,
never assumed); (2) re-verify `id != annotation_sources.current_source_version_id`; (3)
**auto-snapshot** `data/genome.duckdb` to `archive/` first (mirroring the `canonicalize`
convention — the gitignored DB has no other recovery path); (4) execute a **parameterized**
DELETE built **only** from the live probe output (**never** the hardcoded `(6,7,8,10)`
literal); (5) wrap in one transaction, capture before/after counts, re-probe to assert 0
orphans remain and the pointer + `id=8`/`id=10` counts are unchanged. This branch is **not
mechanical** and requires the Branch-B tests (§5) + a `/code-review` + `phi-pii-guardian` pass
before execution. *Files: `data/genome.duckdb` (snapshot + conditional DELETE), `ROADMAP.md`,
`CHANGELOG.md` — only if Branch B fires.*

---

## 5 · Tests (`backend/tests/`)

- **Moot path (expected): NONE.** The closure is docs-only — no importable code, no schema, no
  DB mutation — so there is no behavior to assert. A unit test asserting a one-off DB-state
  against the live gitignored DB is not a repeatable pytest fixture and is out of contract.
  The runtime orphan-**prevention** path (Option B) already ships with full coverage
  (`test_loaders_gnomad.py:969-1192`). The "test" of the moot fact is the live OQ-1 probe
  assertion in §6.
- **Branch B contingency ONLY** *(does not fire on the current DB)*:
  - A **plan-blind** regression-guard test in `test_loaders_gnomad.py`: seed
    `annotation_source_versions` with a gnomad orphan (zero `gnomad_frequencies` refs) **plus**
    the active-pointer row **plus** a superseded-with-data row; run the probe-driven
    parameterized delete helper; assert **only** the zero-row unreferenced id is removed while
    the active + data-referenced rows survive (FK-safety + decisions #7/#8).
  - A guard test asserting the delete **never** fires against a hardcoded id list and **never**
    touches `annotation_sources.current_source_version_id`.

**Must still pass (moot path — identical to `main`):**

- `test_loaders_gnomad.py::test_cleanup_orphan_when_chromosomes_run_yields_zero_rows`
- `test_loaders_gnomad.py::test_cleanup_orphan_when_first_chrom_fails_before_any_insert`
- `test_loaders_gnomad.py::test_resume_does_not_cleanup_preexisting_in_flight_row`
- `test_loaders_gnomad.py::test_successful_full_run_does_not_trigger_cleanup`
- `test_annotate_source_versions.py` (full module)
- Full `pytest` suite green with **collected + passed count identical to `main`** (the moot
  path changes no importable code; any drift signals an unrelated problem).

---

## 6 · Verification

*Greps tightened per Appendix B-1/B-2 so they cannot pass tautologically.*

- **OQ-1 zero-row-orphan probe → MUST be empty:**
  ```bash
  uv run python -c "import duckdb; c=duckdb.connect('data/genome.duckdb', read_only=True); \
  print('ORPHANS:', c.execute(\"SELECT source_version_id FROM annotation_source_versions WHERE source_db='gnomad' AND source_version_id NOT IN (SELECT COALESCE(current_source_version_id,-1) FROM annotation_sources WHERE source_db='gnomad') AND source_version_id NOT IN (SELECT DISTINCT source_version_id FROM gnomad_frequencies)\").fetchall())"
  # → ORPHANS: []   (the load-bearing fact that PR-7 is moot)
  ```
- **Negative-control inventory (proves no DELETE occurred):**
  ```bash
  uv run python -c "import duckdb; c=duckdb.connect('data/genome.duckdb', read_only=True); \
  print('INVENTORY', sorted(c.execute(\"SELECT source_version_id, record_count FROM annotation_source_versions WHERE source_db='gnomad'\").fetchall())); \
  print('POINTER', c.execute(\"SELECT current_source_version_id FROM annotation_sources WHERE source_db='gnomad'\").fetchone()); \
  print('FREQ_BY_ID', sorted(c.execute('SELECT source_version_id, COUNT(*) FROM gnomad_frequencies GROUP BY source_version_id').fetchall()))"
  # → INVENTORY [(8, 4467370), (10, 4568802)]; POINTER (10,); FREQ_BY_ID [(8, 4467370), (10, 4568802)]
  ```
- **§12 inline marker present (sentinel-scoped, not tautological):** grep for the distinctive
  step-3 token so the check fails if the inline edit is skipped (the bare
  `SUPERSEDED|STALE|DANGEROUS` grep already matches the pre-existing Amendment and would pass
  without the §12 fix):
  ```bash
  grep -c 'PR7-MOOT-2026-06-23' docs/findings/finding-015-gnomad-v10-audit-trail-anomaly.md   # ≥ 1
  ```
- **Dangerous literal is fenced, not bare:** confirm the §12 `IN (6, 7, 8, 10)` occurrence
  shares a line with (or is immediately preceded by) the sentinel marker — i.e. it is no
  longer a bare, runnable instruction. Inspect the §12 range rather than asserting a global
  count (the literal legitimately appears in the Amendment warning + the ROADMAP/CHANGELOG
  guards):
  ```bash
  sed -n '144,150p' docs/findings/finding-015-gnomad-v10-audit-trail-anomaly.md   # the DELETE block now carries the inline SUPERSEDED marker
  ```
- **ROADMAP closed-as-moot:** `grep -n '\[x\] \*\*PR 7' ROADMAP.md` → matches; status lines 5
  + 89 advanced to PR-8 next.
- **CHANGELOG closure entry:** `grep -n 'PR 7' CHANGELOG.md` → the `[Unreleased]` moot-closure
  entry naming the `{8, 10}` inventory + "no DELETE executed".
- **Dev-loop unchanged (zero `.py`/`.sql` changed on the moot path):**
  `pytest backend/tests/test_loaders_gnomad.py backend/tests/test_annotate_source_versions.py -q`
  (pass, unchanged) · `pytest` full suite (collected/passed identical to `main`) ·
  `ruff check && ruff format --check` · `mypy --strict backend/src` (all clean, identical to
  baseline).

**Expected real-data outputs / anchors (negative control — the correctness *is* that nothing
moves):** OQ-1 = `[]`; gnomad inventory `{(8, 4467370), (10, 4568802)}`; pointer = 10;
`gnomad_frequencies` per-id `{(8, 4467370), (10, 4568802)}`; obs #4 index counts UNMOVED
(`gnomad_matches` 3,054,426 / `row_count` 3,077,001 / `clinvar_matches` 61,926); obs #3
`variants_master` 3,160,364 UNMOVED; `annotation_sources` total = 7.

---

## 7 · Out of scope

- **PR-9** — the general periodic orphan-row cleanup for **all** superseded `source_version_id`s
  across **all** loaders (incl. the data-bearing gnomad `id=8` and `variant_aliases` orphans)
  + its runbook entry (finding-010 #14). PR-7 is the gnomAD-specific zero-row one-off only.
- **Deleting the superseded `gnomad_frequencies` DATA rows under `id=8`** (4,467,370 rows).
  Kept-by-design under the version-pointer pattern (decision #7 / finding-010 #6) — data-
  referenced, **not** zero-row orphans, FK-unsafe to touch.
- **A new CLI subcommand** (e.g. `genome annotate list-orphan-versions`) — PR-7 is a one-off
  fix, not recurring operational tooling (Phase-7+).
- **A net-new `verification.md` gate block for the probe** — a one-off slot should not add a
  recurring durable verification surface (leans PR-9 / Phase-7+). *(Divergence: gate-backward
  proposed it optionally; the other three angles + the synthesis keep it out — see Appendix A.)*
- **Any change to `gnomad.py` `_cleanup_orphan_version_row` / the post-loop guard** (shipped
  Option-B, PR #53); `source_versions.py` and `cli.py` unchanged.
- **Any `docs/schemas/` or `ddl/` edit** (schema immutable, Things-never-to-do #1).
- **Re-running the gnomAD load / refresh / refresh-index / any pointer flip** — the moot
  branch performs no DB write; obs #4 + obs #3 anchors stay frozen.
- **Executing the stale `IN (6,7,8,10)` DELETE** — explicitly forbidden (erases active `id=10`
  + superseded `id=8`).
- **Reconciling the historical v6/v7/v9 ids** from finding-015's pre-rebuild table — they no
  longer exist; the §2–13 body is documentation of a superseded state, left intact (only the
  §12 inline marker + Amendment closing note are added).
- **Resolving OQ-2's disposition form unilaterally** — VSC-User's call (all three options agree
  "no DELETE").

---

## 8 · End-of-session handoff

At implementation-session end, run `/handoff`. New branch from `main`; the moot path is
**docs-only** (finding-015 + ROADMAP + CHANGELOG) — dev-loop identical to `main` (no `.py`/
`.sql` changed); commit + push; open PR carrying the CHANGELOG entry. The handoff records the
probe evidence, the "no DELETE executed" fact, and the OQ-2 disposition VSC-User chose.

---

## Appendix A · `plan-phase.js` run provenance

Run `wf_2ec21d3c-46e` · 15 agents · ~1.10M subagent tokens · 1 cycle · ~18.5 min. The
orchestrator seeded the dispatcher with a read-only live-DB probe (the ROADMAP "re-derive
against the live DB first" precondition); the synthesizer and **both** auditors independently
re-ran it.

| Stage | Members | Outcome |
|---|---|---|
| 0 · Intake | `scope-dispatcher` | manifest; **risk_tier 2** — back-test S=4→tier 1, **+1 for the open question** (probe-required / stale-id-list) → `min(2, 1+1)=2`; `deep_T2=false` (S<7, A=0). `change_class=[data-backfill]` → docs-only on the moot path. `applicable_anchors=[]`. 1 freshness flag: finding-015 §12 has no inline strikethrough. |
| 1 · Planners | `planner` ×4 (minimal-diff, gate-backward, risk-first, convention-purist) | 4 candidate plans (self-confidence 0.82–0.90). 3 of 4 ran the OQ-1 probe read-only and got `[]`; minimal-diff inferred moot from obs #4. |
| 1 · Judges | `plan-judges` ×5 | **correctness → gate-backward** (only angle to resolve the moot fact empirically) · **locked_decision_fit → convention-purist** · **verification → convention-purist** · **scope_discipline → minimal-diff** · **risk → risk-first** (the only angle carrying the `archive/` snapshot control). |
| 1 · Synthesis | `plan-synthesizer` | gate-backward skeleton + convention-purist §3/§6 + risk-first Branch-B snapshot + minimal-diff §7; **independently re-ran the OQ-1 probe** and confirmed `[]`. Panel confidence **0.88**. |
| 1.5 · Pre-mortem | `plan-premortem` ×2 (anchor-drift, schema-assumption) | **proceed** — no surprise above low likelihood on the moot path; flagged the plan-time `record_count` literal as a stale-number risk (folded, B-3) and the Branch-B FK-sufficiency invariant (verified live: ids 8/10 confined to `gnomad_frequencies`). |
| 1 · Audit | `plan-auditor` ×2 (contract, architecture-fit) | both **ready** — architecture-fit independently re-ran OQ-1 + verified all line-number anchors; locked decisions #7/#8 + schema-immutability respected; reading-list fully covers edited files. 0 blockers; 3 `minor` §6 precision findings (Appendix B). |
| Routing | — | **verdict `ready` · premortem `proceed` → human plan-approval gate (VSC-User)**. No revise cycle needed. `auto_approved=false`. |

**Divergences surfaced** (the ensemble's variance → your open questions): **OQ-2 disposition
form** (a/b/c — the genuine open call) → VSC-User; **net-new `verification.md` block?** (1 yes
/ 3 no) → kept out by default; **Branch-B pre-mutation snapshot?** (1 explicit / 3 absent) →
adopted into the contingency.

---

## Appendix B · Folded-in findings (non-blocking precision improvements)

The auditor verdict was **ready** with zero blockers; these three `minor` (+ one `info`)
findings were deterministic and convergent, so they were folded into this plan rather than
triggering a revise cycle.

1. **Tautological §12 grep** *(folded into §6, step 3).* The original
   `grep -nc 'SUPERSEDED|STALE|DANGEROUS|DO NOT RUN|see Amendment'` already returns ≥1 today
   (the existing Amendment satisfies it), so it would pass even if the §12 inline marker were
   never added. → §6 now greps a **distinctive sentinel token** (`PR7-MOOT-2026-06-23`) that
   only the step-3 inline edit introduces.
2. **Imprecise "dangerous literal" grep** *(folded into §6, step 3).* `IN (6, 7, 8, 10)`
   legitimately appears in the Amendment + ROADMAP/CHANGELOG guards, so a global count can't
   isolate the bare §12 occurrence. → §6 now inspects the §12 line range to confirm the DELETE
   block **shares a line with the marker** (no longer bare/runnable).
3. **Plan-time `record_count` literals** *(folded as step 2a + step 3 instruction; raised by
   both the pre-mortem and the contract auditor).* Steps 3–5 transcribe `4,467,370` /
   `4,568,802` — the exact stale-number defect class PR-7 exists to fix. → the implementer
   **re-reads the live `record_count` tuple at write time** and transcribes from that, never
   from this plan's text.
4. **Provenance nit (`info`, corrected here).** The synthesizer's note said the probed DB
   mtime was `2026-06-23 13:34`; the DB mtime is actually **`2026-06-23 10:58:30`** (13:34 was
   `CHANGELOG.md`'s mtime). Same day; both auditors re-probed and got identical results, so the
   moot conclusion is unaffected. The **step-2 execution-time re-probe** is the real staleness
   gate regardless.
