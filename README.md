# ub-agents

> Project status: this README describes the target design. The CLI is not implemented yet; commands, configuration syntax, and the workflow API are proposed interfaces.

Run project-defined agent workflows. Keep the work on GitHub.

ub-agents is a standalone, MIT-licensed Python tool with an `ub-agent` command. It runs the agents and commands your project configures, handles their process lifecycle, and makes execution easy to follow.

Your project decides which agents exist, what they do, and what happens next.

```text
Work item → project-defined workflow → configured agents or commands → recorded outcome
```

A workflow might use one Codex agent, several agents from the same provider, a Grok-based agent CLI, a mix of providers, or ordinary commands. Implementation and review are optional project roles. There is no required provider pairing or development lifecycle.

## Install

```sh
brew install uberblick-ai/tap/ub-agents
```

Or:

```sh
pipx install ub-agents
```

You need Python 3.11+, Git, and the GitHub CLI. Install and authenticate whichever agent tools your project uses through their normal setup. ub-agents reuses those tools; it does not manage provider accounts.

## Run a workflow

From a configured project:

```sh
ub-agent run delivery --issue 123
```

Or target an existing pull request:

```sh
ub-agent run delivery --pr 456
```

`delivery` is a name defined by this project. It could instead be `investigate`, `prepare`, `audit`, or something else.

The command runs in the foreground. You can watch its progress and stop it with Ctrl-C.

```text
Issue #123 · delivery
Step: build · agent: builder · started
Step: build · finished · PR #456
Step: checks · command · passed · 8ab31f2
Step: assess · agent: assessor · revision requested
Step: build · agent: builder · started
Step: checks · command · passed · d490c87
Step: assess · agent: assessor · accepted
Workflow · complete · PR #456
```

This output illustrates one project's workflow. A single-agent investigation could finish after recording its findings on the issue. Completion may mean a reviewed PR awaiting a human, a merged change when explicitly permitted, or another project-defined outcome.

## How it works

The system has three parts:

- **ub-agents** runs processes, resolves configuration, provides execution context, manages optional worktrees, enforces deadlines, and reports results.
- **Your workflow** selects work and defines ordering, transitions, retries, acceptance, and completion.
- **Your agent instructions** describe how each configured agent performs its assignment.

The workflow reads GitHub, asks ub-agents to execute an agent or command, and reads GitHub again. Decisions use explicit results and durable records; the framework does not interpret reviewer prose or decide whether code is good.

A successful process exit means execution completed. It does not automatically mean the work item is complete.

## Configure your project

Keep agent definitions and workflow policy with the project, or supply them from an explicit external configuration directory:

```text
.agents/
├── ub-agent.toml
├── delivery.py
└── roles/
    ├── build.md
    └── assess.md
```

For example, this project configures two roles using the same runtime:

```toml
[workflows.delivery]
file = ".agents/delivery.py"
max_attempts = 5
timeout_minutes = 60

[agents.builder]
runtime = "codex"
instructions = ".agents/roles/build.md"
isolation = "worktree"
timeout_minutes = 30

[agents.assessor]
runtime = "codex"
instructions = ".agents/roles/assess.md"
isolation = "worktree"
timeout_minutes = 20
```

The names, runtime choices, instructions, and limits are project configuration. Defining an agent does not add it to a workflow automatically.

Workflows are small Python files using the execution API. They can read GitHub through `gh`, invoke configured agents or argv commands, and branch on explicit results. There is no separate workflow language.

Built-in adapters handle Claude Code and Codex. Other CLIs, including a project's Grok-based tool, can use a configured argv command with instructions supplied through stdin. Authentication and permissions stay with the selected tool and project.

Each session pins its originating project, selected instructions, and execution directory. Later configuration changes do not redirect an active session. Configuration remains distinct from the durable work state on GitHub.

## GitHub is the source of truth

Issues describe work. Branches and commits carry implementations. Pull requests, comments, reviews, and checks carry outcomes and handoffs.

The workflow defines the records it needs. Agents communicate through those records rather than relying on private transcripts passed between sessions.

ub-agents keeps local execution logs for diagnosis. Those logs are not a second workflow database. GitHub should contain enough information for a fresh session to continue the work.

## Recover an interrupted run

Run the same command again:

```sh
ub-agent run delivery --issue 123
```

The workflow reads current GitHub state and starts fresh sessions for remaining work. It does not replay an old conversation or blindly repeat the last process.

Recovery rules belong to the project. A project that permits multiple workers must define claiming, liveness, and takeover rules. The framework does not promise exactly-once execution across machines.

## Optional implementation and review

One project may configure:

```text
Issue → builder → PR → checks → assessor → revision or acceptance
```

Another may configure:

```text
Issue → investigator → findings recorded → complete
```

Both use the same executable. Neither flow is built into the framework.

When a project requires independent review, its workflow starts a fresh reviewer session with requirements, acceptance criteria, the exact candidate commit, code, diff, and relevant evidence. The implementation author's reasoning transcript is not review input.

The review names the commit it applies to. The project decides which checks and reviews must repeat when that commit changes, who can disposition findings, and who may merge. A context reset does not make an author an independent reviewer.

## Isolation and permissions

An agent can run in a private Git worktree, while an ordinary command may run in the project's existing directory. Isolation is an execution choice, not an assumption attached to a role name.

A worktree isolates files. The runtime's sandbox and permissions determine what the process can access. Project and operator settings control those permissions; ub-agents does not silently expand access or copy credentials.

Every process has a deadline. On interruption or timeout, ub-agents stops the processes it owns. Cleanup never treats unrelated processes in a shared project directory as its own. If cleanup fails, it reports the failure and preserves the affected workspace for inspection.

## Run existing automation

Workflows can execute tests, builds, scripts, and project-owned dispatchers directly as argv commands.

If a command already coordinates its own agents, ub-agents runs that command directly. This lets you adopt the tool around existing automation without replacing all of it.

## Watch for work

For a workflow that supports queue selection:

```sh
ub-agent watch delivery
```

The foreground loop runs one assignment at a time and waits when the project workflow reports no eligible work. Ctrl-C stops the loop and its active execution.

An optional cheap, read-only probe can avoid starting an agent when there is clearly no work. Probes should be over-inclusive; the workflow makes the actual eligibility decision. Failed GitHub reads are reported as failures, not empty queues.

## Limits and failures

Runs stop on completion, configured limits, or a condition requiring operator action.

Process failures, timeouts, missing outcomes, authentication failures, and runtime refusals are reported separately. The workflow decides what can be retried. Disagreement does not create an unlimited loop.

If a workflow-wide attempt limit must survive restarts, the project records its authoritative count on GitHub. Restarting the CLI must not reset that limit silently.

## Standalone by design

ub-agents lives in its own repository and package. It needs no Uberblick installation, source checkout, workspace service, or `ub launch` command.

Uberblick is one consuming project. It can supply its own roles, workflow, and MCP context through configuration, just as other projects supply theirs.

The framework has no built-in development roles, model intelligence, workflow database, web dashboard, required daemon, or distributed scheduler.

## License

MIT.
