# Installation

Install ub-agents with Homebrew, on macOS or Linux:

```sh
brew install uberblick-ai/tap/ub-agents
```

You also need `git`, `gh` logged in to an account with write access to the repository, and the agent CLI you want to run: `claude` or `codex`.

## Set up your project

From the root of a clean checkout of your default branch, run [init](/docs/commands/init.html):

```sh
ub-agents init
```

It asks a few questions, shows every change and opens a setup PR with the starter files: `ub-agents.yaml` with five roles and their instructions in `.agents/`. Before you merge it, go through init's [next steps](/docs/commands/init.html#next-steps).

## Launch

Merge the setup PR, pull your default branch and start the launcher:

```sh
ub-agents launch
```

If you also work in this checkout yourself, clone a separate one for the launcher. For servers and more machines, see [Add more launchers](/docs/guides/add-a-launcher.html).

## Start work

Label an issue `needs-preparation`. The agents prepare it, implement it in a pull request, review and merge it, and stop with `needs-human` when they need you. See [How the loop works](/docs/how-the-loop-works.html). In a public repository, only someone with the maintain or admin role can start work.

### A good first issue

Good first issues:

- "Fix the typo in the `--help` text of `export`" (one file, obvious when done).
- "Add a test for parse_date() with an empty string" (small, checkable by CI).

Wait with:

- "Refactor the storage layer" (several PRs, needs decisions).
- "Make onboarding better" (no clear way to tell when it is done).

For more guidance, see [Keep issues small](/docs/best-practices/small-issues.html).
