#!/bin/sh
# Local CI: verify one commit on this machine, for example before pushing it.
#
#   mise run ci [commit]    # defaults to HEAD
#
# The commit is checked out into a temporary worktree with a fresh virtualenv,
# so nothing from this checkout (its .venv, untracked files, local edits) can
# make a run pass. The steps mirror the GitHub Actions `Test` workflow, which
# gates merges: whitespace errors against origin/main, the unit suite on every
# core, and `ub-agents check`. This run is optional and posts nothing to GitHub.
set -eu

root=$(CDPATH= cd "$(dirname "$0")/.." && pwd -P)

if [ $# -gt 1 ]; then
	printf 'usage: mise run ci [commit]\n' >&2
	exit 2
fi
commit=${1:-HEAD}

git -C "$root" fetch --quiet origin main
git -C "$root" cat-file -e "$commit^{commit}" 2>/dev/null || git -C "$root" fetch --quiet origin "$commit"
sha=$(git -C "$root" rev-parse --verify --end-of-options "$commit^{commit}")
short=$(printf '%s' "$sha" | cut -c1-12)

worktree=$(mktemp -d "${TMPDIR:-/tmp}/ub-agents-ci.XXXXXX")
cleanup() {
	git -C "$root" worktree remove --force "$worktree" 2>/dev/null || rm -rf "$worktree"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM
git -C "$root" worktree add --quiet --detach "$worktree" "$sha"
cd "$worktree"

# The commit's own code runs without this machine's GitHub and git credentials,
# so it cannot push or call GitHub in the caller's name.
mkdir "$worktree.gh"
trap 'cleanup; rm -rf "$worktree.gh"' EXIT
isolated() {
	env GH_CONFIG_DIR="$worktree.gh" GH_TOKEN= GITHUB_TOKEN= GH_ENTERPRISE_TOKEN= \
		GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
		GIT_AUTHOR_NAME=ci GIT_AUTHOR_EMAIL=ci@example.invalid \
		GIT_COMMITTER_NAME=ci GIT_COMMITTER_EMAIL=ci@example.invalid \
		SSH_AUTH_SOCK= "$@"
}

run() {
	name=$1
	shift
	printf '\n== %s (%s)\n' "$name" "$short"
	if ! isolated "$@"; then
		printf '\nci: %s failed at %s.\n' "$name" "$short" >&2
		exit 1
	fi
}

run "Whitespace" git --no-pager diff --check "origin/main...$sha" --
run "Install" sh -c 'python3 -m venv .venv && .venv/bin/python -m pip install -q -e .'
run "Tests" .venv/bin/python -m tests
run "Config check" .venv/bin/ub-agents check

printf '\nci: all checks passed at %s\n' "$short"
