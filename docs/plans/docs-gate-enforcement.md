# docs-gate-enforcement — make `genome docs check` automated + blocking

> **Status:** Plan — **APPROVED at Human Gate 1 (2026-06-24); decisions locked below.** Produced
> by the per-scope agent team (finding-034), driven via the Agent tool. Now executing Stage 2/3.
>
> **Scope id:** `docs-gate-enforcement` · **Risk tier:** 2 · **Change class:** [ci, tooling,
> docs, **cli**] (cli added by the OQ-5 config-free fix) · **Real-data anchors applicable:** none
> (negative-control — writes no DB, runs no pipeline, must not move a digit under CLAUDE.md
> "Real-data observations").
>
> **Origin:** the deferred Follow-up in `finding-036` (lines 69–70): the CI/pre-commit hook for
> `genome docs check` was deferred. The gate exists and is DB-free (DEC-0085 made `genome.cli`
> pysqlcipher3-lazy) but nothing runs it. This scope closes that gap — the **deliberate reversal**
> of the finding-036 deferred-hook stance, recorded as **DEC-0086**.

---

## ✅ Decisions locked at Human Gate 1 (VSC-User, 2026-06-24)

- **OQ-1 → BOTH.** Implement **Form A** (tracked git pre-commit hook + installer) **and Form B**
  (GitHub Action) **and** the `scripts/verify.sh` fold-in — defense-in-depth: a fast local
  speed-bump *plus* the un-bypassable PR gate. Form B is the repo's **first `.github/`**.
- **OQ-5 → land it now.** Add the one-time `genome.cli`/`genome.config` change that makes the
  docs gate **truly config-free** (no `APP_DB_PASSPHRASE` needed). **Consequence:** the test, the
  hook, and the CI workflow **no longer set/depend on a passphrase env**, and the fresh-clone
  crash (F10) is *eliminated at the source*, not merely accommodated.
- **OQ-2 (framing) accepted:** the code delivers the un-skippable CI signal; **merge-blocking
  still requires the out-of-code repo-admin "required status check" toggle** on `main` (operator
  step in §6/§8). Form A is `--no-verify`-bypassable + per-clone by design.
- **OQ-3 → yes** (gate into verify.sh). **OQ-4 → moot** for the hook; Form B uses a verified
  cyvcf2-skip install (Task 6). Reject the `pre-commit` framework. Part-A is README + verification.md
  prose only (CLAUDE.md already documents the gate, lines 9/172 — no re-edit).

The original escalation analysis (the 3-vs-1 OQ-1 split, the merged riskiest-assumptions, OQ-2's
table) is preserved in the Appendix as the decision record.

---

## 0 · Constraints (locked)

- **Locked #9 local-first** — honored: Form B touches only tracked markdown (no genome data, no
  `data/`/`archive/`, never `git add -A`). The config-free fix does not weaken the DB passphrase
  requirement for any DB command.
- **Negative control (hard).** `git diff --name-only main..HEAD -- docs/schemas/ ddl/` **empty**;
  **no CLAUDE.md "Real-data observations" digit change**; no `rm -rf data/`; no DB write/pipeline.
