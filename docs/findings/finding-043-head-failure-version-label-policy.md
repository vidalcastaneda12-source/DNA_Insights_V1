---
type: decision
status: active
actors: [VSC-User, ClaudeCodeDevelopment]
date: 2026-07-01
supersedes: []
superseded_by: []
---
# Finding 043 — HEAD-failure refuse + bytes-bound version label (version-label correctness policy)

PR 10 (`RM-9f3c52c`, ROADMAP pre-Phase-6 sequence) closes two **atomic label-integrity
defects** in the ClinVar / GWAS Catalog loaders. Both are fault-triggered and invisible on a
normal run, both were already *described* but never *fixed*, and both violate locked decision
#7's premise that the `annotation_source_versions.version` string must identify the bytes in
the table (the pointer flip is the supersession event — never mint a spurious flip) and #8's
provenance-traces-to-an-accurate-label. The two defects are independent, so the fix is a
narrow spine touching three files (`annotate/downloads.py`, `annotate/loaders/clinvar.py`,
`annotate/loaders/gwas_catalog.py`) plus this ledger.

## D1 — the ClinVar HEAD fail-open (finding-010 #13)

`clinvar._resolve_version_via_head` resolved the version label from the upstream `Last-Modified`
header and, on **any** failure short of the privacy gate — an `ExternalCallError` (network /
HTTP 4xx-5xx) or a missing / unparseable `Last-Modified` — fell back to **today's UTC date**
(`_today_label()`) and proceeded. Under the version-pointer model that fallback is corrosive: a
transient HEAD failure mints a fresh `source_version_id` stamped *today*, flips the
`annotation_sources` pointer to it, and orphans the prior rowset under its older date label —
a real supersession event triggered entirely by an upstream hiccup, not by a genuine new
release. Same-day re-runs are indistinguishable from the prior load by label alone. GWAS
Catalog already refuses (`_resolve_version_via_stats` propagates the error), so ClinVar was the
asymmetric outlier.

### DECISION D1 (OQ-1 = A, refuse/propagate) — `DEC-0148`

Ratified at Gate 1: **ClinVar HEAD failure refuses rather than fabricates a label.**
`_resolve_version_via_head` now (a) lets `ExternalCallError` **propagate** (the fail-open
handler is deleted — GWAS-symmetric); (b) **raises `ValueError`** on a missing or unparseable
`Last-Modified` header; and (c) **keeps** the `except ExternalCallsDisabledError:` handler (the
privacy gate stays fail-closed and its subclass ordering is load-bearing — it now carries a
non-bare `clinvar.version.head_call_disabled` marker log so the lone re-raise is not a ruff
TRY203 violation once its `ExternalCallError` sibling is gone). `_today_label` is deleted; no
CLI surface changes.

The two rejected alternatives:

- **Option B (guarded N-day fallback)** — rejected outright: it keeps a fabricated label,
  merely delaying the orphaning.
