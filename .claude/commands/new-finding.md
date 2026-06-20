Create a new `docs/findings/finding-NNN-<slug>.md` capturing a durable discovery from this
session — a real-data observation, a locked decision, a surprise + its mechanism, or a
re-locked anchor set.

A finding is the repo's unit of durable, citable knowledge. The pipeline's correctness is
anchored to findings (a `regression-hunter` cites them; a `plan-premortem` consults them;
`knowledge-curator` re-locks numbers into their bedrock anchor tables). Write it so a
future session can rely on it without re-deriving it.

## Pick the number and slug

- Next number: list `docs/findings/`, take the highest `finding-NNN` and add one. Numbers
  are zero-padded to three digits (`finding-036`). Do not reuse or skip numbers.
- Slug: short, hyphenated, descriptive (`finding-036-<slug>.md`), matching the existing
  naming style.

## Structure (match the existing findings)

1. **Title** — `# Finding NNN — <human title>`.
2. **Status** — what this is (observation / decision / surprise / re-lock) and whether it's
   design-only, built, or superseded; the date and the actors involved.
3. **Context** — the problem or question that produced the finding; cite the ROADMAP slot /
   PR / sibling findings it relates to.
4. **The finding itself** — the specific, durable content. For a real-data observation,
   give the **exact numbers with their source command + corpus + source versions** (the
   "stable identifiers" pattern: a number that, if it drifts on a re-run against the same
   input, is a regression signal). For a decision, state it and its rationale and what it
   locks. For a surprise, name the **mechanism** and the **evidence**.
5. **Bedrock anchor table** (when the finding locks real-data numbers) — a table of
   `anchor | value | source line`, the canonical set `knowledge-curator` re-locks and
   `regression-hunter` watches. Frame any corrected number explicitly as
   correction-not-regression where that applies.
6. **Consequences / follow-ups** — what re-locks downstream, what must be re-run after a
   given signal fires, what's deferred.

## Rules

- **Numbers come from a real run**, captured verbatim from the command output — never from
  memory or estimate. If a number isn't confirmed yet, mark it `GATE-FILL` (the gate-fill
  hook will warn until it's replaced) rather than guessing.
- Cross-link related findings with `[[finding-0NN]]` / a relative path, and add the reverse
  link where it belongs.
- If this finding re-locks an anchor that appears in `CLAUDE.md` / `verification.md`, note
  that those must be updated too (that is `knowledge-curator`'s job at Stage 5) — a number
  re-locked in one place but not another is cross-doc drift.

## Done when

The finding exists at `docs/findings/finding-NNN-<slug>.md` with all sections, numbers
sourced from a real run (or explicitly `GATE-FILL`), and cross-links in both directions.
