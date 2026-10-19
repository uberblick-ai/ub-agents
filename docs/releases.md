# Publishing ub-agents releases

Release prep stays a loop issue. Its PR consolidates the merged changes into a
dated `## X.Y.Z — YYYY-MM-DD` section in `CHANGELOG.md` and bumps `pyproject.toml`.
After that PR merges, a maintainer can invoke the repository's
[release skill](../.agents/skills/release/SKILL.md) to check readiness, show the
notes and confirm publication. No loop role publishes a release.

The task needs Git, `gh` authenticated with permission to push tags, update the
`website` branch and open tap PRs, and Python 3.14 with venv support (`python3.14`
on PATH or Homebrew's `python@3.14`). Source dependencies may also need build
tools. It uses the standard library and pip in temporary virtualenvs, without
adding package dependencies. Git's normal commit and tag signing configuration
applies.

From a clean checkout at current `origin/main`:

```sh
mise trust
python3 bin/release.py X.Y.Z --check
mise run release X.Y.Z
```

Replace X.Y.Z with the prepared version. The check fetches `origin/main` and
refuses unless HEAD matches it, the version and dated notes match, the tag is
absent locally and on origin, and the newest GitHub Actions `Test` run on that
commit passed. If the run is still going, wait for it and check again; if it
failed, rerun it from the Actions tab or fix main first. Each refusal names the
failing check and creates no tag, release or tap PR.

The version must match both `pyproject.toml` and `__version__` in
`src/ub_agents/__init__.py`; an unreadable file or missing `__version__` also
refuses. Preflight requires a Python 3.14 interpreter, looking for `python3.14`
on PATH first, then `brew --prefix python@3.14`. A missing interpreter, Homebrew
or formula, or an interpreter reporting another Python version, refuses before
publication.

The task pushes an annotated `vX.Y.Z` tag, updates `website` to that tag's commit
as a separate step, then creates the GitHub release
`ub-agents X.Y.Z` with the CHANGELOG section verbatim except its heading, and
opens a PR on `uberblick-ai/homebrew-tap`. The formula URL and checksum describe
the tag tarball served by GitHub. Pip resolves that tarball's full dependency
closure with Python 3.14, selecting source archives. Resource blocks for matching
versions stay intact; changed versions are updated and dependencies are added or
removed as needed. Resolution uses pip's
[installation report](https://pip.pypa.io/en/stable/reference/installation-report/).

The website step runs `git push origin vX.Y.Z^{commit}:refs/heads/website` without
force. If `website` is not an ancestor of the tag commit, Git rejects the push and
the task stops before creating the GitHub release. Cloudflare Pages builds
production from `website`, keeping the site's docs and header version at the
latest release. Changes under `site/content/`, including docs fixes between
releases, wait for the next release. Merging to `main` alone does not deploy the
production site; Pages may still build branch previews.

For the initial switch, a maintainer creates `website` at the latest release
before changing Pages. With `v0.3.2` as that release:

```sh
git push origin v0.3.2^{commit}:refs/heads/website
```

In a separate `uberblick-infra` PR, set both `production_branch` fields for the
ub-agents Pages project in `cloudflare/main.tf` to `"website"`, leaving
`path_includes` unchanged. A maintainer applies that change from their machine.
Implementers and reviewers test release tooling with fakes only; they never push
`website` to the real repository.

After opening the tap PR, a separate fresh virtualenv installs the tag tarball
and only the formula's resource archives, checking their checksums, installed
versions and dependency compatibility. Before running the formula's four tests,
it creates a Git repository in the test project with `git init` and sets origin
with `git remote add origin https://github.com/example/project.git`:

- `ub-agents --version` output must contain `ub-agents X.Y.Z`.
- `ub-agents init --repository example/project` must succeed.
- `ub-agents doctor --offline` output must contain `0 required failures`.
- `from ub_agents.view_ui import View` must import successfully.

For the first release without `check`, the tap PR must also update the formula's
`test do` block to initialize this Git repository and origin before `init`, then
run `doctor --offline` and expect `0 required failures`.

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
release notes: update `website`, create the missing GitHub release, complete or
open the formula PR, or rerun and post the formula tests. If a tag push failed
before reaching origin, inspect the local annotated tag and its target before
deciding to push that same tag. If the website step is still to do, inspect
origin's `website` branch and the existing tag's commit, then run
`git push origin vX.Y.Z^{commit}:refs/heads/website` with the literal release
version. Keep the push non-forced; if it is rejected because the histories
diverge, stop for a maintainer to resolve the branch history before continuing
with the GitHub release and tap PR. A tap branch pushed before PR creation failed
can be used to open the missing PR. If the formula needs correction, update the
existing tap PR; merge only after its tests pass.
