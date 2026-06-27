---
type: decision
status: active
actors: [VSC-User, ClaudeCodeDevelopment]
date: 2026-06-25
supersedes: []
superseded_by: []
---
# Finding 039 — Scope-split smart-cut detector (Sub Project B2, Phase 1)

## Status

**Active (2026-06-25).** Adopted at Gate 1 for Sub Project B2, Phase 1. This is the durable
provenance anchor for the `genome.scope_split` core, the `genome scope-split` sub-app, the
`/scope-split` skill, the `/scope-run` Stage-0.5 micro-gate hook, the append-only ROADMAP
managed block, and the ledger row `DEC-0094`. The synthesized plan artifact is transient
(plans get pruned — see `DEC-0084`); this finding is where the design rationale lives.

## Related findings

- [`finding-034`](finding-034-agent-team-plan-phase.md) (the per-scope agent team) — B2 plugs a
  pre-plan separability check into that team's `/scope-run` Stage 0 → Stage 1 seam.
- [`finding-038`](finding-038-fast-follow-drain-loop.md) (Sub Project B — the fast-follow drain
  loop) — B2 reuses B's DB-free-core + JSON-seam + independent-vocab + fail-closed-reducer shape
  one level up: B triages a backlog item; B2 decides whether a single scope is one PR or several.
- [`finding-037`](finding-037-agentic-verify-merge-gate.md) (Sub Project A — the verify-merge
  gate) — the same fail-closed reducer discipline (`verify_gate.verdict.reduce_verdict`'s
  UNKNOWN-dominance) is mirrored here with **atomic** as the dominant outcome.

## Context

`/scope-run` runs every ROADMAP scope as a monolith. A scope that is really several
independently-shippable PRs (e.g. a schema slice, a loader slice, a CLI slice) gets planned,
built, and reviewed as one large change, when it could be three small ones. There was no
detector, no pre-plan micro-gate, and no `genome.scope_split` package. B2 Phase 1 adds the
**detector only** — no campaign runner, no auto-running of sub-scopes, no crossing a gate.

The governing risk is **a false split**: proposing to carve a tight, indivisible cluster (PR-3
S=8, PR-5a S=7 are correctly atomic) into pieces that cannot actually ship independently. So the
detector must **detect separability, not size**, and must fail closed — when in doubt, atomic.

## The finding itself

### Manifest-primary cut policy (DECISION 1)

The primary partition signal is the **Stage-0 dispatcher manifest**, not the import graph. The
footprint (`blast_radius.imports_touched`) is grouped by the manifest's `change_class` boundaries
(schema / ddl / annotation-loader / pipeline / cli / tests / docs — separable AND ordered),
refined by `out_of_scope_candidates`. The git-grep coupling graph is a **veto only**: it never
creates the partition, it only *rejects* a proposed manifest partition that is too entangled to
ship as independent pieces. Concretely, the splitter measures the fraction of total (non-infra)
coupling weight the partition would **sever** — `graph.cut_cost(partition)` — and vetoes the cut
→ atomic when that fraction exceeds `MAX_CUT_COST` (the PR-3 / PR-5a tight-cluster rule). Shared
infra helpers (a module imported by ≥ `SHARED_HELPER_FANIN` footprint modules) are dropped from
the veto graph *before* the fraction is measured, so a common dependency does not inflate the
severed-weight fraction.

> **Doc↔code reconciliation (2026-06-26).** Earlier revisions of this section described the veto
> as connected-components *cluster-fusion* (fusing two clusters joined by a high-coupling edge).
> The as-built veto (`splitter._coupling_veto`) is the `cut_cost` **severed-fraction threshold**
> described above — it rejects the whole proposed partition when
> `graph.cut_cost(partition) > MAX_CUT_COST`; it does not fuse clusters or recount components.
> `CouplingGraph.weakly_connected_components` is implemented (pure union-find, GREEN-from-freeze)
> but is **not consumed by the splitter** — it is retained as a graph primitive for a possible
> future fusion-based policy. Wiring it in is a separate, deferred code task (see
> [`sub-project-B2-phase1-deferred-followups.md`](../plans/sub-project-B2-phase1-deferred-followups.md)),
> not a prose change.

> **DECISION 1 — coupling-signal resolution (2026-06-27, Wave 3).** The open choice this decision
> recorded — adopt **git-grep-as-primary**, or build an `LspCallGraphCouplingBuilder` on the ready
> `make_coupling_builder` seam (the B2 spec headlined an LSP call-graph as *primary* with git-grep
> as fallback; Phase 1 shipped git-grep as primary) — is **resolved in favor of git-grep-as-primary**.
> Evidence: the calibration back-test (`backend/tests/test_scope_split_calibration_backtest.py`) runs
> the real git-grep detector against ROADMAP's hand-authored pre-Phase-6 14-PR oracle and reproduces
> it without over- or under-splitting — **over-split = 0** (every oracle-atomic PR returns atomic,
> including the S=8 PR 3 and S=7 PR 5a "big but atomic" traps), the separable annotate+imputation
> mega-scope **splits** schema-first, and git-grep's measured coupling on the one real import edge
> (`strand_collapse → canonicalize.take_snapshot`) **exceeds `MAX_CUT_COST`**, so the veto is a live
> gate, not dead code. The decisive observation is that the partition is **manifest-primary**: the
> atomic decisions come from the `change_class` `MIN_CLUSTERS` signal, not git-grep — PR 3's two
> modules share *no* import edge yet are correctly kept together by their shared change class — and
> git-grep is decisive only at the veto margin, exactly where it correctly fires. An LSP call-graph
> would add no fidelity the oracle reproduction requires, so it stays the deferred-supersession option
> (LIFECYCLE below). The `MAX_CUT_COST=0.25` / `MIN_SUBSCOPE_SHRINK=0.34` dials are therefore
> **validated against the oracle, no retune** (calibration item 2). `DEC-0119` records this; both
> [`sub-project-B2-phase1-deferred-followups.md`](../plans/sub-project-B2-phase1-deferred-followups.md)
> items 1 & 2 are now closed.