- **No schema/ddl change** (Things-never-to-do #1).
- **Any new/edited `.py`:** ruff `--select=ALL` (+ repo ignores), `ruff format --check`, `mypy
  --strict backend/src`, **structlog / no `print()`**. `backend/tests/**` per-file ignores cover
  the new test.
- **Config-free fix carries no silent swallow** — logging gets its own minimal settings; it does
  **not** try/except-and-ignore a `ValidationError` (the silent-failure lens will check this).
- **Shell scripts:** `#!/usr/bin/env bash` + `set -euo pipefail`; exit-code propagation preserved
  (no exit-masking pipe).
- **Minimal-dep:** no new Python dependency; no `pre-commit` framework.
- **No `genome.docs` body change** — wrap and invoke only. The only `backend/src` change is the
  surgical OQ-5 config-free fix in `genome.config` + `genome.cli._configure_logging`.
- **Hook stays gate-only** (~0.5 s); never the dev-loop.
- **Ledger discipline.** DEC-0086 is a **new append** under the column header (line 57),
  **contiguous with the DEC-0085 row (line 143), no blank line between rows** (the parse-boundary
  trap); not a flip; anchors referenced-not-copied; this plan is committed to
  `docs/plans/docs-gate-enforcement.md` so DEC-0086's detail-link resolves on disk.

---

## 1 · Reading-list confirmation

**Docs.** `CLAUDE.md` (gate already named at 9/172 — **not re-added**); `docs/runbooks/
verification.md` (62–67 document the gate as *manual* — prose to flip); `README.md` (`##
Development` 203–215 — omits the gate/MEMORY.md/verify.sh — the Part-A gap); `finding-036` (69–70
deferred-hook prose to amend); `CHANGELOG.md` `[Unreleased]`; `MEMORY.md` (col header 57; markers
55/145; **DEC-0085 line 143, blank 144, END 145**; high-water DEC-0085).

**Code.** `backend/src/genome/config.py` (`Settings` requires `app_db_passphrase` no-default
line 27; `log_level` default "INFO" line 38; `model_config` env_file=".env" extra="ignore" 18–23;
`get_settings` lru_cache 41–47 — the OQ-5 fix site); `backend/src/genome/cli.py`
(`_configure_logging` 83–94 calls `get_settings().log_level`; root `@app.callback() _main` 97–99;
eager `from genome.annotate import annotate_app` 15 — the 7-line stdout DEBUG noise; `from
genome.config import get_settings` 16); `backend/src/genome/docs/cli.py` (`raise typer.Exit(code=1)`
84; exit strings `docs check: FAIL — N violation(s)` 83 / `docs check: OK — capture + retrieval +
lifecycle all hold` 85); `backend/src/genome/docs/ledger.py:101–126` (`iter_data_rows` stops at
the first non-`|` line after the header — the blank-line trap); `backend/src/genome/docs/
validator.py` (`DUPLICATE_DEC_ID` seed 223, git-independent; `_retrieval_violations` always-runs
443 → `MISSING_INDEX_MARKER` needs a marker'd findings README; in-place fail-open with no HEAD
baseline 364–365; `_ANCHOR_NUM_RE` comma-grouped-only 90 — the DEC-0086 cell can't trip it);
`backend/tests/test_docs_validator.py` (`_readme_with_index` 99 + `_write_repo` 114 — reuse for
the clean fixture); `backend/tests/test_docs_no_db_import.py` (the `_run_probe` clean-subprocess
harness pattern); `scripts/verify.sh` (`run_step` 19–27, free exit-propagation; `set -euo
pipefail` 10; runs from repo root); `pyproject.toml` (`[project.scripts] genome = genome.cli:app`;
`[tool.uv] no-binary-package=["cyvcf2"]`).

**Repo-state (orchestrator-VERIFIED):** `.github/` absent (Form B = first CI); gate config-NOT-free
today (root callback builds `Settings()` → needs the passphrase — fixed by OQ-5); `DUPLICATE_DEC_ID`
git-independent (ideal seed); clean fixture needs a marker'd findings README; the anchor rule
matches only comma-grouped digits; 22/22 live detail-links resolve on disk.

---

## 2 · Problem statement

The `genome docs check` gate (CAPTURE/RETRIEVAL/LIFECYCLE over `MEMORY.md` + finding frontmatter,
finding-036) **exists and is correct, but nothing runs it automatically.** (1) No CI (`.github/`
absent). (2) No git hook. (3) Absent from `scripts/verify.sh` (5 steps, lines 29–33). (4)
verification.md:62–67 frames it as *manual*. (5) finding-036:69–70 still records the deferral.
Two latent flaws compound it: the gate **crashes without `APP_DB_PASSPHRASE`** (the CLI root
callback builds `Settings()` — so it isn't truly fresh-checkout-runnable as DEC-0085 implied), and
**README's `## Development` omits the gate** entirely. **Net:** a dirty ledger (duplicate/
non-monotonic DEC id, orphan supersession, missing DEC row, in-place rewrite) can be committed +
merged with zero automated friction.

---

## 3 · Implementation plan

Riskiest-first. **Task 1 (the frozen-module config-free fix) and Task 2 (the anti-theatre
falsifier) land + gate first**, because everything else (hook, CI, test assertions) depends on the
gate being config-free and on the falsifier proving the gate actually fails a bad tree.

**Task 1 — OQ-5 config-free logging (the only `backend/src` change; frozen-module, riskiest).**
- `backend/src/genome/config.py`: add a minimal `LoggingSettings(BaseSettings)` — same
  `model_config` (env_file=".env", encoding, case_sensitive=False, **extra="ignore"**) and a
  single field `log_level: str = Field(default="INFO")` (**no `app_db_passphrase`**, so it
  constructs with no `.env`) — plus `@lru_cache(maxsize=1) def get_logging_settings() ->
  LoggingSettings: return LoggingSettings()`.
- `backend/src/genome/cli.py`: `_configure_logging()` reads `get_logging_settings().log_level`
  (import `get_logging_settings` alongside the existing `get_settings`). **No try/except, no
  swallow** — logging simply no longer depends on DB credentials; DB commands still call
  `get_settings()` and fail loudly without the passphrase (unchanged; `get_settings`'s lru_cache
  doesn't cache the failure, so each DB command re-validates).
- Rationale comment on `LoggingSettings`: "logging must configure on any `genome` invocation —
  including `genome docs check` on a fresh checkout with no `.env` — so it reads only `log_level`
  and never requires the DB passphrase." Step-gate: from a no-`.env` cwd, `genome docs check`
  reaches the ledger logic (no `ValidationError`); a DB command (`genome status`) still raises
  without the passphrase.

**Task 2 — Anti-theatre substance test (write against the frozen interface; config-free now).**
New `backend/tests/test_docs_gate_enforcement.py`. Build a **complete clean fixture** in
`tmp_path` reusing `_readme_with_index` + `_write_repo` (test_docs_validator.py:99,114): a
`CLAUDE.md` (so `_repo_root()` anchors), a real-shaped `MEMORY.md`, **and a
`docs/findings/README.md` with valid `<!-- BEGIN/END findings-index -->` markers + a consistent
index** (else `MISSING_INDEX_MARKER` false-fails the clean case). Then:
- **Clean → PASS:** invoke the real wrapper (subprocess `genome docs check`, `cwd=fixture`, **no
  `APP_DB_PASSPHRASE` in env** — config-free after Task 1) → exit **0** and `"docs check: OK —
  capture + retrieval + lifecycle all hold" in stdout` (substring — 7 `annotate.registry.register`
  DEBUG lines precede the verdict).
- **Seeded-bad → FAIL:** insert a `DUPLICATE_DEC_ID` row (verbatim duplicate of an existing
  `DEC-NNNN`) **contiguous with the last data row, NO blank line** (ledger.py:119–121) → exit
  **non-zero AND `"DUPLICATE_DEC_ID" in stdout`** (the specific code, not bare rc≠0).
- A **config-free regression assertion:** `genome docs check` runs to a verdict with
  `APP_DB_PASSPHRASE` absent from the env (locks Task 1). Docstring pins all three rationales.

**Task 3 — Fold the gate into `scripts/verify.sh` (OQ-3).** One line after the mypy step
(verify.sh:33): `run_step "genome docs check" uv run genome docs check`. Exit propagation inherited
from `run_step`; runs from repo root. No other change.

**Task 4 — Form A: tracked git pre-commit hook.** New executable `scripts/git-hooks/pre-commit`:
```bash
#!/usr/bin/env bash
set -euo pipefail
# finding-036 decision-tracking gate (DEC-0086). Gate-only: ~0.5s, DB-free + config-free.
# --no-sync reuses the synced dev venv (never triggers a cyvcf2 from-source build).
exec uv run --no-sync genome docs check
```
`set -euo pipefail` + `exec` (the gate's exit IS the hook's exit, no swallow); `--no-sync` (no
cyvcf2 build); gate-only. **No `.env` dependency** now that Task 1 made the gate config-free.

**Task 5 — Form A: idempotent, robust installer.** New executable `scripts/install-hooks.sh`:
resolve the hooks dir via **`git rev-parse --git-path hooks`** (honors `core.hooksPath`,
worktree-safe), `mkdir -p`; if `<hooksdir>/pre-commit` exists and is **not** our symlink, **refuse
+ error** (don't clobber a foreign hook); else link with an **absolute** target
`ln -sf "$(git rev-parse --show-toplevel)/scripts/git-hooks/pre-commit" "<hooksdir>/pre-commit"`
(absolute avoids the relative-symlink-resolves-from-the-link's-dir bug), `chmod +x`; **self-test**
that the link resolves to an existing executable; print a confirmation. `set -euo pipefail`;
idempotent.

**Task 6 — Form B: the GitHub Action (first `.github/`).** New `.github/workflows/docs-gate.yml`:
trigger `pull_request` + `push` to `main`; job `docs-check` on `ubuntu-latest`:
`actions/checkout@v4` → `astral-sh/setup-uv@v5` → a **cyvcf2-build-avoiding install** → `uv run
genome docs check` as the final step (its non-zero exit fails the job; **no `|| true`,
`continue-on-error`, or post-gate `exit 0`** — silent-failure-hunter target). **No
`APP_DB_PASSPHRASE` env** (config-free after Task 1). **Verify the install recipe before
committing** — candidate `uv sync --no-install-package cyvcf2` (skips the from-source build the
gate never needs); if it doesn't resolve typer/structlog, fall back to `uv sync --only-group dev`
+ `PYTHONPATH=backend/src uv run --no-project python -m genome.docs.cli check`. **Operator step
(out-of-code, OQ-2):** a repo admin sets `docs-check` as a required status check on `main` for
merge-blocking — documented in §6/§8, not committable.

**Task 7 — Part-A README discoverability (no CLAUDE.md re-edit).** Edit `README.md` `## Development`
(203–215): add `genome docs check` to the command list + a one-line pointer that `scripts/verify.sh`
runs the full local protocol (incl. the gate) and `./scripts/install-hooks.sh` installs the
pre-commit gate once. Reference finding-036; no anchor digit.

**Task 8 — verification.md prose flip (manual → automated).** Edit verification.md:62–67: reframe
from *"Run it whenever…"* to *"now automated — runs in `scripts/verify.sh`, at the pre-commit
boundary where the hook is installed, and as the `docs-check` GitHub Action on every PR (made
merge-blocking by the repo-admin required-status-check on `main`)."* Keep the DB-free + exit-0
facts. Prose flip on the existing block.

**Task 9 — finding-036 Follow-up amendment.** Edit finding-036:69–70: deferred → **landed** under
DEC-0086 (BOTH forms + config-free fix). **Prose only — leave the frontmatter untouched.**

**Task 10 — DEC-0086 ledger row.** Append **immediately after the DEC-0085 row (line 143), NO blank
line between rows**: `| DEC-0086 | tactical | 2026-06-24 | active | — | VSC-User,
ClaudeCodeDevelopment | docs/plans/docs-gate-enforcement.md | Made the decision-tracking gate
automated + blocking via BOTH a tracked git pre-commit hook and a GitHub Action + a verify.sh step,
and made the gate config-free (logging no longer needs the DB passphrase); reverses the finding-036
deferred-hook stance | docs/plans/docs-gate-enforcement.md |`. New append, not a flip. No
comma-grouped digit (can't trip `COPIED_ANCHOR_NUMBER`). **Verify the row registered:**
`parse_ledger(MEMORY.md)[-1].dec == "DEC-0086"` (not merely "gate exits 0").

**Task 11 — CHANGELOG `[Unreleased]`.** One entry: the gate is now automated + blocking (verify.sh
fold-in + a pre-commit hook + a GitHub Action) and config-free, with a PR ref.

---

## 4 · Tests

**New** (`backend/tests/test_docs_gate_enforcement.py`):
- `test_gate_blocks_seeded_bad_ledger` — full clean fixture + **contiguous** `DUPLICATE_DEC_ID` →
  rc≠0 AND `"DUPLICATE_DEC_ID" in stdout` (the F1 falsifier).
- `test_gate_passes_clean_ledger` — full clean fixture → rc 0 AND `"docs check: OK — …" in stdout`.
- `test_gate_runs_config_free` — `genome docs check` reaches a verdict with `APP_DB_PASSPHRASE`
  absent from the env (locks Task 1's config-free fix).
- (Optional, recommended by silent-failure) `test_db_command_still_requires_passphrase` — a DB
  command / `get_settings()` still raises `ValidationError` without the passphrase (proves Task 1
  didn't over-broaden).

**Must still pass (no existing test mutated):** `test_docs_no_db_import` probes; `test_docs_cli.py`
(exit-string contract); `test_docs_validator.py` (`DUPLICATE_DEC_ID` + the reused fixtures);
`test_cli_no_pysqlcipher.py` (the lazy-import lock — Task 1 must not regress it); **`genome docs
check` exits 0 on this PR's own tree** (dogfood — DEC-0086 contiguous + monotonic; detail-link
resolves once the plan doc is committed); full dev-loop (`pytest` +tests, `ruff check`, `ruff
format --check`, `mypy --strict backend/src`).

---

## 5 · Verification

- **Anti-theatre falsifier (F1):** the new test pair green — seeded-bad (contiguous duplicate) →
  rc≠0 + `DUPLICATE_DEC_ID`; clean → rc 0 + `docs check: OK …`.
- **Config-free (OQ-5):** from a no-`.env` cwd, `env -u APP_DB_PASSPHRASE uv run genome docs check`
  reaches a verdict (no `ValidationError`); `genome status` (or `get_settings()`) still raises
  without the passphrase.
- **Form A e2e:** `./scripts/install-hooks.sh` links the hook (absolute target; self-test green;
  re-run no-op; refuses a foreign hook); a contiguous-duplicate commit is rejected at pre-commit;
  `--no-verify` bypasses. `bash scripts/verify.sh` runs the new step and fails the run on a dirty
  ledger (`FAILED at genome docs check`).
- **Form B e2e:** the workflow runs `genome docs check` on PR/push; the chosen install completes
  **without a from-source cyvcf2 build**; the gate step has no swallow; a dirty-ledger PR shows
  `docs-check` red. **Merge-blocking requires the repo-admin required-status-check toggle** on
  `main` (operator step — surfaced, not code).
- **Dev-loop clean:** `pytest`, `ruff check`, `ruff format --check`, `mypy --strict backend/src`.
- **Negative control (hard):** `git diff --name-only main..HEAD -- docs/schemas/ ddl/` **empty**;
  `git diff main..HEAD -- CLAUDE.md` shows **no** edit; no `rm -rf data/`; `genome docs check`
  exits 0 on the PR's own tree; `parse_ledger(MEMORY.md)[-1].dec == "DEC-0086"`.
- **Expected gate strings:** FAIL → `[lifecycle/DUPLICATE_DEC_ID] DEC-00NN: duplicate DEC id` then
  `docs check: FAIL — N violation(s)`; OK → `docs check: OK — capture + retrieval + lifecycle all
  hold` (both after 7 harmless `annotate.registry.register` DEBUG lines).

---

## 6 · Out of scope

- The `pre-commit` framework / `.pre-commit-config.yaml` (minimal-dep).
- Any other check in CI (ruff/mypy/pytest as a workflow) — only the decision-tracking gate.
- The branch-protection required-status-check toggle **as code** (repo-settings operator action).
- Broadening the config-free change beyond logging (e.g. making `Settings.app_db_passphrase`
  optional) — Task 1 is surgical: a logging-only settings class; DB commands still require it.
- Silencing the 7 `annotate.registry.register` DEBUG lines (an annotate import-time behavior).
- Any CLAUDE.md gate-existence re-edit (already at 9/172).
- Any `docs/schemas/` or `ddl/` edit, `rm -rf data/`, or new Python dependency.

---

## 7 · End-of-session handoff

`/handoff` at session end. New branch from `main`; **commit this plan to
`docs/plans/docs-gate-enforcement.md`**; clean dev-loop **and `genome docs check` exiting 0** on
the branch's own tree (dogfood). Commit + push; open a PR with the CHANGELOG `[Unreleased]` entry.
Append **DEC-0086** (contiguous) and amend the **finding-036 Follow-up** deferred→landed. **Operator
follow-up to surface in the PR body:** merge-blocking is unrealized until a repo admin sets the
`docs-check` GitHub Action as a required status check on `main`.

---

## Appendix · plan-phase provenance + the Gate-1 decision record

| Stage | Members | Outcome |
|---|---|---|
| **0 · Intake** | `scope-dispatcher` | manifest; tier 2; change_class [ci, tooling, docs] (+cli post-OQ-5); anchors none; next id DEC-0086; flagged the cyvcf2 Form-B hazard. |
| **1 · Planners** | `planner` ×4 (minimal-diff · gate-backward · risk-first · convention-purist) | 4 plans; conf 0.83/0.82/**0.85**/0.83; **OQ-1 split 3-vs-1** (3× Form A, 1× BOTH/Form-B-spine); convention-purist caught the Part-A-already-done correction. |
| **1 · Synthesis** | `plan-synthesizer` | risk-first skeleton + grafts (gate-backward OQ-2 framing + branch-protection step; convention-purist Part-A narrowing; minimal-diff `--no-sync` hook; risk-first F-table + installer). Judge fan-out compressed given convergence. |
| **1.5 · Pre-mortem** | `plan-premortem` ×2 (mechanism · convention) | **revise** — both reproduced findings live. 3 blockers + 3 warns folded (config-not-free/F10; relative-symlink/F9; blank-line insert/F-S3; clean-fixture README; stdout substring; detail-link-resolves). |
| **1 · Audit** | `plan-auditor` (contract + arch-fit) | **ready** — all 8 contract points pass; fixes confirmed folded; OQ-1 not pre-decided; negative-control intact; arch-fit attested. 2 warns / 2 nits (nits fixed). |
| **— · Human Gate 1** | **VSC-User** | **APPROVED.** OQ-1 → **BOTH**; OQ-5 → **land the config-free fix**; OQ-2 framing accepted; OQ-3 yes; reject pre-commit framework. (This decision record + the decisions-locked banner.) |

**The OQ-1 decision record (why BOTH is coherent):** the panel split 2-way on what "blocking"
means — commit-blocking-where-installed (Form A) vs un-bypassable-by-author (only Form B). BOTH
takes both: Form A is the fast local speed-bump; Form B (once the admin toggle is set) is the
un-bypassable PR gate. The OQ-5 config-free fix removes the one wart that made both forms awkward
(the passphrase dependency).

**Surviving risk carried into implementation:** *(S5)* the Form-B workflow is **advisory until the
repo-admin required-status-check toggle is set** — the "shipped enforcement, nothing enforced" trap;
surfaced in §6/§8 + the PR body, it is the one enforcement step no commit can perform.
