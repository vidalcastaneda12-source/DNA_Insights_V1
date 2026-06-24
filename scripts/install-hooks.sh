#!/usr/bin/env bash
#
# Install the tracked decision-tracking pre-commit hook (finding-036 / DEC-0086).
# Idempotent; run once per clone. Bypass a single commit with `git commit --no-verify`.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
# `git rev-parse --git-path hooks` honors core.hooksPath and is worktree/submodule-safe.
# It may be relative to the repo root, so resolve it to an absolute path.
hooks_dir="$(git -C "$repo_root" rev-parse --git-path hooks)"
case "$hooks_dir" in
    /*) : ;;
    *) hooks_dir="$repo_root/$hooks_dir" ;;
esac
mkdir -p "$hooks_dir"

source_hook="$repo_root/scripts/git-hooks/pre-commit"
target="$hooks_dir/pre-commit"

# Fail loud and early if the tracked hook is missing, rather than creating a dangling link.
if [[ ! -f "$source_hook" ]]; then
    echo "install-hooks: tracked hook missing at $source_hook" >&2
    exit 1
fi

# Refuse to clobber a pre-existing hook that is not our symlink (a real script or a
# foreign symlink), rather than silently overwriting it.
if { [[ -e "$target" ]] || [[ -L "$target" ]]; } &&
    [[ "$(readlink -- "$target" 2>/dev/null || true)" != "$source_hook" ]]; then
    echo "install-hooks: $target already exists and is not ours; not overwriting." >&2
    echo "install-hooks: inspect/remove it, then re-run (or symlink scripts/git-hooks/pre-commit manually)." >&2
    exit 1
fi

# Absolute symlink target (a relative target would resolve from the hooks dir, not the
# repo root, and dangle).
ln -sf "$source_hook" "$target"
chmod +x "$source_hook"

# Self-test: the installed hook must resolve to an existing, executable file.
if [[ ! -x "$target" ]]; then
    echo "install-hooks: FAILED — $target is not executable after install." >&2
    exit 1
fi

echo "install-hooks: installed pre-commit -> scripts/git-hooks/pre-commit"
echo "install-hooks: it runs 'genome docs check' on every commit; bypass one with 'git commit --no-verify'."
