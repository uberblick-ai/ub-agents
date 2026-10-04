#!/bin/sh
# Local CI: verify one pushed commit on this machine and sign off on it.
#
#   mise run ci <sha>
#
# The commit is checked out into a temporary worktree with a fresh virtualenv,
# so nothing from this checkout (its .venv, untracked files, local edits) can
# make a run pass. The steps mirror what GitHub ran before: the unit suite with
# and without the `ui` extra, `ub-agents check`, and whitespace errors in the
# diff. When every step passes the commit gets the `signoff` status, which is
# the merge gate; a failed step posts a red status instead. Signing off needs
# the gh-signoff extension.
#
# Run it from a checkout at origin/main: main owns this recipe, and the commit
# under test only supplies the code it runs.
set -eu

root=$(CDPATH= cd "$(dirname "$0")/.." && pwd -P)

if [ $# -ne 1 ]; then
	printf 'usage: mise run ci <sha>\n' >&2
	exit 2
fi

if ! gh signoff --help >/dev/null 2>&1; then
	printf 'ci: gh signoff is missing; install it with `gh extension install basecamp/gh-signoff`.\n' >&2
	exit 1
fi

git -C "$root" fetch --quiet origin main
git -C "$root" cat-file -e "$1^{commit}" 2>/dev/null || git -C "$root" fetch --quiet origin "$1"
if [ "$(git -C "$root" rev-parse HEAD)" != "$(git -C "$root" rev-parse origin/main)" ] ||
	! git -C "$root" diff --quiet origin/main -- bin/ci.sh mise.toml; then
	printf 'ci: refusing; run from a checkout at origin/main with bin/ci.sh and mise.toml unmodified.\n' >&2
	exit 1
fi
sha=$(git -C "$root" rev-parse --verify --end-of-options "$1^{commit}")
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
# so it cannot post a signoff or push in the caller's name.
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
		gh signoff fail --commit "$sha" --description "$name failed" >/dev/null
		printf '\nci: %s failed at %s; posted a failing status.\n' "$name" "$short" >&2
		exit 1
	fi
}

run "Whitespace" git diff --check "origin/main...$sha" --
run "Install" sh -c 'python3 -m venv .venv && .venv/bin/python -m pip install -q -e .'
run "Tests" .venv/bin/python -m unittest discover
run "Config check" .venv/bin/ub-agents check
run "Install ui extra" .venv/bin/python -m pip install -q -e '.[ui]'
run "Tests with ui extra" .venv/bin/python -m unittest discover

gh signoff --commit "$sha"
printf '\nci: signed off %s\n' "$short"
