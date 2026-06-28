# Sub Project B2 — Phase 1 deferred follow-ups

Non-blocking backlog from the PR #106 close (2026-06-25). The Phase 1 smart-cut detector +
Stage-0.5 micro-gate shipped clean; these are Stage-3 review residuals captured so they are not
lost. None gates any other work. Durable rationale for the scope lives in
[`finding-039`](../findings/finding-039-scope-split-smart-cut.md).

- [x] **LSP coupling engine (design-fidelity)** — **done (2026-06-27 Wave 3): formally accept
  git-grep-as-primary.** Resolved finding-039 DECISION 1 in favor of git-grep-as-primary, with the
  calibration back-test as evidence (the partition is **manifest-primary**; git-grep is a veto-only
  backstop that the back-test shows fires correctly on real coupling — an LSP call-graph adds no
  fidelity the oracle reproduction requires). The `LspCallGraphCouplingBuilder` is **declined for
  Phase 1** and stays the deferred-supersession option on the ready `make_coupling_builder` seam.
  Recorded as `DEC-0119`; see finding-039 "DECISION 1 — coupling-signal resolution".

- [x] **Calibration back-test** — **done (2026-06-27 Wave 3): dials validated, no retune.**
  `backend/tests/test_scope_split_calibration_backtest.py` runs the real git-grep detector against
  ROADMAP's pre-Phase-6 14-PR oracle reconstructed from real `annotate`/`imputation` modules: it
  reproduces the decomposition without over- or under-splitting (over-split = 0 — the S=8 PR 3 and
  S=7 PR 5a "big but atomic" traps stay atomic; the separable mega-scope splits schema-first; the
  veto fires on the real `strand_collapse → canonicalize` edge above `MAX_CUT_COST`). Verdict:
  `MAX_CUT_COST=0.25` / `MIN_SUBSCOPE_SHRINK=0.34` hold. Reconstruction is a documented loose bound,
  not an exact-match assertion (the detector is change-class-primary + depth-capped, so it coarsens
  the 14-way hand cut by design). Folded into `DEC-0119`.

- [x] **finding-039 doc↔code wording** — **done (2026-06-26 Wave-1 docs sweep): wording
  reconciled.** DECISION 1's veto prose + the reduction-order line now describe the as-built
  `cut_cost` severed-fraction threshold (`graph.cut_cost(partition) > MAX_CUT_COST`), with a dated
  doc↔code reconciliation note recording that `CouplingGraph.weakly_connected_components` is
  implemented but unused by the splitter. The wcc-wiring alternative is **declined** (kept as a
  retained graph primitive); wiring it in remains a separate deferred code task, not this docs
  reconcile.

- [x] **Dead inter-cluster cycle branch** in `splitter._topo_order` — **done (2026-06-26, PR #115):
  removed.** The branch keyed a module→cluster map but probed it with `depends_on` scope-ids, so it
  never fired; confirmed structurally unreachable (`depends_on` carries external scope-ids, and
  `_primary_partition` places each module in exactly one cluster) and removed with a comment
  recording why. The reachable self-cycle guard (`scope_id in depends_on`) stays.

- [x] **Type-design nits** — **done (2026-06-26, PR #115).** Landed `RiskTier = Literal[0, 1, 2]`
  (computed-tier domain), a `CouplingEdge` `NamedTuple` value type, and `TypedDict` shapes for the
  four `to_json()` serializers (`engine` was already the `CouplingEngine` `Literal`). The sealed
  `AtomicResult | SplitProposal` union was **consciously declined** — the existing
  `SplitResult.__post_init__` guard already makes the illegal two-shape states unconstructable, and
  the union would have broken ~9 test constructions + 3 rejection tests against the
  behavior-preserving constraint. `source` / `termination` have no referent in `scope_split`.

- [x] **Test-coverage nits** — **done (2026-06-26, PR #115): all six added.** Shrink-gate
  `<`-strict boundary, a direct `format_roadmap_block` unit, `out_of_scope_candidates` peeling, the
  `--manifest <file-path>` success path, `_grep_count_line` bare/unparsable paths, and
  extraction-guard AND semantics — each in its matching `test_scope_split_*.py`.

- [x] **Phase 2 (genome.campaign orchestrator + multi-session resumability)** — the larger half of
  B2: the persistent campaign state machine that sequences sub-scope `/scope-run`s through the two
  human gates. Deferred pending Sub Project C (its resumability infra is the campaign's first
  consumer); see finding-039 "Consequences / follow-ups".
  **PR 1** shipped the DB-free core + advisory CLI (finding-041 / `DEC-0120`): the persistent state
  machine, supersession ledger, adaptive re-validation, and multi-session resumability, built and
  tested. **PR 2 (live-launch) shipped** — finding-041 / `DEC-0121`: the gate-event-recording
  `revalidate` / `approve-plan` / `record-merge` / `show` commands + the `/campaign-run`
  model-driven conductor.
