Run the smart-cut split check (Sub Project B2 Phase 1, `finding-039`) over one Stage-0
dispatcher scope manifest: read the manifest, propose whether the scope is **separable** into
independently-shippable sub-scopes or is one indivisible unit (atomic), and present the result
as a pre-plan advisory. Argument: a scope id (e.g. `PR-6`) whose dispatcher manifest you have,
or a path / `-` for a manifest fed on stdin.

This is the detector for the per-scope agent team (`finding-034`): before planning a scope as a
monolith, the model-driven split check asks "is this really one PR, or several ordered PRs?".
It is **advisory only** — it never auto-runs a sub-scope, never writes ROADMAP without the
`write-roadmap` subcommand, and never crosses a gate. The whole decidable reduction lives in the
DB-free, unit-tested `genome.scope_split` core; the skill is faithful plumbing with one human
touchpoint (the pre-plan micro-gate).

## Two invariants (read first)

1. **Atomic is the fail-closed default.** The detector proposes a split **only** when a candidate
   cut survives every gate (primary partition ≥ 2 clusters, coupling veto does not fuse them
   below the minimum, the topo order is acyclic, the quality gate passes, the re-split cap is not
   hit). Any degenerate or undecidable input → atomic. A false split is the costliest mode, so
   the detector under-proposes by construction.
2. **Never auto-act on a split.** The split check presents a proposal; the human approves, edits,
   or runs-as-one at the pre-plan micro-gate. No approval, no carve. `write-roadmap` is the only
   command that writes anything, and it touches only the managed inter-sentinel region.

## Steps

1. **Obtain the manifest.** Take the Stage-0 `scope-dispatcher` manifest JSON for the scope
   (`scope_id`, `change_class`, `blast_radius.imports_touched`, `out_of_scope_candidates`,
   `applicable_anchors`, `depends_on`, `risk_tier`, `risk_breakdown`). It is threaded as
   in-prompt JSON — feed it on stdin with `--manifest -`.
2. **Propose.** Run `genome scope-split check --manifest - --json` (feeding the manifest on
   stdin). The fail-closed reducer returns either `{"atomic": true, "reason": ...}` or a full
   split proposal (`sub_scopes`, `order`, `cut_quality`). Use `dry-run` for the human-readable
   block plus the literal `would create N sub-scopes` affordance — `dry-run` creates nothing,
   writes no ROADMAP, and runs no scope-run.
3. **Micro-gate (the one touchpoint).** Present the proposal:
   - **Atomic** → report `atomic — no split` with the reason; the scope proceeds to planning
     unchanged.
   - **Split** → present the ordered sub-scopes (each with its `origin_scope`, change classes,
     estimated footprint, re-scored tier, and the cut-quality summary) and ask the human to
     **approve / edit / run-as-one**. Stop for the human; do not proceed on your own.
4. **Record (only on approval).** If the human approves the split, `genome scope-split
   write-roadmap --manifest -` splices the proposed sub-scope slots into the managed ROADMAP
   block (append-only, byte-idempotent). Atomic → it echoes the sentinel and writes nothing.

## The cut policy

The primary partition signal is the **manifest** (group the footprint by `change_class` boundary
refined by `out_of_scope_candidates`), with the git-grep import graph as a **veto** only — a cut
that would sever two heavily-coupled modules is vetoed (the clusters are fused). Shared infra
helpers are dropped from the veto graph so a common dependency does not fuse independent clusters.
The placeholder sub-scope ids (`<origin>-s1..sN`) are advisory; minting real PR ids is the
human's call.

## Temporal note

This skill governs the pre-plan separability check for **future** scopes. It is the consumer of
the Stage-0 manifest and the producer of the Stage-0.5 micro-gate in `/scope-run`; it never acts
without the pre-plan approval touchpoint.
