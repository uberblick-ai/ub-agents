# Working on ub-agents

ub-agents develops itself with ub-agents. `ub-agents.yaml` and `.agents/` configure
the loop for this repository; agents run in private worktrees under `.ub-agents/worktrees/`.

## Checks

```sh
python3 -m venv .venv
.venv/bin/pip install -q -e .
.venv/bin/python -m tests
.venv/bin/ub-agents check
git diff --check
```

Run them in your own worktree while you work.

## Local CI

CI runs on a maintainer's or the integrator's machine, not on GitHub. From a checkout
at `origin/main`, for a pushed commit:

```sh
mise run ci <sha>
```

mise only reads a trusted `mise.toml`, and every fresh worktree is a new path, so an
agent runs `mise trust` in its own worktree before its first `mise` command. That is
expected and needs no approval.

It checks the commit out into a temporary worktree with a fresh virtualenv, runs
`git diff --check`, the unit suite, and
`ub-agents check`. When all pass it posts a green `signoff` commit status through
[gh-signoff](https://github.com/basecamp/gh-signoff); a failure posts a red one.
Install the extension once with `gh extension install basecamp/gh-signoff`. The
script refuses to run from a checkout other than `origin/main`, because main owns the
recipe. GitHub Actions runs the suite on Linux with Python 3.11 and 3.14 after each
merge to main (`.github/workflows/test.yml`).

## Uberblick corpus

- Two MCP servers reach two corpora. Use `uberblick-agents` for this
  framework's workflow context and `uberblick-product` for current Uberblick
  product context, when relevant. Discover documents through the servers
  rather than relying on copied corpus content.
- The MCP servers are optional context for this standalone framework; they are
  not a runtime dependency. Repository behavior and the assigned issue define
  the implementation scope.
- The project MCP configuration pins each server's workspace and hub.
  Credentials remain in each machine's local Uberblick configuration.

The process-supervision tests in `tests/test_execution.py` call `ps`. A sandbox that
blocks `ps` makes them fail with `Operation not permitted: 'ps'`. That failure is
environmental: state it with your results and do not change code or tests to avoid it.

## Changelog

`CHANGELOG.md` and GitHub release notes give users a concise overview of what changed.

- Use a neutral, factual tone. Lead with the user-visible capability or effect,
  not the implementation. Avoid promotional language and development narration.
- Aim for one short sentence per bullet, usually 15–30 words, followed by the issue
  or PR reference. Leave algorithms, internal state, validation details and edge-case
  inventories in the linked issue or PR; link to documentation for usage details.
- Keep breaking changes, changed defaults and required operator actions explicit.
  Put upgrade instructions in one short **Upgrading** note rather than repeating
  them across bullets. Link to a migration guide for a longer procedure; do not
  omit essential compatibility warnings or steps just to meet the length target.

For example: "Launchers can run a specific issue or PR while applying the normal
eligibility checks (#127)."

PRs do not edit `CHANGELOG.md`, so open PRs do not conflict with each other on it.
The release-prep issue writes the release's section from the PRs merged since the
previous tag, in the Added, Changed, Removed or Fixed group, each entry ending with
the issue or PR number.

- The implementer titles the PR with its user-visible change and puts any
  **Upgrading** note in the PR body. An internal-only PR, such as tests, CI or a
  refactor without behavior change, says so in its body.
- The reviewer treats an inaccurate PR title or a missing **Upgrading** note as a
  required correction.
- Release prep consolidates related changes, keeps every issue or PR reference and
  required upgrade information, dates the version's section (folding in any
  `Unreleased` entries) and copies that section into the GitHub release notes.

## Changes

- Keep `README.md`, the docs under `docs/` and the website pages under `site/content/`
  accurate for any behavior you change, and change tests with the code.
- Match the surrounding code: small modules, the standard library plus PyYAML, `gh`
  as the GitHub client, `unittest` with the recording fakes in `tests/support.py`.
- Add no runtime dependencies without maintainer agreement.
