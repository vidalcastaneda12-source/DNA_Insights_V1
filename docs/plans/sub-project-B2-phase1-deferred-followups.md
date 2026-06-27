# Sub Project B2 — Phase 1 deferred follow-ups

Non-blocking backlog from the PR #106 close (2026-06-25). The Phase 1 smart-cut detector +
Stage-0.5 micro-gate shipped clean; these are Stage-3 review residuals captured so they are not
lost. None gates any other work. Durable rationale for the scope lives in
[`finding-039`](../findings/finding-039-scope-split-smart-cut.md).

- [ ] **LSP coupling engine (design-fidelity)** — the spec headlined an LSP call-graph as the
  primary coupling signal with git-grep as fallback; Phase 1 ships git-grep as primary (the veto is
  correct *as a git-grep veto*, but lower-fidelity than "smart-cut" implies). Decide whether to add
  an `LspCallGraphCouplingBuilder` (the `make_coupling_builder` Protocol seam is ready) or formally
  accept git-grep-as-primary in finding-039's DECISION 1.

- [ ] **Calibration back-test** — `MAX_CUT_COST=0.25` / `MIN_SUBSCOPE_SHRINK=0.34` are unvalidated
  dials; back-test against ROADMAP's 13-PR pre-Phase-6 sequence (the hand-authored decomposition the
  detector aims to reproduce) to confirm they neither over- nor under-split.

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

- [ ] **Phase 2 (genome.campaign orchestrator + multi-session resumability)** — the larger half of
  B2: the persistent campaign state machine that sequences sub-scope `/scope-run`s through the two
  human gates. Deferred pending Sub Project C (its resumability infra is the campaign's first
  consumer); see finding-039 "Consequences / follow-ups".
