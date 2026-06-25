Run the fast-follow drain loop (Sub Project B, `finding-038`) over the residual backlog:
scan the backlog into candidates, triage each into a fail-closed DRAIN / EJECT / DISCARD
plan, take a triage approval, then drain the DRAINs one batch at a time — each batch
merged only through Sub Project A's `/verify-and-merge` gate. Argument: an optional scan
scope hint (e.g. a path or `repo-sweep`); defaults to a full residual-backlog scan.

This is the consumer for repo-sweep's backlog: before Sub Project B, repo-sweep was a backlog
*producer* with no consumer and the drain was a manual ROADMAP step (e.g. PR 8). This skill
replaces that with the bounded, fail-closed triage loop around the unit-tested
`genome.fast_follow` core. It never merges
on its own and never writes ROADMAP — drains go through A's gate, ejects are drafted for a
human to paste into `/scope-run`.

## Two invariants (read first)

1. **Never drain without the triage-approval touchpoint.** The loop reaches a drain only
   after the operator approves the presented triage plan (touchpoint 1). No approval, no
   drain — full stop.
2. **Every drained batch merges only through A's `/verify-and-merge`.** This skill never
   squash-merges. Each DRAIN batch is handed to Sub Project A's evidence-gated gate
   (touchpoint 2), which is the per-batch backstop with its own typed `merge` token.

## Steps

1. **Scan.** Reuse `repo-sweep` to enumerate the residual backlog (it emits
   `{kind, location, evidence, confidence, fix_effort, suggested_action}`). This is the raw
   candidate list — not yet the classifier's input.
2. **Model-driven triage step (the repo-sweep → Candidate bridge, ESC-2 / R3).** For **each**
   candidate, **READ the files it would touch** and DERIVE the classifier attributes:
   `change_class` (the `core | schema | pipeline | annotation` label set), `applicable_anchors`
   (count of real-data anchors the change would move), `blast_radius` (file count), `tier`
   (`tier-0` / `tier-1`), and `touched_paths` (the **literal** files, verbatim from the read —
   the classifier's independent path guard keys on this list, A2, so a schema item you mislabel
   `core` still EJECTs on its `docs/schemas/**` / `ddl/**` path). **Fail closed:** when a read
   or the prose is unclear, emit `None` for the undecidable field — the classifier EJECTs it.
   Write the derived candidates to `candidates.json` (the canonical JSON seam; agents emit JSON
   natively, encoding the list/set fields losslessly). `genome fast-follow scan-assemble` is a
   flat-token convenience for the same file; the JSON is authoritative.
3. **Triage.** Run `genome fast-follow triage --candidates candidates.json --dry-run` to print
   the fail-closed plan (per-item verdict + reason, the drain / eject / discard counts, the
   overflow partition, the termination summary). The classifier reduction order is:
   extraction-fail-closed → literal `touched_paths` guard on `docs/schemas/**` / `ddl/**` →
   `is_stale` DISCARD → guarded-class / `applicable_anchors != 0` / over-cap `blast_radius`
   EJECT → else DRAIN. DRAIN is reachable only past every guard.
4. **🚦 Touchpoint 1 — triage approval.** Present the triage plan. The operator approves the
   DRAIN set before anything is drained. No approval → stop. If the plan is empty or every
   candidate EJECTed / DISCARDed, the formatter emits the "nothing drainable" sentinel — this
   is a **no-op**, never an empty PR.
5. **Batch + implement.** Group the approved DRAINs (`group_drains`) and, per batch, reuse the
   `implementer` + `green-keeper` stages to make the change. Each change **records the drained
   backlog item** it resolves (the `Triage.drains` provenance, decision #8).
6. **🚦 Touchpoint 2 — A's gate, per batch.** Hand each drained batch to Sub Project A's
   `/verify-and-merge`. That gate runs the full verification protocol, presents raw evidence,
   takes its own typed `merge` token, and squash-merges. This skill never merges directly.
7. **Loop until dry or cap.** After each batch, persist the handled keys to the seen-set at
   `data/fast_follow/seen.json` (every drained / ejected / discarded candidate), so the next
   scan excludes them — the self-spawning-nit termination guard. The loop ends `dry` when no
   drainable candidate remains, or `cap` at `MAX_BATCHES` (the bounded-loop guarantee).

## Eject and discard handling

- **Eject** = `genome fast-follow eject-draft --candidates candidates.json`. This prints a
  ROADMAP-style draft block per ejected candidate (recording the source candidate) to stdout.
  The **human pastes it into ROADMAP / `/scope-run`** — this skill **never** writes ROADMAP
  autonomously (OQ-3).
- **Discard** = a stale / already-handled candidate; logged, not actioned.
- **Empty / eject-only** = a no-op (the "nothing drainable" sentinel); never open an empty PR.

## Temporal note

This skill governs **future** residual backlogs. It is offered (offer only) from the close
step of `/verify-and-merge` — never acting without the triage-approval touchpoint.
