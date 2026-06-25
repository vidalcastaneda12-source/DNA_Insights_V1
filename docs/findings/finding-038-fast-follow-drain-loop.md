---
type: decision
status: active
actors: [VSC-User, ClaudeCodeDevelopment]
date: 2026-06-25
supersedes: []
superseded_by: []
---
# Fast-follow drain loop — bounded fail-closed backlog triage (Sub Project B)

## Status

**Active (2026-06-25).** Adopted at Gate 1 for Sub Project B. This is the durable
provenance anchor for the `genome.fast_follow` core, the `/fast-follow` skill, the
`/verify-and-merge` close-step auto-offer hook, and the three ledger rows
`DEC-0091 … DEC-0093`. The synthesized plan artifact is transient (plans get pruned —
see `DEC-0084`); this finding is where the design rationale lives.

## Related findings

- [`finding-037`](finding-037-agentic-verify-merge-gate.md) (Sub Project A — the agentic
  `/verify-and-merge` gate). B reuses A's gate as its touchpoint-2 per-batch backstop, and
  B's auto-offer hook extends A's `/verify-and-merge` close step. A is a hard dependency.
- [`finding-034`](finding-034-agent-team-plan-phase.md) (the per-scope agent team) — the team
  that planned, built, and reviewed Sub Project B.

## What changed

`repo-sweep` is a backlog **producer** with no consumer: the drain of its Tier-0 /
bounded-Tier-1 items was a manual ROADMAP step. Sub Project B adds the bounded, fail-closed
triage loop that gives that backlog a consumer. The loop DRAINs the small, anchor-free,
schema-untouching candidates through Sub Project A's `/verify-and-merge` gate, and EJECTs
schema / pipeline / annotation / anchor-exposed candidates back to `/scope-run`. The whole
decidable reduction lives in a DB-free, unit-tested core; the skill is faithful plumbing with
two mandatory human touchpoints.

The safety invariant is the governing rule: **no candidate carrying a guarded class, a
non-empty anchor set, an over-cap blast_radius, or a touched path under `docs/schemas/**` or
`ddl/**` is EVER classified DRAIN.** Everything undecidable fails closed to EJECT.

## The drain-vs-eject-vs-discard classifier (fail-closed)

`genome.fast_follow.classifier.classify` reduces one `Candidate` to one `Triage` in a strict,
fail-closed reduction order — DRAIN is reachable only past **every** guard:

1. **Extraction fail-closed.** Any decision-bearing field undecidable — an empty
   `change_class`, or a `None` `blast_radius` / `applicable_anchors` / `tier` — → **EJECT**.
   The model-driven derivation (below) emits `None` whenever a file read is unclear, so an
   unreadable candidate routes to EJECT, never to a guessed DRAIN.
2. **The independent `touched_paths` guard.** Any literal path under `docs/schemas/**` or
   `ddl/**` → **EJECT**, keyed on the literal read-from-disk path list, **not** on the
   derived `change_class` label (see next section).
3. **Stale / already-handled** (`is_stale`) → **DISCARD** (logged, not actioned).
4. **Guarded class** (`change_class ∩ {schema, pipeline, annotation}`) **OR** anchor-exposed
   (`applicable_anchors != 0`) **OR** over-cap (`blast_radius > MAX_DRAIN_FILES`) → **EJECT**.
