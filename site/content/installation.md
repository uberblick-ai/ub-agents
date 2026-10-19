# Installation

Install ub-agents with Homebrew, on macOS or Linux:

```sh
brew install uberblick-ai/tap/ub-agents
```

You also need `git`, `gh` logged in to an account with write access to the repository, and the agent CLI you want to run: `claude` or `codex`.

## Set up your project

Run [`ub-agents init`](/docs/commands/init.html) from the root of a clean checkout of your default branch. It asks a few questions, shows every change and opens a setup PR with the starter files: `ub-agents.yaml` with five roles and their instructions in `.agents/`.

Before you merge the setup PR, go through init's [next steps](/docs/commands/init.html#next-steps): permissions, the shared policy and your check commands.

## Launch

Merge the setup PR, pull your default branch and start the launcher:

```sh
ub-agents launch
```

Run it on your own machine or on an always-on server. If you also work in this checkout yourself, clone a separate one for the launcher. For servers and more machines, see [Add a launcher](/docs/guides/add-a-launcher.html).

## Start work

Label an issue `needs-preparation` to start the default workflow. The agents prepare the issue, implement it in a pull request, review and merge it, and stop with `needs-human` when they need you. [How the loop works](/docs/how-the-loop-works.html) explains each step. In a public repository, only someone with the maintain or admin role on the repository can start work; see [Approvals](/docs/configuration/approvals.html).
