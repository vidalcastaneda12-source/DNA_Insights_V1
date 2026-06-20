Add or update a CHANGELOG.md `[Unreleased]` entry for the change on this branch.

CLAUDE.md requires: *"Every PR that changes behavior, schema, dependencies, or build
steps should add an entry to `CHANGELOG.md` under the `[Unreleased]` section. The entry
should be one or two sentences describing what changed and why, with a PR reference."*
This skill produces that entry — accurate to what actually landed, not to intent.

## When an entry is required

Add an entry when the branch changes any of: **behavior**, **schema/DDL**,
**dependencies**, or **build/CI steps**. Pure-internal refactors with no observable change
may be skipped — but when in doubt, add one. Dev-infrastructure changes (agents, hooks,
skills, workflows) count as build/process changes and get an entry.

## Gather facts from the diff, not from memory

- Files changed vs main: `git diff --name-only main..HEAD`.
- What actually changed in each: read the diff (`git diff main..HEAD -- <path>`). Do not
  reconstruct the description from session memory — describe what the diff shows.
- The PR number: from the open PR (`gh pr view --json number --jq .number`) or the PR the
  branch targets. If no PR is open yet, use the branch name and note the PR ref is pending.

## Write the entry

1. Open `CHANGELOG.md`. Confirm an `## [Unreleased]` section exists near the top (create
   it directly under the format/preamble lines if missing — never below a versioned
   release section).
2. Add a bullet at the **top** of `[Unreleased]` (newest first), matching the existing
   bullets' style: one or two sentences, **what changed and why**, ending with the PR
   reference `(#NN)` (or `(PR #NN)` if that's the file's convention — match what's there).
3. For a schema change, say so explicitly and note the rebuild implication
   (`rm -rf data/ && uv run genome init`) so a reader knows pulling it requires a rebuild.
4. For an anchor-moving change, name the anchor(s) and the expected direction, so the entry
   ties to the verification gate.
5. Keep the bullet self-contained — a reader scanning `[Unreleased]` should understand the
   change without opening the PR.

## Done when

The `[Unreleased]` section carries an accurate, diff-grounded, one-to-two-sentence bullet
with a PR reference, in the file's existing style, and it is the only change you made to
`CHANGELOG.md` (do not roll up `[Unreleased]` into a release section — that happens at a
phase milestone, not here).
