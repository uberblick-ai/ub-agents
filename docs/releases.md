# Publishing ub-agents releases

You publish a release after its release-prep PR merges; release prep itself is a
loop issue, described in [AGENTS.md](../AGENTS.md#changelog). A maintainer
invokes the [release skill](../.agents/skills/release/SKILL.md), which follows
this procedure. No loop role publishes a release.

You need Git, `gh` authenticated with permission to push tags and open tap PRs,
and Python 3.14 with venv support (`python3.14` on PATH or Homebrew's
`python@3.14`). Source dependencies may also need build tools. Git's normal commit
and tag signing configuration applies.

From a clean checkout at current `origin/main`:

```sh
mise trust
python3 bin/release.py X.Y.Z --check
mise run release X.Y.Z
```

Replace X.Y.Z with the prepared version. `--check` runs only the preflight and
prints the release notes. The preflight refuses unless HEAD is a clean checkout of
current `origin/main`, `pyproject.toml` and `__version__` match X.Y.Z, `CHANGELOG.md`
has a dated section for it (with no Added, Changed or Removed group for a patch
version), the tag is unused locally and on origin, Python 3.14
is available, and the newest GitHub Actions `Test` run on the commit passed. Each
refusal names its check and publishes nothing.

Release prep must put exactly one machine-readable marker in the version's dated
CHANGELOG section. Publication copies it verbatim into the GitHub release body;
the preflight refuses a missing, duplicate or unknown marker:

```html
<!-- ub-agents:launcher-upgrade=independent -->
```

Use `independent` only when launchers may upgrade and restart one at a time. Use
the following whenever every launcher of a project must stop and upgrade together,
including incompatible coordination changes or configuration this repository
starts using that older launchers reject:

```html
<!-- ub-agents:launcher-upgrade=coordinated -->
```

For `coordinated`, also include a visible **Upgrading** note with the required
operator steps. The marker supplements that note. Automatic launcher maintenance
reads all published releases newer than its running version through its target,
including intermediate releases across skipped versions. Any coordinated,
missing or unreadable marker prevents automatic upgrading and restarting. Keep
the marker when finishing partial publication or editing release notes. Older
unmarked releases deliberately require manual upgrades.

The task pushes an annotated `vX.Y.Z` tag, creates the GitHub release
`ub-agents X.Y.Z` from the CHANGELOG section without its heading, and opens a PR on
`uberblick-ai/homebrew-tap` that updates the formula. It then tests the tag and
the formula's exact resources in a fresh Python 3.14 virtualenv, without building
a Homebrew bottle, and posts the result to the tap PR.

A maintainer reviews and merges the tap PR. The release skill's last step then
upgrades Homebrew and checks the installed version.

## Partial publication

On a failure, the task exits non-zero and lists completed and remaining steps,
with links to published artifacts. A failing formula test is also posted to the
tap PR when GitHub is reachable. If a publication command failed, inspect remote
state first: the server may have accepted it before the connection failed.

Do not rerun the task once a local or remote tag exists. Never move or delete a
pushed tag. Complete the remaining steps by hand using the existing tag and release
notes: create the missing GitHub release, complete or open the formula PR, or rerun
and post the formula tests. If a tag push failed before reaching origin, inspect
the local annotated tag and its target before you push that same tag. A tap branch
pushed before PR creation failed can be used to open the missing PR. If the
formula needs correction, update the existing tap PR and merge only after its
tests pass.
