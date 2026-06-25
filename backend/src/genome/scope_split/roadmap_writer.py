"""Append-only ROADMAP managed-block writer for scope-split (``finding-039``; plan §4 §7).

``append_roadmap_block`` is a **pure string transform**: given the current ROADMAP text and a
rendered block, it replaces only the region between the
``<!-- B2-SUBSCOPES:BEGIN -->`` / ``<!-- B2-SUBSCOPES:END -->`` sentinels, leaving every
hand-authored line untouched. The CLI does the read / call / write-if-changed around it
(mirroring how :mod:`genome.fast_follow.persistence` keeps I/O out of the pure core); the
transform itself is byte-idempotent (mech #9) so a re-run with the same block is a no-op.

A ROADMAP missing the managed slot or the sentinels raises rather than appending blindly — the
clobber guard (plan failure-ordering (b)).

**No** :mod:`genome.db` import. The transform body is **stubbed** at interface-freeze
(``raise NotImplementedError``); the three constants are real (the sentinels the parser keys on
and the default path).
"""

from __future__ import annotations

from pathlib import Path

#: The opening sentinel of the ROADMAP managed block. Lives ONLY in ROADMAP.md, never in
#: scope-split.md (mech #10 — avoids a doc-consistency regex collision).
BLOCK_BEGIN: str = "<!-- B2-SUBSCOPES:BEGIN -->"

#: The closing sentinel of the ROADMAP managed block.
BLOCK_END: str = "<!-- B2-SUBSCOPES:END -->"

#: The default ROADMAP path the CLI reads/writes when ``--roadmap`` is not given.
DEFAULT_ROADMAP_PATH: Path = Path("ROADMAP.md")


def append_roadmap_block(roadmap_text: str, block: str, *, origin_scope: str) -> str:
    """Splice ``block`` into the ROADMAP managed region — a pure, idempotent transform (plan §4).

    Replaces the text between :data:`BLOCK_BEGIN` and :data:`BLOCK_END` with ``block``, normalized
    to a canonical inter-sentinel form so the result is byte-idempotent regardless of the parent's
    trailing newline (mech #9). Raises :class:`ValueError` when the parent lacks the managed slot
    or either sentinel (the clobber guard). ``origin_scope`` is the provenance label for the slots
    inside ``block`` (locked decision #8).

    The transform is a **pure region replace**, so it is naturally idempotent (re-applying the
    same ``block`` yields byte-identical output) and reversible (an empty ``block`` returns the
    managed region to its empty state with no residue). Everything outside the two sentinels is
    byte-identical to the input.
    """
    begin = roadmap_text.find(BLOCK_BEGIN)
    if begin == -1:
        msg = f"ROADMAP is missing the managed-slot begin sentinel {BLOCK_BEGIN!r}"
        raise ValueError(msg)
    end = roadmap_text.find(BLOCK_END, begin)
    if end == -1:
        msg = f"ROADMAP is missing the managed-slot end sentinel {BLOCK_END!r}"
        raise ValueError(msg)

    _ = origin_scope  # provenance is carried inside `block`; kept on the signature (frozen)
    prefix = roadmap_text[: begin + len(BLOCK_BEGIN)]
    suffix = roadmap_text[end:]

    normalized = block.strip()
    # Canonical inter-sentinel region: a single leading + trailing newline framing the content
    # (or just a bare newline when empty), so the result is byte-idempotent regardless of the
    # parent's original trailing-newline shape (mech #9) and an empty block leaves no residue.
    rebuilt_inner = f"\n{normalized}\n" if normalized else "\n"
    return prefix + rebuilt_inner + suffix
