# Plan — Build finding-034 Stages 2–5 (Implement · Review · Handoff · Close)

**Scope item:** complete the per-scope agent team designed in
[`finding-034`](../findings/finding-034-agent-team-plan-phase.md). Stage 0–1 (Intake +
Plan) shipped in PR #79; this brief covers **Stages 2–5 + orchestration + authoring
skills**. All work lands on PR #79 (branch `claude/refine-local-plan-gtel6p`).

**Decisions taken (VSC-User, this session):** everything on PR #79 · segmented workflow
scripts (not one unified lifecycle) · build the `/changelog`, `/new-finding`, `/pr-ready`
skills now.

---

## 1 · Reading list confirmation

- `docs/findings/finding-034-agent-team-plan-phase.md` — full (1343 lines): Stages 2–5
  member specs, the correctness-maximized refinement, Build notes, verified diagrams.
- `CLAUDE.md` — locked decisions, conventions, "Things never to do", real-data anchors.
- `.claude/agents/*.md` (the 6 shipped Stage 0–1 members) — authoring style to match.
- `.claude/workflows/plan-phase.js` — the orchestrator pattern the two new scripts mirror.
- `.claude/agents/README.md`, `.claude/commands/handoff.md` — README to update; the one
  existing skill whose shape `/changelog` etc. follow.

## 2 · Problem statement

The agents README marks Stages 2–5 "Designed, not built". The finding fully specs each
remaining member (role, reads, model, tools, Output JSON, prompt checklist, done-when,
hands-to) but no `.claude/agents/*.md` files exist for them, no Stage-2/3/5 orchestration
exists, and Stage 4's `handoff-assembler` depends on `/changelog` + `/new-finding` skills
that don't exist (only `/handoff` does). The team can plan a scope item but cannot
implement, review, hand off, or close one.

## 3 · Constraints (locked decisions respected)

- **finding-034 is the source of truth.** Each member is authored from its exact spec —
  same Output JSON shape, prompt checklist, model, and read/write tools as written.
- **Read vs write (Build notes §"Read vs write"), exactly:**
  - *Writers* (`Edit`/`Write`): `implementer`, `schema-change-executor`,
    `fan-out-implementer` (worktree-isolated), `knowledge-curator` (durable docs,
    post-merge only). `test-author` writes **only `backend/tests/`** and is denied the
    implementation diff as input.
  - *Read-only* (everything else): all monitors (`plan-adherence-sentinel`,
    `test-triage`, `deep-debugger`), all Stage-3 lenses + `finding-verifier` +
    `review-synthesizer` + `completeness-critic`, `handoff-assembler`, `repo-sweep`.
    `green-keeper` is read-mostly (may run `ruff format`, not edit logic).
- **Schema immutability** (hook-enforced): no member or script edits `docs/schemas/` |
  `ddl/`; `schema-change-executor`'s body documents the rebuild protocol and the
  never-remove-`notes_fts` rule, but the deliberate-change override stays a human action.
- **No edits to the shipped Stage 0–1 files** except `README.md` (status flip) — the
  plan-phase agents and `plan-phase.js` are unchanged.
- **Self-contained workflow scripts**: `implement-review.js` and `close.js` each inline
  the `runAgent`/`coerceJson`/`requireKeys`/`progress`/merge helpers (mirroring
  `plan-phase.js`), isolating the one undocumented subagent-invocation primitive behind a
  single `runAgent()` — no cross-file `require` (uncertain in the workflow runtime).
- **Marketplace seeds** (`python-pro`, `serena`, `architect-reviewer`, `hipaa-compliance`,
  …) are referenced as **inline guidance only**, never hard dependencies — matching how
  the shipped 6 agents fold methods in.
- **Conventions:** structlog/no-`print`, type-annotate, ruff/mypy clean apply to *Python*;
  these are `.md`/`.js` artifacts, so no Python is added (dev-loop Python checks are
  unaffected by construction).

## 4 · Implementation plan

**A. Stage 2 — Implement (8 agents → `.claude/agents/`)**
1. `implementer.md` (writer, Opus) — executes approved §4 mechanically; STOP+escalate on
   any surprise; drives blind tests green.
2. `test-author.md` (writer→`backend/tests/` only, Opus, **plan-blind**) — §5 tests from
   plan §5/§6 + frozen interface; independence attestation; `test→spec` provenance stamps.
3. `plan-adherence-sentinel.md` (read-only) — diff-vs-plan drift; PAUSE+escalate.
4. `green-keeper.md` (read-mostly) — pytest·ruff·ruff-format·mypy; escalate vs weaken.
5. `test-triage.md` (read-only) — classify real/flaky/env/needs-update + route.
6. `deep-debugger.md` (read-only proposer, on-demand) — root-cause + minimal fix; never
   weakens a test.
7. `schema-change-executor.md` (writer, rare) — documented rebuild protocol; FTS5 rule.
8. `fan-out-implementer.md` (writer, worktree-isolated) — wide independent mechanical
   breadth only; gated by `blast_radius`.

**B. Stage 3 — Review (12 agents → `.claude/agents/`)**
Lenses (read-only): 9. `convention-compliance` · 10. `phi-pii-guardian` ·
11. `test-integrity` · 12. `regression-hunter` · 13. `silent-failure-hunter` ·
14. `type-design-analyzer` · 15. `pr-test-analyzer` · 16. `comment-analyzer` ·
17. `architect-reviewer`. Then 18. `finding-verifier` (refute-by-default, severity-scaled)
· 19. `review-synthesizer` (pre-gate package + anchors-to-watch) ·
20. `completeness-critic` (loop-until-dry). *(`/code-review` + `/security-review` are
existing skills the orchestrator composes as lenses — not new files.)*

