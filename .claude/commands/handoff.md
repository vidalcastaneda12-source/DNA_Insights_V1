Produce an end-of-session handoff message for the planning chat and for VSC-User.

The handoff is a contract between VSC-Claude (you) and VSC-User. It must be
specific enough that VSC-User can verify the change without re-reading the PR,
and complete enough that the planning chat can decide whether to merge based
on the handoff alone.

## Gather facts from git and gh, not from memory

Run these commands and use the output verbatim. Do not reconstruct any of
this from session memory — values drift between what you intended and what
actually landed.

- Current branch: `git rev-parse --abbrev-ref HEAD`
- Commit SHA(s) on this branch since main: `git log --format='%H %s' main..HEAD`
- Files changed against main: `git diff --name-only main..HEAD`
- For each file, get a one-line description by inspecting the diff
  (`git diff --stat main..HEAD` for line counts, `git diff main..HEAD -- <path>`
  to see what changed in a file). Do not invent descriptions — read the diff.
- PR URL (after `gh pr create`): `gh pr view --json url --jq .url`. If the PR
  has not been opened yet, say so explicitly rather than fabricating a URL.

## Required fields, in this order

1. **Branch name.** From `git rev-parse --abbrev-ref HEAD`.

2. **Commit SHA(s).** Every SHA on the branch since main, with the subject
   line, one per line. From `git log --format='%H %s' main..HEAD`.

3. **Files changed.** The full list from `git diff --name-only main..HEAD`,
   with a brief one-line description of what changed in each file. If the
   list is long, group by directory but do not omit files.

4. **Verification commands for VSC-User.** Default to:

   ```
   ./scripts/verify.sh
   ```

   Add a note that `docs/runbooks/verification.md` lists the underlying
   commands for anyone who prefers running them individually, and that any
   additional schema-rebuild or pipeline-verification steps from the runbook
   apply per the change class.

5. **PR URL.** From `gh pr view --json url --jq .url`. If no PR yet, write
   "No PR opened in this session" — do not fabricate.

6. **Environment notes — conditional.** Check whether the diff touches
   `docs/schemas/` or `ddl/`:

   ```
   git diff --name-only main..HEAD -- docs/schemas/ ddl/
   ```

   - If the command returns any path, the environment notes section MUST
     include the schema-rebuild step explicitly:

     ```
     SCHEMA CHANGED — VSC-User must run:
       rm -rf data/
       uv run genome init
       [re-ingest per docs/runbooks/]
     ```

     Name the specific re-ingest steps that apply per the per-source runbooks
     (`imputation.md` for chip/merge/imputation; `annotations.md` for
     annotation refreshes). Do not just point to the runbook generically.

   - If the command returns nothing, the environment notes section MUST
     explicitly state:

     ```
     None — no schema change, no re-ingest, no DB write.
     ```

     The explicit "none" matters. The absence is intentional, not an omission.

7. **Decision rows (`MEMORY.md`).** Append (or confirm) a `DEC-NNNN` ledger row for
   every decision made *during this session* — a threshold chosen, an approach
   reversed, a deferral. A reversal/supersession is **insert-then-flip** (a new row
   + a back-pointer on the old), never an in-place content edit. Reference real-data
   anchors by pointer (`see CLAUDE.md obs #N`), never copy the digits. If the session
   made no durable decision, write **"None"** explicitly — the absence is intentional,
   not an omission. Confirm `genome docs check` exits 0.

   Likewise, capture any **newly-identified deferred or incomplete work** from this session
   as a `ROADMAP.md` checklist line item with a fresh `RM-<7 hex>` id — `ROADMAP.md` is the
   single source of truth for scope (finding-042 / `DEC-0125`), so a deferral must not live
   only in a finding / PR body / comment. List the new `RM-` ids here, or write **"None"**
   explicitly. Confirm `genome roadmap check` exits 0.

8. **Pre-change pytest baseline / post-change pytest result.** State the test
   count before and after, and call out any tests added, removed, tightened,
   or relaxed with a one-line description of what changed in each. If the
   counts match and no existing tests changed, say so explicitly.

9. **For investigation-only sessions** (no behavior change, only a new
   `docs/findings/` doc), include a two-line conclusion summary at the very
   end suitable for the planning chat to read first. Skip this section for
   sessions that ship code or schema changes.

## What the handoff is NOT

Do not include:

- Implementation rationale or design discussion. Those belong in the PR
  description or in a finding doc.
- Alternative approaches considered and rejected. Same — PR description or
  finding doc.
- A narrative recap of the session ("first I tried X, then Y…"). The
  handoff is a static record of what landed, not a story.
- Self-congratulation or apologies. Neither is contractually relevant.

Keep the handoff terse. One line per fact unless the fact genuinely needs
more. VSC-User reads many handoffs; respect that.
