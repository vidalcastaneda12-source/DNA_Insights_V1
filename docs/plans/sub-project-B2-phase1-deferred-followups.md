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

- [ ] **finding-039 doc↔code wording** — the finding describes the veto as connected-components
  cluster-fusion; as-built it is a `cut_cost` severed-fraction threshold (and
  `CouplingGraph.weakly_connected_components` is unused by the splitter). Reconcile the wording or
  wire wcc in.

- [ ] **Dead inter-cluster cycle branch** in `splitter._topo_order` — keyed by module names but
  probed with `depends_on` scope-ids, so it never fires (harmless — the schema-first sort is
  acyclic). Remove or re-key.

- [ ] **Type-design nits** (deferred, same class as the fast-follow PR) — `Literal`/enum for
  `tier` / `source` / `termination` / engine; a `TriageCounts`-style `TypedDict`; a sealed
  `AtomicResult | SplitProposal` union for `SplitResult`; a `CouplingEdge` value type.

- [ ] **Test-coverage nits** — shrink-gate boundary (`achieved_shrink == MIN_SUBSCOPE_SHRINK`),
  `format_roadmap_block` direct unit test, `out_of_scope_candidates` peeling, `--manifest <file-path>`
  success path, `_grep_count_line` bare/unparsable paths, extraction-guard AND semantics.

- [ ] **Phase 2 (genome.campaign orchestrator + multi-session resumability)** — the larger half of
  B2: the persistent campaign state machine that sequences sub-scope `/scope-run`s through the two
  human gates. Deferred pending Sub Project C (its resumability infra is the campaign's first
  consumer); see finding-039 "Consequences / follow-ups".
