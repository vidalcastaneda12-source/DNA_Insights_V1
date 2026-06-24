---
type: observation
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-06-11
supersedes: []
superseded_by: []
---
# Finding 024 — `genome status` reports the `.env` external-calls flag, not the gate's live value

## Context

CLAUDE.md locked decision #9 (local-first privacy): external calls require
`external_calls_enabled = true`, and every external call is audit-logged. The
user-facing way to inspect that posture is `genome status`. The flag lives in two
stores:

- `Settings.external_calls_enabled` — a pydantic `BaseSettings` field bound to
  `.env`, read once per process and cached (`config.py:29`; `get_settings()`
  `@lru_cache` at `config.py:41-47`).
- `user_preferences.external_calls_enabled` — a live row in the SQLCipher
  `app.db`, seeded `"false"` at init (`init_schema.py:45`) and mutated by
  `genome config set` (`cli.py:290-347`).

This finding documents that `status` reads the first store while the egress gate
enforces the second, so `status` can misreport the effective gating state. It also
records the framing-check result that separates this from a gating bypass.

## Observation

Confirmed call sites (line numbers as of this finding):

1. **`status` displays the `.env`/Settings value.** `cli.py:146`:
   `typer.echo(f"External calls enabled: {settings.external_calls_enabled}")`.
   `settings` is the `@lru_cache`d `get_settings()` (`cli.py:114`) — a load-time
   snapshot of `.env`, fixed for the process lifetime.

2. **`config set` writes the live store.** `cli.py:290-347` INSERTs/UPDATEs
   `user_preferences` in `app.db` (`cli.py:334`, `:341`) and audit-logs the change
   (`cli.py:346`). It never touches `.env` or Settings.

3. **The egress gate reads the live store.** `_read_external_calls_enabled`
   (`external_client.py:107-119`) runs
   `SELECT pref_value FROM user_preferences WHERE pref_key='external_calls_enabled'`,
   fail-closed `False`. The gate inside `_audited_attempt`
   (`external_client.py:255-276`) calls it and raises `ExternalCallsDisabledError`
   when not enabled.

4. **A live-read helper already exists and is already used in the CLI.**
   `is_external_enabled()` (`external_client.py:484-498`) returns the live
   `user_preferences` value; its docstring already states
   "`Settings.external_calls_enabled` reflects the `.env` value at process startup;
   this function reads `user_preferences` which is the authoritative runtime source."
   The gnomAD loader (`gnomad.py:931`), dbSNP loader (`dbsnp.py:810`), and
   reference-panel download (`cli.py:777`) already early-gate on
   `is_external_enabled()`. `status` is the lone CLI command reading Settings instead.

Framing check (severity-determining) — swept every `external_calls_enabled` usage
and every outbound path under `backend/src/genome/`:

- **Every egress path gates on the live `user_preferences` value.** All HTTP egress
  flows through the single `ExternalClient` choke point, whose `_audited_attempt`
  gate reads `_read_external_calls_enabled` (live). The loaders' early checks read
  live via `is_external_enabled()`. No `httpx`/`requests`/`urllib` call bypasses the
  audited client.
- **Nothing gates egress on `settings.external_calls_enabled`.** The Settings field
  is read in exactly one place: the `status` display at `cli.py:146`. It is not even
  used to seed `user_preferences` — the seed value is hardcoded `"false"`
  (`init_schema.py:45`). The `.env` flag is therefore decorative for gating: it
  changes only what `status` prints, never whether a call proceeds.

## Implication

**This is a display/observability bug, not a gating bypass.** External calls are
always correctly gated on the live `user_preferences` value; only `status` reads the
wrong store. Severity should not be overstated on that basis.

The cost is to trust and observability. Because `user_preferences` seeds `"false"`
and `.env` defaults `False`, the moment a user runs the documented
`genome config set external_calls_enabled true` (the intended Phase-4 step, per the
`config set` docstring) the two stores diverge for the rest of that process:

- **Under-report (privacy-relevant):** `.env=false` (default) + `config set true` →
  `status` prints `External calls enabled: False` while egress is live. A user
  inspecting their privacy posture is told egress is off when it is on.
- **Over-report:** `.env` set `true` (or stale) while `user_preferences` is `false`
  → `status` prints `True` while every call is blocked (fail-closed). Confusing, but
  not unsafe.

The under-report direction is the concerning one for a privacy-first app: a status
command that can claim egress is disabled while it is live undercuts the exact
assurance `status` exists to give. This is the same shape as
[`finding-022`](finding-022-loader-version-label-decoupling.md) (two stores that can
disagree about one fact); here the disagreement is between the displayed flag and the
enforced flag. The decorative `.env` field is itself a smell — see Option C.

## Follow-up

Fix options (recommendation below; disposition deferred to a later PR):

- **Option A (minimal).** `status` reads the live value via the existing
  `is_external_enabled()` helper (`external_client.py:484`) instead of
  `settings.external_calls_enabled`. One line at `cli.py:146`; `status` already opens
  a `sqlcipher_connection()` a few lines up (`cli.py:135`), so the live read is cheap
  and in-pattern. Ends the misreport; makes `status` consistent with the gate and
  with the loaders/reference-panel CLI paths that already call `is_external_enabled()`.
  Con: stops surfacing the `.env` value at all, leaving the (already decorative)
  `.env` knob silently inert.

- **Option B (transparent).** `status` shows both the `.env`/Settings value and the
  live `user_preferences` value, and flags when they diverge. Most informative given
  the two-store reality, and best for debugging "why is egress happening / not
  happening." Con: more code; surfaces the two-store implementation detail in the
  user-facing summary.

- **Option C (unify).** Make `user_preferences` the single source of truth and remove
  `external_calls_enabled` from `Settings`/`.env`. Root-cause fix: the `.env` field is
  decorative for gating today, so removing it eliminates the divergence permanently.
  The sweep confirms the only reader of `settings.external_calls_enabled` is
  `cli.py:146`, so removal is clean. Con: larger change (`config.py`, `.env.example`,
  docs, any `EXTERNAL_CALLS_ENABLED` references); removes a documented `.env` knob, so
  it warrants its own discussion.

**Recommendation:** take **Option A** as the smallest correct change — it ends the
active under-report and reuses `is_external_enabled()` for consistency with the rest
of the CLI. Separately evaluate **Option C** as the root-cause cleanup; if the team
prefers to keep `.env` as a visible knob, **Option B** is the middle ground. A and
B/C should not be bundled together.

Verification when a fix lands: a test asserting `status` and the gate report the same
effective value after a `config set` that changes only `user_preferences` (not
`.env`/Settings); `pytest` / `ruff` / `mypy --strict` clean.
