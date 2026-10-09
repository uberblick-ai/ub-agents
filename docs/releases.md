# Publishing ub-agents releases

Release prep stays a loop issue. Its PR consolidates the merged changes into a
dated `## X.Y.Z — YYYY-MM-DD` section in `CHANGELOG.md` and bumps `pyproject.toml`.
After that PR merges, a maintainer can invoke the repository's
[release skill](../.agents/skills/release/SKILL.md) to check readiness, show the
notes and confirm publication. No loop role publishes a release.

The task needs Git, `gh` authenticated with permission to push tags and open tap
PRs, and Python 3.14 with venv support (`python3.14` on PATH or Homebrew's
`python@3.14`). Source dependencies may also need build tools. It uses the standard
library and pip in temporary virtualenvs, without adding package dependencies.
Git's normal commit and tag signing configuration applies.

From a clean checkout at current `origin/main`:

```sh
mise trust
python3 bin/release.py X.Y.Z --check
mise run release X.Y.Z
```

Replace X.Y.Z with the prepared version. The check fetches `origin/main` and
refuses unless HEAD matches it, the version and dated notes match, the tag is
absent locally and on origin, and that commit has a green `signoff`. If signoff
is missing, run `mise run ci SHA` from `origin/main` and check again. Each refusal
names the failing check and creates no tag, release or tap PR.

The version must match both `pyproject.toml` and `__version__` in
`src/ub_agents/__init__.py`; an unreadable file or missing `__version__` also
refuses. Preflight requires a Python 3.14 interpreter, looking for `python3.14`
on PATH first, then `brew --prefix python@3.14`. A missing interpreter, Homebrew
or formula, or an interpreter reporting another Python version, refuses before
publication.

The task pushes an annotated `vX.Y.Z` tag, creates the GitHub release
`ub-agents X.Y.Z` with the CHANGELOG section verbatim except its heading, and
opens a PR on `uberblick-ai/homebrew-tap`. The formula URL and checksum describe
the tag tarball served by GitHub. Pip resolves that tarball's full dependency
closure with Python 3.14, selecting source archives. Resource blocks for matching
versions stay intact; changed versions are updated and dependencies are added or
removed as needed. Resolution uses pip's
[installation report](https://pip.pypa.io/en/stable/reference/installation-report/).

After opening the tap PR, a separate fresh virtualenv installs the tag tarball
and only the formula's resource archives, checking their checksums, installed
versions and dependency compatibility. It runs the formula's four tests:

- `ub-agents --version` output must contain `ub-agents X.Y.Z`.
- `ub-agents init --repository example/project` must succeed.
- `ub-agents check` output must contain `Valid configuration: example/project`.
- `from ub_agents.view_ui import View` must import successfully.

The task prints the result and comments on the tap PR. These tests replay the
formula's steps; they do not build a Homebrew bottle. A maintainer reviews and
merges the tap PR. Follow any **Upgrading** notes, then run
`brew update && brew upgrade ub-agents` and verify `ub-agents --version` outside
this checkout, where mise cannot select the development install.

## Partial publication

On a failure, the task exits non-zero and lists completed and remaining steps,
with links to published artifacts. A failing formula test is also posted to the
tap PR when GitHub is reachable. If a publication command failed, inspect remote
state first: the server may have accepted it before the connection failed.

Do not rerun the task once a local or remote tag exists. Never move or delete a
pushed tag. Complete the remaining steps manually using the existing tag and
release notes: create the missing GitHub release, complete or open the formula
PR, or rerun and post the formula tests. If a tag push failed before reaching
origin, inspect the local annotated tag and its target before deciding to push
that same tag. A tap branch pushed before PR creation failed can be used to open
the missing PR. If the formula needs correction, update the existing tap PR;
merge only after its tests pass.
