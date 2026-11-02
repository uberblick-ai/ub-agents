# Changelog and releases

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

Good: "Launchers can run a specific issue or PR while applying the normal
eligibility checks (#127)."

Bad: "Refactored selection internals and added comprehensive validation (#127)."

## Pull requests

PRs do not edit `CHANGELOG.md`, so open PRs do not conflict with each other on it.
The implementer titles the PR with its user-visible change. An internal-only PR,
such as tests, CI or a refactor without behavior change, says so in its body.

A PR needs an **Upgrading** note in its body for breaking changes, changed defaults
or required operator actions. The reviewer treats an inaccurate PR title or a
missing required **Upgrading** note as a required correction.

Good: "**Upgrading:** Set the renamed configuration key before restarting each
launcher." Bad: omit the note because the new key appears in the diff.

## Release preparation

Every release contains at least one user-visible change. Internal-only work ships
with a user-visible change, rather than as a release on its own.

The release-prep issue writes the release's section from the PRs merged since the
previous tag, in the Added, Changed, Removed or Fixed group, each entry ending with
the issue or PR number. Release prep consolidates related changes, keeps every
issue or PR reference and required upgrade information, dates the version's section
(folding in any `Unreleased` entries) and bumps `pyproject.toml`. It stays a loop
issue and ends with a merged release-prep PR.

A patch release contains only fixes. Anything under Added, Changed or Removed, a
new config key or value, or an upgrade step beyond restarting launchers one at a
time makes it a minor release. Milestones are numbered the same way.

## Publishing releases

A maintainer invokes the [release skill](../../.agents/skills/release/SKILL.md)
after release prep merges. `mise run release X.Y.Z` checks current `origin/main`,
a clean checkout, the prepared version and dated CHANGELOG section, an unused tag
and a green GitHub Actions `Test` run on the commit. It pushes an annotated tag,
copies the section without its heading into the GitHub release notes, and opens a
PR updating the Homebrew tap formula.
It tests the tag and exact formula resource versions in a fresh Python 3.14
virtualenv and posts the result to that PR. A maintainer reviews and merges the tap
PR, then upgrades Homebrew and verifies the installed version.

No loop role cuts releases. Implementers and reviewers test the tooling with fakes
and never run it against the real repositories. The task never merges the tap PR
or moves or deletes a pushed tag. See the [release procedure](../releases.md) for
prerequisites and completing a partially published release.
