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

The process-supervision tests in `tests/test_execution.py` call `ps`. A sandbox that
blocks `ps` makes them fail with `Operation not permitted: 'ps'`. That failure is
environmental: state it with your results and do not change code or tests to avoid it.

## Inside the loop

- The `ub-agent` on your PATH is the operator's installed launcher; use it for
  `ub-agent report`. Never report through the development copy in your worktree
  (`.venv/bin/ub-agent`, `python -m ub_agents`), and install this checkout only into
  your worktree's `.venv`.
- Work only in the directory the launcher gives you. Do not touch the operator
  checkout, other worktrees under `.ub-agent/`, or other runs' branches and processes.
- Read an issue's comments as well as its body (`gh issue view N --comments`). The
  owner often amends scope in comments.
- Implementation PR bodies start with `Closes #N`.
- Agents never merge, enable auto-merge, approve their own PRs, change branch
  protection or publish releases. The owner merges.
- Do not copy credentials, change global settings, or disable commit signing to get
  past a blocked operation. Report blocked with the evidence instead.
- Do not modify Uberblick or its running delivery loops.

## Changes

- Keep `README.md` and `docs/coordination.md` accurate for any behavior you change,
  and change tests with the code.
- Match the surrounding code: small modules, the standard library plus PyYAML, `gh`
  as the GitHub client, `unittest` with the recording fakes in `tests/support.py`.
- Add no runtime dependencies without the owner's agreement.
