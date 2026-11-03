# Development checks

Run the commands in [AGENTS.md](../../AGENTS.md#checks) in your checkout. They install
the package and the development tools into `.venv`. `requirements-dev.txt` pins
Ruff, mypy and the PyYAML stubs for local checks and GitHub Actions; these tools
are not runtime dependencies.

`ruff check src` checks functions for McCabe complexity above 10 (`C901`), more
than 12 branches (`PLR0912`) and more than 50 statements (`PLR0915`). Functions
already over these limits when the checks were introduced carry a rule-specific
`noqa` on their definition, generated once with `ruff check --add-noqa src`.
This is the existing complexity baseline, not a way to exempt new or growing
functions. Split new logic into smaller functions to keep it within the limits.

`RUF100` rejects unused suppressions. When a refactor brings a function below a
limit, remove that rule from its `noqa`. List the remaining baseline with:

```sh
grep -rn 'noqa: C901\|noqa: PLR09' src
```

`mypy` checks `src/ub_agents` using Python 3.11 and default strictness. It does
not check unannotated function bodies (`check_untyped_defs` is off). Neither
check covers `tests/`.

`tests/test_import_boundaries.py` reads source imports, including function-local
imports, to keep core modules independent of the `view/` package and prevent
a cycle between `log_format` and `log_json`. Shared snapshot grouping and history
paths live in `observation_data`; shared log bounds live in `log_limits`.

## CI

GitHub Actions runs the `Test` workflow (`.github/workflows/test.yml`) on every PR
and after each merge to main. Ubuntu runs Python 3.11, 3.14 and 3.15 without
sharding. macOS runs Python 3.14 and 3.15 in two test shards each. Version,
offline doctor and package checks run once per OS and Python version: in each
Ubuntu job and in shard 1 on macOS.
Ruff and mypy run in its `static` job. The required `Test result` check succeeds
only when that job and the test matrix succeed, and gates merges through the main
ruleset. The release check requires a green run on the release commit.
The coverage jobs report coverage and do not gate merges or releases.

To run these checks locally before pushing, optionally:

```sh
mise run ci [commit]    # defaults to HEAD
```

It checks the commit out into a temporary worktree with a fresh virtualenv,
installs the same development tool pins and runs `git diff --check` against
`origin/main`, Ruff, mypy, the unit suite and `ub-agents doctor --offline`.
It posts nothing to GitHub and gates nothing.

mise only reads a trusted `mise.toml`, and every fresh worktree is a new path, so an
agent runs `mise trust` in its own worktree before its first `mise` command. That is
expected and needs no approval.

CLI fixtures using `isolate_observations` or `isolate_runtime_state` keep real
startup-cleanup sweeps and join their threads during test cleanup, before removing
fixture directories. Other tests that start real sweeps use `join_startup_cleanup`.
Shutdown tests must release any held sweep before that join; launcher shutdown
itself still returns without joining stalled reads. A worker that outlives its
fixture fails the test, and directory cleanup errors remain visible.

## Remote loop shortcuts

The checkout includes a mise convenience task for hosts running the per-loop
`ub-agents@<loop>` systemd user units and the `ub-agents-tmux` wrapper. It uses
ordinary SSH, so your SSH config, agent and host verification apply. The host
must already have those services installed; the task does not provision them.

Create a gitignored `mise.local.toml` for your own host and welcome-card examples:

```toml
[env]
UB_AGENTS_REMOTE_HOST = "operator@your-loop-host"
UB_AGENTS_REMOTE_LOOPS = "backend frontend"
```

Then, from this checkout or a subdirectory:

```sh
mise run remote-loop backend          # attach; Ctrl-a d detaches
mise run remote-loop backend status   # service state, without a pager
mise run remote-loop backend logs     # follow the last 80 service-journal lines
mise run remote-loop backend drain    # finish the run and stay stopped
mise run remote-loop backend start    # start immediately
mise run remote-loop backend stop     # interrupt the run and release its claim
```

`mise remote-loop backend` is the interactive shorthand. An inactive service
is reported by SSH/systemd; attaching does not start it. Automatic restart
delays are configured on the server and are unaffected by these shortcuts.

With mise activated in the shell, entering this checkout installs the editable
package when needed and shows a welcome card of local and remote commands.
`mise run welcome` shows it again. The entry card makes no network calls and
stays quiet without a terminal, in CI, or with `UB_AGENTS_WELCOME=0` set.
`NO_COLOR` disables its colors. The remote section uses your local host and
loop names; without a host it shows the configuration hint instead.