### Fail-closed atomic guard (the safety invariant)

`splitter.propose_split` is a flat reducer (mirroring `verify_gate.verdict`) with **atomic** as
the dominant outcome. A non-atomic proposal is returned **only** when a candidate cut survives
every gate, in this REVISED reduction order: re-split cap → extraction guard → primary partition
(< `MIN_CLUSTERS` → atomic) → coupling veto (cut severs > `MAX_CUT_COST` of total non-infra
coupling → atomic) → topo order
(cycle → atomic) → quality gate → build sub-scopes. Any degenerate / undecidable input fails
closed to atomic; an exhaustive property test enumerates the degenerate inputs and asserts none
returns non-atomic.

### Relaxed quality gate (the tier term)

The gate accepts a cut when: every sub-scope shrinks ≥ `MIN_SUBSCOPE_SHRINK` of the parent AND
`max_tier_after <= max_tier_before` AND the split does not duplicate work. The tier ceiling is
the **recomputed parent tier** (`max(declared risk_tier, est_risk_tier(full footprint))`), not
the possibly-stale manifest field — the hard `max_tier_after < max_tier_before` term was removed
because it is structurally unsatisfiable against the dispatcher's max-not-min tier floors (a
schema sub-scope always floors to Tier 2).

### Append-only ROADMAP writer

`roadmap_writer.append_roadmap_block` is a pure string transform: it replaces only the region
between the `<!-- B2-SUBSCOPES:BEGIN -->` / `<!-- B2-SUBSCOPES:END -->` sentinels under the
bootstrapped B2-Phase1 slot, leaving every byte outside the markers identical. It is
byte-idempotent (newline-normalized so a re-run is a no-op regardless of the parent's trailing
newline) and reversible (an empty block returns the region to empty). A ROADMAP missing the
managed slot raises rather than clobbering — the clobber guard.

### DB-free core + JSON seam

The core (`model`, `graph`, `splitter`, `formatter`, `roadmap_writer`, `cli`) imports no
`genome.db`; a package-local clean-subprocess test (`test_scope_split_no_db_import.py`) locks the
boundary, so it runs on a fresh checkout with no DuckDB / SQLCipher built. The manifest crosses
the seam as JSON (`--manifest` accepts a path or `-` for stdin, since `/scope-run` threads the
manifest as in-prompt JSON). The CLI routes structlog to stderr so `check --json` keeps stdout
pure machine output.

### Vocabulary discipline

`CHANGE_CLASS_VOCAB` is owned by `scope_split.model` and reconciled to the **dispatcher C-map**
(`scope-dispatcher.md`), NOT to `verify_gate.model.CHANGE_CLASS_VOCAB` — the splitter partitions
on the same boundaries Stage-0 emits. The dispatcher S-formula (`S = C + B + P`, `tier_from_S`
banding, `max(floor, tier_from_S)` floor) is re-implemented locally so the no-DB guard stays
green; a reconciliation test pins both.

## Provenance — CAPTURE / RETRIEVAL / LIFECYCLE

This finding is the citable knowledge unit the `genome docs check` gate validates across its
three categories:

- **CAPTURE** — this finding is born with the `---`-fenced frontmatter (`type` / `status` /
  `actors` / `date` / `supersedes` / `superseded_by`) the gate requires, and `DEC-0094` is
  appended to the `MEMORY.md` ledger with its `detail-link` pointing back here.
- **RETRIEVAL** — a `plan-premortem` / `regression-hunter` can cite `finding-039` for the
  smart-cut detector's design and its safety invariant; the named constants
  (`MAX_CUT_COST=0.25`, `MIN_SUBSCOPE_SHRINK=0.34`, `MIN_CLUSTERS=2`, `MAX_RESPLIT_DEPTH=1`,
  `SHARED_HELPER_FANIN=3`) are the tunable knobs. The first two are **back-test-validated** against
  ROADMAP's pre-Phase-6 oracle (2026-06-27, `DEC-0119`;
  `backend/tests/test_scope_split_calibration_backtest.py`) — a drift in either is a regression
  signal against that oracle reproduction.
- **LIFECYCLE** — `status: active`; DECISION 1's coupling-signal choice is **resolved to
  git-grep-as-primary** (2026-06-27, `DEC-0119`; see the resolution note above). A future change to
  the cut policy (e.g. swapping in the deferred LSP coupling adapter, recursive re-split, or Phase 2
  `genome.campaign`) is an insert-then-flip supersession, never an in-place edit.

## Consequences / follow-ups

- Phase 2 (`genome.campaign`, auto-running sub-scopes through the per-scope team) is out of scope
  and deferred.
- The placeholder sub-scope ids (`<origin>-s1..sN`) are advisory; minting real PR-N ids is the
  human's call at the micro-gate.
- The riskiest assumption is that flat `imports_touched` + a git-grep scan carry enough
  separability signal; the detector is deliberately atomic-biased (under-proposes) so the failure
  mode is a missed split, never a false one — the safe direction.
