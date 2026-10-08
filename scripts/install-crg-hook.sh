#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Install the code-review-graph pre-commit hook in a form that is safe for
# `git worktree` checkouts. Per-machine: git hooks are not versioned, so each
# clone runs this once.
#
#   sh scripts/install-crg-hook.sh
#
# Why not plain `code-review-graph install`: releases before 2.3.9 wrote an
# unguarded block (`code-review-graph update` + `detect-changes`, no timeout).
# In a linked worktree there is no graph.db yet, so `update` falls into a full
# graph build of that checkout (multiprocessing parse pool), and an agent
# committing there appeared to hang -- orphaned `code-review-graph update`
# forkserver workers were left behind. This block keeps the hook's purpose
# (graph refresh + risk summary) in the main checkout, skips it in linked
# worktrees (CRG_HOOK_WORKTREES=1 opts back in), and bounds every call with
# `timeout` so a stuck graph can never block a commit.
set -eu

hooks_dir=$(git rev-parse --git-path hooks)
case $hooks_dir in /*) ;; *) hooks_dir="$(git rev-parse --show-toplevel)/$hooks_dir" ;; esac
hook="$hooks_dir/pre-commit"
begin='# >>> tan-cli crg pre-commit hook >>>'
end='# <<< tan-cli crg pre-commit hook <<<'

mkdir -p "$hooks_dir"
if [ -f "$hook" ]; then
    if grep -qF "$begin" "$hook"; then
        # Re-run: drop our previous block, keep everything else.
        tmp=$(mktemp)
        awk -v b="$begin" -v e="$end" '$0==b{skip=1} !skip{print} $0==e{skip=0}' "$hook" > "$tmp"
        cat "$tmp" > "$hook"
        rm -f "$tmp"
    elif grep -qF 'code-review-graph' "$hook"; then
        # A hook that is nothing but the unguarded stock block (shebang, note,
        # 5-line if/fi) is ours to replace; anything longer is hand-edited.
        if [ "$(grep -c . "$hook")" -le 7 ]; then
            printf '#!/bin/sh\n' > "$hook"
        else
            echo "install-crg-hook: $hook has a hand-edited code-review-graph block;" >&2
            echo "remove it and re-run." >&2
            exit 1
        fi
    fi
else
    printf '#!/bin/sh\n' > "$hook"
fi

cat >> "$hook" <<BLOCK
$begin
if command -v code-review-graph >/dev/null 2>&1; then
    crg_git_dir=\$(git rev-parse --absolute-git-dir 2>/dev/null) || crg_git_dir=""
    crg_root=\$(git rev-parse --show-toplevel 2>/dev/null) || crg_root=""
    if [ -z "\$crg_root" ]; then
        :
    elif [ -f "\$crg_git_dir/commondir" ] && [ "\${CRG_HOOK_WORKTREES:-}" != "1" ]; then
        echo "code-review-graph: skipped in a linked worktree (CRG_HOOK_WORKTREES=1 to enable)." >&2
    else
        timeout 60 code-review-graph update --repo "\$crg_root" </dev/null || true
        timeout 60 code-review-graph detect-changes --brief --repo "\$crg_root" </dev/null || true
    fi
fi
$end
BLOCK
chmod +x "$hook"
echo "install-crg-hook: wrote $hook"