5. else Tier-0 / bounded-Tier-1 → **DRAIN**, recording in `Triage.drains` which backlog item
   it drains (provenance, decision #8).

The exhaustive property the test-author enumerates (not samples): no candidate satisfying any
guard condition ever returns DRAIN. The single-attribute-flip negative-control sweep is the
companion — flipping exactly one guarded attribute on an otherwise-drainable candidate must
flip the verdict away from DRAIN.

## The independent `touched_paths` guard

The classifier's path guard (step 2) keys on the candidate's **literal `touched_paths`** —
the verbatim file list the model-driven triage step read from disk — and never on the derived
`change_class` label. This is deliberate: the derivation is a trusted, unverifiable input
(the core cannot itself re-open the repo), so a candidate the skill mislabels `core` while it
in fact edits `docs/schemas/**` or `ddl/**` must still EJECT. The literal-path guard is the
backstop that makes a mis-derivation safe — it catches a schema/DDL touch regardless of how
the class was labeled. The schema/DDL roots it guards mirror the immutable set in CLAUDE.md
"Things never to do".

## The safety composition (triage guards + A's gate backstop + two touchpoints)

No single mechanism carries the safety guarantee; the composition does:

- **The triage guards** (the five-step fail-closed reduction) keep guarded / anchor-exposed /
  over-cap / schema-touching candidates out of the DRAIN lane in the first place.
- **Touchpoint 1 (triage approval).** The loop reaches a drain only after the operator
  approves the presented triage plan — no approval, no drain.
- **Touchpoint 2 (A's gate per batch).** Each drained batch is merged only through Sub Project
  A's `/verify-and-merge`, which re-runs the full verification protocol, presents raw
  evidence, and takes its own typed `merge` token. This skill never merges directly. A's
  fail-closed core is the per-batch backstop — even a mis-triaged DRAIN cannot merge without
  clearing the gate.

## The DB-free core + JSON seam

The core (`model`, `classifier`, `loop`, `persistence`, `formatter`, `cli`) **imports no
`genome.db`** — a package-local clean-subprocess test locks the boundary, exactly as Sub
Project A's verify-gate does — so it runs on a fresh checkout with no DuckDB / SQLCipher
built. The reduction is split deterministically: `classifier.py` is the pure
`Candidate → Triage` reducer (the exhaustive property test targets this one function);
`loop.py` owns the batcher, the cross-invocation seen-set dedup, the `MAX_ITEMS` / `MAX_BATCHES`
caps with explicit overflow (no silent truncation), and the `dry` / `cap` termination
predicate; `persistence.py` owns the seen-set filesystem I/O so `model.py` stays I/O-free.

The serialization seam is a JSON file (`candidates.json`), not the flat scalar-token idiom:
the scalar `k=v,...` token splits on `,`, which cannot losslessly encode the `touched_paths`
list or the `change_class` set without the value-separator colliding with the field-comma —
and a mis-parsed `touched_paths` is exactly a false-DRAIN path. The agent emits JSON natively;
`scan-assemble` is kept as a convenience that uses `|` as the intra-field sub-delimiter for the
two collection fields, with a malformed token raising a clean non-zero `BadParameter` rather
than a silent coerce.

The seen-set persists at `data/fast_follow/seen.json` (the gitignored runtime-state home),
keyed on a pure stable `seen_key` (`source:candidate_id`). Each `/fast-follow` run is a
separate post-merge process, so without cross-invocation persistence every handled item would
re-surface — the persisted seen-set is the self-spawning-nit termination guard alongside the
batch cap.

## The model-driven derivation bridge (ESC-2)

The `repo-sweep → Candidate` bridge is a **model-driven triage step**, not a Python adapter.
`repo-sweep` emits only `{kind, location, evidence, confidence, fix_effort, suggested_action}`
— not the classifier's attributes. The skill's triage step is where the agent reads each
candidate's touched files and derives `change_class` / `applicable_anchors` / `blast_radius` /
`tier` / `touched_paths`, then writes `candidates.json`. When a read or the prose is unclear,
it emits `None`, and the classifier EJECTs. This is the rank-1 riskiest assumption — the
derived attributes are trusted inputs the pure core cannot verify — and the mitigation is the
composition above: the independent literal-path guard, the fail-closed extraction bias, A's
gate backstop, and touchpoint 1. Consequently the `--dry-run` smoke tests the **classifier**
on pre-structured input by design; the derivation itself is a model-driven property, not a
unit test.

## Vocabulary discipline (the independent guard vocab)

`GUARD_CLASS_VOCAB` is owned independently by `fast_follow.model`, **not** imported from
`verify_gate.model.CHANGE_CLASS_VOCAB`. The two consumers use the same four labels with
**opposite polarity** — verify_gate uses them as a positive check-set selector; fast_follow
uses them as a guard that routes to EJECT — so a raw shared frozenset would be
action-at-a-distance on a safety path: a future verify_gate vocab edit would silently
re-route B's classifier. A reconciliation test (`GUARD_CLASS_VOCAB ⊆ CHANGE_CLASS_VOCAB`, or
an explicit documented diff) keeps the single-source-of-truth benefit — drift fails a test,
not silently — without the coupling. `TIER_VOCAB` (`tier-0` / `tier-1`) is deliberately named
to not collide with the clinical `1A | 1B | 2A | 2B | 3 | 4` evidence-tier scale; these are
loop-internal drain priorities, not evidence grades.

## The auto-offer hook

The close step of `/verify-and-merge` (step 9) gains a distinct auto-offer line: after close,
auto-scan the residual backlog and **offer a `/fast-follow` drain-loop scan** of it. The
wording is deliberately distinct from the pre-existing "fast-follow" sense in that step (the
knowledge-curator doc re-lock). The offer is **offer-only** — it never drains, never merges,
and never acts without `/fast-follow`'s own triage-approval touchpoint — so B's own self-merge
triggering it is harmless.

## Provenance

Ledger rows `DEC-0091` (DB-free fail-closed classifier + the no-false-DRAIN exhaustive-property
invariant), `DEC-0092` (the independent `GUARD_CLASS_VOCAB` + reconciliation test + literal-path
guard + fail-closed extraction bias, noting the opposite polarity vs verify_gate's
`CHANGE_CLASS_VOCAB`), and `DEC-0093` (two-touchpoint / drafts-and-human-approves eject +
persisted seen-set) all point at this finding. The eject writer drafts to stdout and the human
pastes into ROADMAP — never an autonomous ROADMAP write.

## OQ + ESC resolutions

- **OQ-1** → ship the `genome fast-follow` typer sub-app.
- **OQ-2** → pytest `--dry-run` via a seeded `candidates.json`.
- **OQ-3** → the eject writer drafts to stdout; the human approves and pastes (never an
  autonomous ROADMAP write).
- **ESC-1** → per-item blast_radius DRAIN cap = `MAX_DRAIN_FILES` (a tunable module constant);
  a candidate over the cap EJECTs.
- **ESC-2** → the `repo-sweep → Candidate` bridge is the model-driven triage step (above), not
  a repo-sweep scope change.
- **ESC-3** → the seen-set persists at `data/fast_follow/seen.json` (the gitignored
  runtime-state home; `archive/` is snapshot territory).
