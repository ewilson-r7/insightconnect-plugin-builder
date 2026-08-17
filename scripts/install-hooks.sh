#!/bin/sh
# Enable the versioned hooks in .githooks/ for this clone.
#
# Deliberately opt-in. Enabling changes local git config (core.hooksPath), and it
# takes effect immediately for every session using this checkout -- including any
# agent or IDE workflow mid-task. Run it when nothing else is working in the tree.
#
#   Enable:   ./scripts/install-hooks.sh
#   Disable:  git config --unset core.hooksPath
#   Inspect:  git config --get core.hooksPath
#
# What you get:
#   pre-commit  refuses commits made directly on main/master
#   pre-push    refuses pushes whose remote destination is main/master
#
# Both are bypassable with --no-verify, by design: they are a guardrail against
# habit and automation, not a security control. Server-side branch protection is
# the only real enforcement.

set -eu

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

if [ ! -d .githooks ]; then
    echo "install-hooks: .githooks/ not found at $repo_root" >&2
    exit 1
fi

chmod +x .githooks/pre-commit .githooks/pre-push

current=$(git config --get core.hooksPath || true)
if [ "$current" = ".githooks" ]; then
    echo "install-hooks: already enabled (core.hooksPath=.githooks)"
    exit 0
fi

if [ -n "$current" ]; then
    echo "install-hooks: core.hooksPath is currently '$current'."
    echo "               Refusing to overwrite it. Unset it first if you want ours:"
    echo "                 git config --unset core.hooksPath"
    exit 1
fi

git config core.hooksPath .githooks
echo "install-hooks: enabled (core.hooksPath -> .githooks)"
echo "               disable with: git config --unset core.hooksPath"
