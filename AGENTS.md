# Working on ub-agents

ub-agents develops itself with ub-agents. `ub-agents.yaml` and `.agents/` configure
the loop for this repository; agents run in private worktrees under `.ub-agents/worktrees/`.

## Checks

```sh
python3 -m venv .venv
.venv/bin/pip install -q -e . -r requirements-dev.txt
.venv/bin/ruff check src
.venv/bin/mypy
.venv/bin/python -m tests
.venv/bin/ub-agents doctor --offline
git diff --check
```

Run them in your own worktree while you work.

## Documentation homes

- `site/content/` holds operator documentation: commands, configuration, guides
  and FAQ. Keep each topic there once and link to it from contributor material.
- `docs/` holds contributor and maintainer material: coordination and other
  contracts, design documents, development and releases. Contracts link to the
  site for configuration keys and usage; site pages link back for exact rules.
- The Uberblick corpus holds why the product works this way: product reasoning
  and workflow context, rather than copies of the repository documentation.

Keep the site and `docs/` accurate for any behavior you change. The
[corpus rule](docs/development/uberblick-corpus.md#who-writes-the-corpus) says who
updates the corpus and when. `README.md` stays an overview and links to the
relevant home for details.

## Contributor guidance

- [Principles](docs/development/principles.md)
- [Conventions](docs/development/conventions.md)
- [Testing](docs/development/testing.md)
- [Development checks, CI and remote loop shortcuts](docs/development/checks.md)
- [Changelog and releases](docs/development/changelog-and-releases.md)
- [Uberblick corpus, including required reachability](docs/development/uberblick-corpus.md)
- [Review checklist](docs/development/review-checklist.md)
- [Loop policy](.agents/ub_agents.md) and [role configuration](ub-agents.yaml)