**C. Stage 4 — Handoff (1 agent)** 21. `handoff-assembler.md` (read-only) — wraps
`/handoff` + `/changelog` + `/new-finding`; appends verdict, anchors-to-watch(expected),
residual risk, surviving predicted-surprises; adaptive by tier.

**D. Stage 5 — Close (2 agents)** 22. `knowledge-curator.md` (durable-doc **writer**,
post-merge, human-confirmed numbers only, reviewable change) — re-lock anchors, flip
ROADMAP, cross-link. 23. `repo-sweep.md` (read-only detector) — whole-repo staleness →
backlog; the same detector as the dispatcher freshness slice.

**E. Authoring skills (3 → `.claude/commands/`)** `changelog.md`, `new-finding.md`,
`pr-ready.md` — frontmatter + body matching `handoff.md`'s shape; encode the CLAUDE.md
CHANGELOG convention, the findings-doc/bedrock-anchor convention, and a pre-PR readiness
checklist respectively.

**F. Orchestration (2 → `.claude/workflows/`)**
- `implement-review.js` — post-Gate-1 segment: Stage 2 (interface-freeze → test-author ∥
  implementer → green loop watched by sentinel; tier-gated side-channels) → Stage 3
  (parallel lenses gated by `manifest.review_lenses` → finding-verifier →
  completeness-critic loop-until-dry → review-synthesizer), bounded fix-first loop ×2 →
  escalate. Emits the pre-gate package for Gate 2. Args: `scope_id` + approved-plan +
  manifest + `predicted_surprises`.
- `close.js` — post-Gate-2 segment: `knowledge-curator` (re-lock confirmed numbers) +
  `repo-sweep` (backlog). Args: `scope_id` + confirmed gate anchors.

**G. Docs** — update `.claude/agents/README.md` (Status table → Stages 2–5 **Built**;
expand Members/Usage; resolve the skills reference), add `CHANGELOG.md` `[Unreleased]`
entry, and add a one-line "build landed in PR #79" note to finding-034's status.

*Authoring approach:* members are derived directly from their finding sections (mechanical
transcription of an already-complete spec). Independent batches may be fanned out to
sub-agents for speed, but every file is reviewed for consistency against the shipped 6.

## 5 · Tests / validation

No Python is added, so there are no new pytest/ruff/mypy targets. Validation is
structural and per-artifact:
- **Frontmatter check** — every new `.claude/agents/*.md` and `.claude/commands/*.md` has
  parseable YAML frontmatter with `name`, `description`, `tools`, `model` (a node/python
  one-liner over the files; not a committed test, run in the dev loop).
- **Tool-grant check** — writers carry `Edit`/`Write`; all read-only members carry only
  `Read, Grep, Glob, Bash`; `test-author`'s body confines writes to `backend/tests/`.
- **Workflow scripts** — `node --check` clean; CommonJS-require isolation (no auto-run
  under a test loader); loud-failure path (missing primitive → actionable error, no
  `ReferenceError`); `args`/`process.argv[2]` entry parsing.
- **Existing suite** — `ruff check` + `ruff format --check` confirmed clean (trivially,
  no `.py` change); pytest baseline unaffected (stated, not re-run against real data).

## 6 · Verification (how to confirm success)

- 23 new agent files + 3 skill files exist, frontmatter valid, each carrying the spec'd
  Output-JSON block + prompt checklist + done-when/hands-to from its finding section.
- Read/write tool grants match Build notes §"Read vs write" exactly (writers vs read-only
  audited file-by-file).
- `implement-review.js` and `close.js`: `node --check` clean, require-isolated, fail loud
  on the undocumented primitive (same isolation as `plan-phase.js`).
- `README.md` Status table shows Stages 2–5 Built; no dangling skill references; finding
  status notes the PR-#79 build.
- `CHANGELOG.md` `[Unreleased]` carries the entry.
- Staged set contains only `.claude/`, `CHANGELOG.md`, `docs/`, `docs/plans/` — no
  `data/`, `archive/`, or raw exports (privacy decision #9; `git add -A` hook-blocked, so
  staging is by explicit path).

## 7 · Out of scope (explicit)

- **Candidate cross-examination** (finding defers it — escalation-only, overlaps pre-mortem).
- **One unified `pr-lifecycle.js`** (rejected for the segmented scripts) and any
  pause-for-human runtime mechanism — the two human gates are honored by segmentation.
- **Installing/relying on marketplace MCP agents** (serena/greptile/context7) — referenced
  as optional inline seeds only.
- **End-to-end execution of the workflow JS** — the dynamic-workflows subagent primitive
  is undocumented (same caveat as `plan-phase.js`); logic is `node --check`-clean and the
  one runtime call is isolated for a one-line adjustment.
- **New enforcement hooks** — the shipped 4 (schema-edit, git-add-all, gate-fill,
  changelog) suffice; the sentinel covers the judgment-call layer in-prompt.
- **Changes to shipped Stage 0–1 members** beyond the README status flip.

## 8 · End-of-session handoff

`/handoff` at session end; PR #79 updated with the Stage 2–5 commits; this brief committed
under `docs/plans/` as the implementation record.