- **Option C (sidecar-fallback synthesis)** — the resolver falls back to reading the
  `<dest>.version` sidecar (D2's mechanism) instead of raising, coupling D1↔D2. Feasible but
  **not free** (the resolver would gain a `downloads.py` path import it lacks and must use the
  side-effect-free `default_annotations_root()`, not `source_download_dir()`), and it does
  **not** preserve offline rebuild on the *first* transitional run (a pre-PR-10 cache has no
  sidecar → HEAD-fail + sidecar-absent still raises). Deferred; A is the spine.

The one honest cost of A: a `rm -rf data/` rebuild now **requires upstream reachability** to
resolve the ClinVar label. Previously it was offline-capable but *mislabeled* (D2). Correct
label > offline convenience for a personal-use app; the operator retries when upstream is back.

## D2 — the rebuild-relabel decoupling (finding-022 #4, finding-005 #10)

The on-disk download cache survives a `rm -rf data/` rebuild on purpose (skip-if-exists in
`download_to_cache`, so a rebuild does not re-pull hundreds of MB). But ClinVar/GWAS resolve
the label from a **live** call *before* the download. On a rebuild the loader has no active row,
Step-1 resolves the *current upstream* label (e.g. a June release), Step-3 returns the *cached
older bytes* (e.g. May) with no fetch, and Step-4 stamps the June label onto May bytes.
`DownloadResult` carried `path`/`sha256`/`size_bytes` only — **no cache-hit signal** — so the
loader could not tell the label did not describe the bytes. finding-014's hash fallback cannot
catch this on a rebuild: it reconciles against the *active* row, and on a fresh rebuild there is
no active row.

### DECISION D2 (sidecar bind + inline steady-state guard) — `DEC-0149`

**Bytes-bound label via a version sidecar.** `DownloadResult` gains two trailing, defaulted
fields — `from_cache: bool = False` and `cached_version_label: str | None = None` — and
`download_to_cache` gains a trailing `version_label: str | None = None` kwarg. On a **fresh**
download (and only then) the label is persisted to a `<dest>.version` sidecar
(`dest.with_name(dest.name + '.version')` — a string append, *not* `with_suffix`, so
`variant_summary.txt.gz` yields `…txt.gz.version` and does not clobber the `.gz`), chmod `0600`,
**best-effort** (a write failure logs `annotate.download.version_sidecar_write_failed` and never
aborts the download — the bytes are the payload). A **cache hit** returns `from_cache=True` and
reads the sidecar back into `cached_version_label`. The two fields are trailing+defaulted so all
existing keyword-construction call sites (4 non-target consumers + every test ctor) are
byte-unaffected; this is a frozen dataclass, not a DB schema — no `ddl/` or `docs/schemas/`
touch.

**Loader rebind (3b/3c in clinvar, 3c/3d in gwas).** After the download, each loader:

1. **Rebinds** `version` to `cached_version_label` when `from_cache` is true and the sidecar
   label differs from the live-resolved one — so the `source_version` row identifies the bytes
   it loads (`*.version.label_rebound_to_cache`). A cache hit with **no** sidecar warns
   (`*.version.unbound_cache_hit`) and proceeds with the live label (see the transitional gap
   below).
2. **Guards** (OQ-4 = 4a-i, inline): when `force` is `False` and the now-rebound
   `(version, sha256)` both match the active row, returns `was_already_current=True` without
   minting a fresh `source_version_id` (`*.skip_already_current_post_rebind`). Version **and**
   hash, not version-only.

### The 3a-before-rebind ordering invariant (LOAD-BEARING)

The rebind MUST run **after** the existing label-based short-circuits — for GWAS specifically
after the finding-014 3a hash-fallback (`current.version != version`, evaluated against the
**live** label) *and* the opt-in `maybe_skip_same_version`. Rebinding first would flip
`current.version != version` to False on a same-content-drifted-label rebuild, skip the 3a
short-circuit, and fall through to a **duplicate row** — the traced `4717ff06` regression.
`test_skip_when_content_unchanged_despite_label_drift` is the standing guard; it stays green
with the rebind wired precisely because the rebind lands after 3a.

### OQ-4 = 4a-i — inline guard, `supersession.py` untouched

Ratified at Gate 1: the steady-state guard is **inlined** in each loader as a version+hash
check against the active row. The rejected 4a-ii would have extracted a shared
`_provable_noop_against_active(...)` core in `supersession.py`; the rejected 4b would have
called `maybe_skip_same_version(skip_if_same_version=True)` from the rebind path, which
overrides the finding-009 #14 opt-in default (off) and is a silent contract change. The inline
form leaves `supersession.py` and the opt-in contract byte-identical.

## The transitional unbound-cache-hit gap (NOT a defect)

A cache populated **before** PR 10 has bytes but no `.version` sidecar. On the first
post-PR-10 rebuild that hits such a cache, `cached_version_label` is `None`, the rebind cannot
fire, and the loader proceeds with the **live** label — i.e. the D2 mislabel can still occur
**once** on a pre-existing cache. This is logged as `*.version.unbound_cache_hit` (a warning,
not an error) and **self-heals on the next `--force`**, which re-downloads and writes the
sidecar. It is a deliberate, bounded transitional window, documented here so a reader does not
read the warning as a fresh bug. (Option C would have had the same first-run hole on the
resolver path — another reason it was not free.)

## Scope boundary — what PR 10 does NOT do

- **`RM-25072d2` stays OPEN and separate** (OQ-3): generalizing finding-014's hash-match
  fallback into a shared `maybe_skip_on_hash_match(...)` helper is a *refactor of the existing
  gwas-only 3a*, orthogonal to the label-binding spine here. The inline 3c/3d guard is version+
  **hash** but is not that shared helper. Left `[ ]` in ROADMAP.
- **Sidecar write/read atomicity is not adversarial-hardened** (`RM-fd3f213`, follow-up): a
  swallowed sidecar-write failure can leave a STALE sidecar rather than degrading to ABSENT, and
  `_read_version_sidecar` swallows broad `OSError` without a log. Real on-theme label-correctness
  hardening (temp-file+atomic-rename / narrow to `FileNotFoundError`) but only adversarially
  reachable under the single-user `0700`/`0600` cache — captured forward, not fixed here.
- **Already-mislabeled live rows** are not rewritten (finding-022 #11): a future
  `refresh --source {clinvar,gwas_catalog} --force` against a re-pulled current cache mints a
  correctly-labeled row. PR 10 stops *new* mislabels; it does not retro-correct old ones.
- **dbSNP / gnomAD are out.** They resolve version from their own source (not
  `download_to_cache`) and are not wired.

## Empty-anchor negative control (structural)

`manifest.applicable_anchors = []`. This PR runs **no** live refresh and computes **no**
genome real-data count, so the CLAUDE.md "Real-data observations" anchors (obs #3/#4/#7/#8)
cannot move — the negative control is *structural*, not a re-measured hold. `git diff main --
ddl/ docs/schemas/ CLAUDE.md` is empty by construction (adding a Real-data entry would itself
break the empty-anchor invariant, so none is added). Verification block:
[`verification.md`](../runbooks/verification.md) "PR 10 version-label correctness gate".

## Provenance

Ships under `RM-9f3c52c` / `MEMORY.md` `DEC-0148` (D1 refuse) + `DEC-0149` (D2 sidecar bind +
inline steady-state guard). Supersedes the fail-open behavior described in finding-010 #13 and
the decoupling described in finding-022 #4-#10 / finding-005 #10 (all amended to SHIPPED).
finding-014's `maybe_skip_on_hash_match` generalization (`RM-25072d2`) remains open.
