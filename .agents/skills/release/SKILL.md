---
name: release
description: >-
  Publish a prepared ub-agents release and its Homebrew tap PR when a maintainer
  invokes this skill, then verify the installed version after the maintainer
  merges the tap PR. Not for loop roles, implementation or review runs.
---

# Release ub-agents

Use the repository's [release procedure](../../../docs/releases.md). Release prep
is a separate loop issue; this workflow starts after its PR has merged. Work from
a clean checkout at current `origin/main` with `gh`, Git and Python 3.14 available.
Use literal versions, PR numbers and commit SHAs in commands below.

1. Establish go/no-go. Identify the release-prep PR and confirm its `MERGED` state
   with `gh pr view NUMBER --repo uberblick-ai/ub-agents --json state,mergeCommit,url`.
   Fetch `origin/main`, verify HEAD equals it, and check `pyproject.toml` and the
   dated CHANGELOG section for the intended X.Y.Z. Run
   `python3 bin/release.py X.Y.Z --check`: it also checks local and origin tag
   absence and the commit's green `signoff`, and prints the complete release notes.
   If `signoff` is missing, run `mise trust` then `mise run ci SHA` from
   `origin/main`, and repeat the check. A failing signoff or any other failing
   check is no-go. Show the release notes, including **Upgrading** instructions,
   and ask the maintainer to confirm publication of this version and commit.

2. After confirmation, run `mise trust` if needed, then `mise run release X.Y.Z`.
   Report the GitHub release and tap PR links and the formula test result. If it
   fails, report its completed and remaining steps; follow the partial-release
   procedure rather than rerunning publication or moving or deleting a pushed tag.

3. Wait for the maintainer to review and merge the tap PR; never merge it yourself.
   Confirm `MERGED` with `gh pr view NUMBER --repo uberblick-ai/homebrew-tap --json state,url`.
   Apply the release's **Upgrading** instructions before updating; when they call
   for stopping launchers, wait for the maintainer to confirm that step is done.
   Run `brew update && brew upgrade ub-agents`, then `ub-agents --version` from
   outside this development checkout so mise's editable install cannot mask the
   Homebrew version. Confirm it prints exactly `ub-agents X.Y.Z` and report that
   result. If the PR is closed without merging, or the upgrade/version check
   fails, report the state and wait for the maintainer's next step.
