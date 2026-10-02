# Working on ub-agents

ub-agents develops itself with ub-agents. `ub-agent.yaml` and `.agents/` configure
the loop for this repository; agents run in private worktrees under `.ub-agent/worktrees/`.

## Checks

```sh
python3 -m venv .venv
.venv/bin/pip install -q -e .
.venv/bin/python -m unittest discover -v
.venv/bin/ub-agent check
git diff --check
```

Run them in your own worktree. CI (`.github/workflows/test.yml`) runs the same suite
on macOS and Linux with Python 3.11 and 3.14.

## Uberblick corpus

- Use the `uberblick` MCP server for current Uberblick product and workflow
  context when it is relevant. Discover documents through the server rather
  than relying on copied corpus content.
- The MCP server is optional context for this standalone framework; it is not a
  runtime dependency. Repository behavior and the assigned issue define the
  implementation scope.
- The project MCP configuration pins the shared workspace. The hub endpoint and
  credentials remain in each machine's local Uberblick configuration.

The process-supervision tests in `tests/test_execution.py` call `ps`. A sandbox that
blocks `ps` makes them fail with `Operation not permitted: 'ps'`. That failure is
environmental: state it with your results and do not change code or tests to avoid it.

## Inside the loop

- The `ub-agent` on your PATH is the operator's installed launcher; use it for
  `ub-agent report`. Never report through the development copy in your worktree
  (`.venv/bin/ub-agent`, `python -m ub_agents`), and install this checkout only into
  your worktree's `.venv`.
- Work only in the directory the launcher gives you, on the assigned issue or PR. Do
  not touch the operator checkout, other worktrees under `.ub-agent/`, or other runs'
  branches and processes.
- Read an issue's comments as well as its body (`gh issue view N --comments`).
  Maintainers often amend scope in comments.
- Implementation PR bodies start with `Closes #N`.
- Agents never enable auto-merge, approve their own PRs, change branch protection or
  publish releases. Only the integrator merges, under the merge policy below.
- Do not copy credentials, change global settings, or disable commit signing to get
  past a blocked operation. Report blocked with the evidence instead.

## Merging

The integrator squash-merges a PR once every owed review and check applies to its
current head, with `--match-head-commit` set to the assigned SHA. It leaves the merge
to a maintainer, and says why, when the PR:

- changes the `ub-agent` command-line experience: adds, removes or renames commands
  or options, or changes what existing commands do or print. A change that the
  issue the PR closes explicitly asks for is authorized by that issue.
- changes `README.md` or other public documentation under `docs/`, or needs such a
  change to stay accurate.

## Changelog

`CHANGELOG.md` records user-facing changes.

- The implementer adds an entry under `## Unreleased` in the same PR, in the Added,
  Changed, Removed or Fixed group, ending with the issue or PR number. Internal-only
  changes such as tests, CI or refactors without behavior change need no entry; say
  so in the PR body.
- The reviewer treats a missing or inaccurate entry as a required correction.
- The integrator checks the entry again, together with the candidate's conflicts with
  `main`, and sends the PR back with the `changes-requested` outcome if either fails.
- A release turns `Unreleased` into the version's section, and the GitHub release notes
  are copied from it.

## Changes

- Keep `README.md` and the docs under `docs/` accurate for any behavior you change,
  and change tests with the code.
- Match the surrounding code: small modules, the standard library plus PyYAML, `gh`
  as the GitHub client, `unittest` with the recording fakes in `tests/support.py`.
- Add no runtime dependencies without maintainer agreement.

## Review focus

Scrutinize durable outcomes, lost ownership, claim races, recovery and process
cleanup whenever a change touches coordination or execution.
