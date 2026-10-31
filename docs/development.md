# Development checks

Run the commands in [AGENTS.md](../AGENTS.md#checks) in your checkout. They install
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

GitHub Actions runs both commands in the `static` job of the `Test` workflow.
The required `Test result` check succeeds only when that job and the test matrix
succeed. `mise run ci [commit]` installs the same pins and runs both commands in
a fresh worktree, along with the tests, offline doctor and whitespace check.
