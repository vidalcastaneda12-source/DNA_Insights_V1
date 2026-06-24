# Decision-Tracking Follow-ups — discoverability docs + `genome.cli` lazy-import

> **Status:** Plan — produced by the `plan-phase.js` per-scope agent team (finding-034),
> driven via the Agent tool. **Pre-gate package for VSC-User approval; not implemented.**
>
> **Scope id:** `decision-tracking-followups` · **Risk tier:** 2 (S=3, bumped from 1 by open
> questions + the deferred-risk precedent) · **Change class:** docs + cli (import-graph
> refactor) · **Real-data anchors applicable:** none (negative-control).
>
> **Origin:** the two disclosed follow-ups to PR #94 (`finding-036`, the decision-tracking
> ledger). Part A: the new `genome docs check` gate isn't discoverable from the two most-read
> operator docs. Part B: the deferred `genome.cli` fresh-checkout lazy-import (escalated during
> PR #94 as risky; VSC-User deferred it).

---

## 0 · Escalations to VSC-User (resolve at the plan-approval gate)

Two are genuine judgment calls; the rest are resolved in-plan and flagged only for confirmation.

1. **One PR or two? (genuine call.)** Parts A (docs-only, trivial) and B (the central-CLI
   refactor) are independently shippable. **Recommendation: one PR** — both are small, share the
   `finding-036` context, and Part A's `verification.md` line reads better once Part B makes the
   gate fresh-checkout-runnable. Choose **two** if you want the trivial docs to land immediately
   and isolate the refactor's blast radius for its own review.
2. **Keystone mechanism for the `sqlcipher_connection` re-export (genuine call — the audit
   sharpened this).** Two viable approaches; the implementation plan (§4) is written for (a),
   but the audit's analysis modestly favors (b):
   - **(a) lazy `__getattr__`** in `genome/db/__init__.py` (defers the import to attribute
     access) + a `TYPE_CHECKING` `as`-re-export. **Verified ruff+mypy clean** against the real
     repo config — but **only with two `# noqa` markers** the first draft omitted (`# noqa:
     ANN401` on the `__getattr__` signature, `# noqa: PLC0415` on the deferred import; both
     confirmed by the pre-mortem *and* the auditor). Preserves the public path `from genome.db
     import sqlcipher_connection`, but introduces a PEP-562 pattern with **no in-repo precedent**.
   - **(b) drop the package re-export** and point the **two** consumers (`test_connections.py`,
     `test_init_schema.py`) at `from genome.db.sqlite_conn import sqlcipher_connection` directly.
     Zero dynamic-attribute machinery, zero new `noqa`, convention-purer. Cost: a one-symbol
     public-API trim + 2 test-import edits.
   - **Decision input (audit):** the keystone's mypy-precision benefit accrues to **no current
     caller** — the only 2 consumers are tests (under the `backend/tests/** → ANN` per-file
     ignore) using it purely as `with sqlcipher_connection() as conn:`. So (a)'s headline
     advantage (preserving a precisely-typed public import) is moot today. **Recommendation:
     lean (b)** unless you want to keep `from genome.db import sqlcipher_connection` as a stable
     public surface. Either is correct and low-risk.

**Resolved in-plan** (both planners converged; no VSC-User judgment needed):
- **OQ-1 — strategy:** lazy *db imports* (move sqlite imports to function scope), **not**
  deferring the `add_typer` calls. The `add_typer` route is unworkable: cli.py's `DEFAULT_*`
  Typer default-arg constants are evaluated at module load, so the sub-app objects must import
  at module scope anyway.
- **OQ-2 — public API:** **preserve** `from genome.db import sqlcipher_connection` (lazily); do
  not split the package or edit the 2 test callers (unless the fallback in #2 is taken).
- **OQ-3 — regression lock:** **add** a `genome.cli` clean-subprocess probe that asserts the
  **pysqlcipher3-bearing module is absent** (`'genome.db.sqlite_conn' not in sys.modules`) — NOT
  "no `genome.db`": `genome.cli` legitimately imports `genome.db.duckdb_conn` (a clean duckdb
  wheel); only pysqlcipher3 is the fresh-checkout blocker.

---

## 1 · Reading-list confirmation

Read and grounded during planning: `CLAUDE.md` ("Environment requirements" — SQLCipher+FTS5 is
a custom from-source build; tech-stack + conventions), `docs/runbooks/verification.md`,
`docs/findings/finding-036-decision-tracking-ledger.md`; `backend/src/genome/cli.py`,
`genome/db/__init__.py`, `db/init_schema.py`, `db/sqlite_conn.py`, `db/duckdb_conn.py`,
`genome/privacy/external_client.py`, `genome/docs/{__init__,cli}.py`, the heavy sub-app
`__init__`s (annotate/ingest/merge/imputation); `backend/tests/test_docs_no_db_import.py`,
`test_docs_cli.py`, `test_connections.py`, `test_init_schema.py`, `test_cli_phase4.py`,
`test_privacy_external_client.py`; `pyproject.toml` (`[project.scripts] genome = genome.cli:app`;
mypy `pysqlcipher3` ignore-missing-imports).

**Verified facts the plan depends on:**
- `genome.docs` is already DB-free (the `test_docs_no_db_import` probes pass) and `app.add_typer(
  docs_app, name="docs")` is already wired (cli.py:80). This scope is **pure import-time
  decoupling of the central CLI**, not feature code.
- The console script `genome = genome.cli:app` fails to import under absent pysqlcipher3 via
  **four** module-scope sites — see §2.
- `genome.db`'s package `__init__` re-export of `sqlcipher_connection` has **exactly 2**
  consumers, both tests; **zero** src consumers (every src caller imports from
  `genome.db.sqlite_conn` directly). Idiom precedent for function-scope imports:
  **21 `# noqa: PLC0415` sites across 10 src files** (e.g. cli.py:389).

---

## 2 · Problem statement

`genome docs check` is meant to run on a **fresh checkout with no pysqlcipher3 built**, but the
`genome` console script (`genome.cli:app`) can't import there: `import genome.cli` under absent
pysqlcipher3 raises `ModuleNotFoundError: import of pysqlcipher3 halted` (chain ends at
`db/sqlite_conn.py: from pysqlcipher3 import dbapi2`). pysqlcipher3 enters `genome.cli`'s import
closure through **four module-scope sites**:

1. `cli.py:19` — direct `from genome.db.sqlite_conn import sqlcipher_connection` (used by
   `status` / `config get` / `config set`).
2. `cli.py:18` → `db/init_schema.py:19` — module-scope sqlite import (used only inside
   `init_databases`).
3. `cli.py:44` → `privacy/external_client.py:32` — module-scope sqlite import (3 call sites).
4. `cli.py:{15,41,43}` → the heavy sub-apps → **any** `genome.db.*` submodule first runs
   `genome/db/__init__.py`, whose eager re-exports pull pysqlcipher3.

**The co-keystone correction (risk-first, empirically verified):** `db/__init__.py` has **two**
eager paths to pysqlcipher3 — line 3 (`sqlcipher_connection`) *and* line 2 (`init_databases` →
`init_schema.py:19`). So `import genome.db.duckdb_conn` **alone** fails. Both must be neutralized;
the `init_schema` import (Task 1) must move **before** the `__getattr__` keystone (Task 2) can
fully clear the package. `init_schema`'s module-scope `sqlcipher_connection` import is used only
inside `init_databases` (init_schema's *other* module export, `materialize_view`, is imported by
`merge/chrx_qc.py:24` and is pysqlcipher3-clean — so Task 1's land-first ordering also clears the
`cli.py → merge → chrx_qc → init_schema` reach, not just `import genome.db.duckdb_conn`). duckdb
is a clean wheel and is a legitimate residual — only pysqlcipher3 is the blocker.

**Part A:** neither `CLAUDE.md` nor `verification.md` references `genome docs check` / `MEMORY.md`,
so a future session has no entry point to the convention `finding-036` establishes.

---

## 3 · Constraints

- **Locked #1 (two databases) + #6 (SQLCipher on `app.db`) preserved** — pysqlcipher3 is made
  import-**lazy**, never removed. Every `status`/`config`/`init`/external-call path still opens
  the encrypted `app.db`, just resolving the import at call time.
- **No schema/ddl change** (`docs/schemas/`, `ddl/` untouched); no `rm -rf data/` rebuild; no new
  dependency.
- **Conventions:** ruff `--select=ALL` (+ the repo's ignores), `ruff format --check`,
  `mypy --strict backend/src`, structlog/no-`print`. Function-scope imports carry
  `# noqa: PLC0415` + a one-line reason (cli.py:389 precedent).
- **Preserve the public re-export** `from genome.db import sqlcipher_connection` (lazily) — unless
  the §0 #2 fallback is chosen.
- **No-refactor zones:** do **not** touch `sqlite_conn.py`/`duckdb_conn.py`, the DDL-apply
  internals, or `external_client`'s audit/hash logic, and do **not** make `duckdb_conn` lazy
  (it's a clean wheel and a legitimate module-scope import). Move imports only; never re-flow a
  function body.
- **Negative control:** no CLAUDE.md "Real-data observations" number changes; this scope writes
  no DB and runs no pipeline.
- Parts A and B independently shippable (see §0 #1).

---

## 4 · Implementation plan

Riskiest-first ordering (each step independently gated — see §6).

**Task 1 — `init_schema.py`: defer the co-keystone sqlite import.** Delete the module-scope
`from genome.db.sqlite_conn import sqlcipher_connection`; add it (`# noqa: PLC0415`) inside
`init_databases()` before its single `with sqlcipher_connection() as conn:` use. This is the
highest-uncertainty edit (it's why `import genome.db.duckdb_conn` fails today) — land + gate it
**first** (step-gate A).

**Task 2 — `db/__init__.py`: lazy `sqlcipher_connection` re-export** *(if §0 #2 option (a) is
chosen; for option (b), Task 2 instead deletes the re-export and edits the 2 test callers)*.
Keep `duckdb_connection` and `init_databases` as eager top-level imports (both pysqlcipher3-clean
after Task 1). Replace the eager `sqlcipher_connection` import with: `if TYPE_CHECKING: from
genome.db.sqlite_conn import sqlcipher_connection as sqlcipher_connection` (precise type for
static callers) + a module-level `def __getattr__(name: str) -> Any:  # noqa: ANN401` that, for
`name == "sqlcipher_connection"`, does `from genome.db.sqlite_conn import sqlcipher_connection
# noqa: PLC0415` and returns it (else `raise AttributeError`). **Both `# noqa` markers are
mandatory** — `ANN401` (Any in `__getattr__`) and `PLC0415` (function-scope import) are NOT in
the repo ignore list; the pre-mortem + auditor each verified that with exactly these two the file
is ruff `--select=ALL` clean AND `mypy --strict` keeps the precise type. `__all__` unchanged.
Step-gate B.

**Task 3 — `external_client.py`: defer its sqlite import.** Move the module-scope import into the
3 functions that use it (`write_config_change_audit`, `_open_audit_db`, `is_external_enabled`),
each `# noqa: PLC0415`. Step-gate C.

**Task 4 — `cli.py`: defer its direct sqlite import.** Move `cli.py:19` into the 3 callbacks that
use it (`status`, `config get`, `config set`), each `# noqa: PLC0415` (cli.py:389 precedent).
Leave the `add_typer` calls, sub-app imports, `DEFAULT_*` constants, and `get_args(...)` exactly
as-is. Step-gate D (headline).

**Task 5 — regression-lock test** (`backend/tests/test_cli_no_pysqlcipher.py`, new file; reuse the
`_run_probe` clean-subprocess harness from `test_docs_no_db_import.py`). **The `sys.modules[
"pysqlcipher3"] = None` stub MUST be the probe string's first statement, before any `genome.*`
import** — pysqlcipher3 *is* installed in this environment, so a clean subprocess alone would
false-pass; the test must additionally assert the stub took effect (a direct `import
genome.db.sqlite_conn` raises `ModuleNotFoundError`) so a future edit can't silently neuter it.
With the stub first, the probe does `from genome.cli import app` and asserts (a) it imports, (b)
`'genome.db.sqlite_conn' not in sys.modules` while `'genome.db.duckdb_conn' in sys.modules`, (c)
`'docs' in {g.name for g in app.registered_groups if g.name}`; plus a companion (un-stubbed) that
confirms `from genome.db import sqlcipher_connection` still resolves to the real callable (the
lazy re-export contract — option (a) only).

**Task 6 — Part A discoverability docs.** (a) `CLAUDE.md`: one sentence (under "Environment
requirements" + a pointer near "How to run") noting `genome docs check` validates the `MEMORY.md`
ledger + finding frontmatter and runs on a fresh checkout with no DB build (finding-036). (b)
`verification.md`: a short "decision-tracking gate" note that `genome docs check` is DB-free and
must exit 0. Reference `finding-036`; transcribe no anchor digit.

**Task 7 — CHANGELOG `[Unreleased]`** one entry with a PR ref.

---

## 5 · Tests

**New** (`test_cli_no_pysqlcipher.py`): console-script imports without pysqlcipher3 (the headline
regression lock); `genome.db` package imports + `duckdb_connection` accessible with sqlite absent
(proves the keystone); `genome docs --help` via the root app under the stub exits 0; the lazy
re-export still serves `from genome.db import sqlcipher_connection`.

**Must still pass:** both `test_docs_no_db_import` probes (unchanged); `test_docs_cli.py` (incl.
`test_docs_subapp_registered_on_genome_cli`); `test_connections.py` + `test_init_schema.py` (the
2 public-re-export consumers — runtime + mypy-strict); `test_cli_phase4.py` (config get/set open
`app.db`); `test_privacy_external_client.py` (the deferred call sites); the **full 1178-test
suite** (→ 1178 + new); `ruff check` · `ruff format --check` · `mypy --strict backend/src`.

---

## 6 · Verification

- **Step-gates** (each `uv run python -c` with `sys.modules['pysqlcipher3']=None`): **A**
  `import genome.db.init_schema` ok; **B** `import genome.db` + `duckdb_connection` ok and
  `genome.db.sqlite_conn` absent; **C** `import genome.privacy.external_client` ok; **D
  (headline)** `from genome.cli import app` ok, `sqlite_conn` absent, `duckdb_conn` present,
  `'docs'` registered. (Today **D fails** — that's the bug.)
- **Dev-loop:** full `pytest` (count rises by the new tests, no existing test mutated) · `ruff
  check` · `ruff format --check` · `mypy --strict backend/src` (specifically validates the
  `__getattr__` + `TYPE_CHECKING` re-export keeps `from genome.db import sqlcipher_connection`
  precisely typed).
- **Negative control:** `git diff --name-only main..HEAD -- docs/schemas/ ddl/` empty; no
  CLAUDE.md real-data number change; `genome docs check` still exits 0.

---

## 7 · Out of scope

- Any `genome.docs/` body change (already DB-free + wired); any `docs/schemas/`/`ddl/` edit; a
  `rm -rf data/` rebuild.
- Making `duckdb_conn` (or anything but pysqlcipher3) lazy; refactoring `sqlite_conn`/`duckdb_conn`
  or the DDL-apply internals; splitting `cli.py` into per-domain command modules.
- Removing pysqlcipher3 from deps; any SQLCipher build-config / `notes_fts` schema change.
- CI / pre-commit / GitHub-Action enforcement of `genome docs check` (deferred in finding-036).
- Any parent-plan content work (MEMORY.md backfill, frontmatter) — already complete in PR #94.
- **Phase boundary:** Parts A and B are independently shippable (see §0 #1).

---

## 8 · End-of-session handoff

`/handoff` at session end (dogfooding: emit the DEC row for this scope's decision — the
import-decoupling approach). New branch from `main`, clean dev-loop, commit + push, open PR with
the CHANGELOG entry. Add the scope's `DEC` row to `MEMORY.md` and run `genome docs check`.

---

## Appendix · plan-phase provenance

| Stage | Members | Outcome |
|---|---|---|
| 0 · Intake | `scope-dispatcher` | manifest; **tier 2** (S=3, +1 open-questions bump); change_class [docs, cli]; anchors none; 3 OQs; caught the stale `docs/cli.py` "lazy registration" docstring |
| 1 · Planners | `planner` ×2 (minimal-diff, risk-first) | 2 convergent 8-section plans (conf 0.82 / 0.86) |
| 1 · Synthesis | (driver graft) | **risk-first skeleton** (the co-keystone correction + step-gates) + shared OQ resolutions; divergence = co-keystone recognition, test-file placement, the `__getattr__`-vs-drop-re-export fork |
| 1.5 · Pre-mortem | `plan-premortem` (hidden-coupling) | **revise** — empirically reproduced 3 items: Task-2 ruff ANN401+PLC0415 (HIGH), the missed `merge→chrx_qc→init_schema` reach (LOW), the test-stub-must-be-first (MED). Confirmed the keystone is otherwise sound (4-site map exhaustive, no attribute-style access, mypy keeps the precise type). |
| 1 · Audit | `plan-auditor` (contract + architecture-fit) | **revise** — 1 blocker (Task 2 ships ruff-failing code vs the §6 clean-gate claim; two-noqa fix verified) + 1 warn (stub-first) + 2 nits. Confirmed locked #1/#6/#9 respected, negative-control + 1178-baseline hold, both §0 escalations correctly surfaced. |
| Revise cycle 1 | (collapsed) | findings were deterministic + convergent (both members verified the same ruff result) → folded into this plan rather than re-fanning. The two §0 escalations are the only items left for the human gate. |

**Resolution:** all revise findings folded — §0 #2 sharpened (the keystone needs two `# noqa`s and
its mypy benefit is moot for the current test-only callers → audit leans fallback); §2 corrected
(the `merge→chrx_qc→init_schema` reach); §4 Task 2 carries the mandatory `# noqa: ANN401` +
`# noqa: PLC0415`; §4 Task 5 pins the `pysqlcipher3` stub as the first probe statement. Post-fold,
no blocker remains; the plan ends at the human plan-approval gate with the two §0 escalations open.

**Merged riskiest assumption (residual):** no module-scope symbol beyond the audited `with
sqlcipher_connection()` call sites needs the sqlite import at import time (grep-verified across
the 4 sites + the chrx_qc reach, but a hidden default-arg/class-attr use would surface at
mypy/import time — caught by the step-gates A–D and the full dev-loop).
