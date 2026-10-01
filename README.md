# ub-agents

ub-agents is a naive, opinionated framework for running agents in engineering loops. It uses GitHub as the source of truth and lets each project define the agents, triggers, and workflow steps it needs, with basic recovery and retry mechanisms.

Install it on a machine, configure your project, and launch it. ub-agents watches GitHub state and automatically runs the appropriate agent when its trigger matches.

> This README describes the target design. Implementation is still ahead; commands and configuration below are proposed interfaces.

## Get going

1. Install ub-agents on a machine where your agents will run, such as a remote Mac or Linux host.
2. Configure the available agents, their runtimes, and their GitHub triggers in a YAML file.
3. Launch the loop. Agents pick up work, record their outcomes on GitHub, and move it to the next configured step.

```sh
brew install uberblick-ai/tap/ub-agents

cd your-project
ub-agent init
ub-agent launch
```

`ub-agent init` writes a starter configuration and role instructions for you to customize. Install and authenticate `gh` and the agent CLIs you want to use on the same machine.

The loop runs in the foreground. Use your normal terminal session manager to keep it running on a remote host. Ctrl-C stops the loop and its active agents.

## Configure the agents

Which workflow steps exist and which LLM runs each step are project configuration. ub-agents comes with a starter workflow for preparing issues, implementing changes, reviewing PRs, and integrating accepted work. You are encouraged to change it.

For example, `ub-agent.yaml` might contain:

```yaml
repository: your-org/your-project

agents:
  issue-preparer:
    runtime: "claude:opus-5.5:high"
    trigger: needs-preparation
    instructions: .agents/issue-preparer.md

  implementer:
    runtime: ["claude:opus-5.5:high", "codex:sol-6.1:high"]
    trigger: [ready, needs-changes]
    instructions: .agents/implementer.md

  reviewer:
    runtime: ["claude:opus-5.5:high", "codex:sol-6.1:high"]
    trigger: needs-review
    different-runtime-from: implementer
    instructions: .agents/reviewer.md

  integrator:
    runtime: "claude:opus-5.5:high"
    trigger: ready-to-merge
    instructions: .agents/integrator.md

limits:
  max-attempts: 5
  agent-timeout-minutes: 30
```

A trigger names a GitHub label on an issue or PR. A runtime names the agent CLI, model, and effort setting. The runtime identifiers above are illustrative; use identifiers supported by your installed tools.

A runtime list declares the alternatives available for that step; it does not request a separate run on every model. In this example, `different-runtime-from: implementer` requires a different CLI/provider and model from the one recorded as the implementer of the candidate. Effort settings do not count as a different runtime: changing `high` to `low` cannot satisfy the rule. If Claude implemented, Codex reviews; if Codex implemented, Claude reviews. If none is eligible, the loop reports the blockage rather than ignoring the restriction. The implementation author also cannot act as its own independent reviewer.

The instruction files explain what each agent should do, what constitutes a valid outcome, and which GitHub state to leave behind. The starter instructions establish those conventions; your project can replace them.

You can use only Codex, configure another agent CLI such as a Grok-based tool, mix providers, or remove review altogether. The framework does not require these four roles or this sequence.

## What belongs in your project repository?

Check in `ub-agent.yaml` and the instructions it references in the project the agents work on. They define how that project is developed and should be reviewed and versioned alongside its code.

```text
your-project/
├── ub-agent.yaml
├── AGENTS.md                 # optional shared repository guidance
├── .agents/
│   ├── issue-preparer.md
│   ├── implementer.md
│   ├── reviewer.md
│   └── integrator.md
└── ... your project code
```

Commit the agent definitions, runtime/model choices, triggers, limits, role instructions, and any project-specific scripts or non-secret runtime settings they need. Instructions should identify the project's checks, handoff conventions, and merge policy. Keep shared guidance in one place and reference it from individual roles.

Keep credentials, authentication tokens, local logs, PIDs, scratch files, and temporary worktrees out of Git. Machine-specific secrets come from the operator's environment or the tools' normal authentication stores. GitHub carries leases, attempts, and work outcomes; those do not belong in a checked-in local state file.

A fresh clone plus the installed CLI and authenticated runtimes should be enough to launch the project's configured agents. `ub-agent init` copies starter files into the project; once committed, they are project-owned. Upgrading the executable does not silently replace them.

The separate `ub-agents` repository owns the framework, CLI adapters, packaging, and starter templates. Consuming projects own their configuration and instructions. They do not copy the framework's implementation into their repositories.

## The loop

```text
Observe GitHub
    ↓
Match a configured trigger
    ↓
Claim the work and select an eligible runtime
    ↓
Run the agent with the work item and its instructions
    ↓
Record the outcome on GitHub
    ↓
Observe GitHub again
```

The orchestration is deliberately mechanical. Matching labels, selecting eligible runtimes, launching processes, and enforcing limits are framework responsibilities. Understanding the task and deciding whether it meets the project's requirements are agent responsibilities, guided by project instructions.

In the starter workflow, preparation leaves an issue `ready`. Implementation creates a PR marked `needs-review`. Review marks the PR `needs-changes` for another implementation pass, or leaves it `ready-to-merge`. Integration runs the project's final checks and completes the work according to its merge policy.

Those labels and transitions are conventions of the starter workflow. They are not built-in role names or a mandatory development process. A different project might have just one agent that investigates labelled issues and posts findings.

## GitHub handoffs and leases

Issues are the main entry point for work. Most later handoffs happen on the implementation PR, which links back to its issue.

The starter labels are:

| Location | Label | Next action |
|---|---|---|
| Issue | `needs-preparation` | Prepare the requirements. |
| Issue | `ready` | Implement an unclaimed, eligible issue. |
| PR | `needs-review` | Review the candidate commit. |
| PR | `needs-changes` | Revise the existing implementation. |
| PR | `ready-to-merge` | Run integration checks and apply the project's merge policy. |
| Either | `needs-human` | Park automation pending a human decision. |

Labels describe the next action. Leases record who owns the current assignment. The implementer claims the issue before starting new work; subsequent review, revision, and integration assignments are claimed on the PR.

A lease is a GitHub record identifying the agent, run, runtime/model, expiry, and branch or PR where applicable. A live agent renews the same record, then releases or completes it at handoff. Expired leases can be recovered under the configured rules. An `in-progress` label is not required to establish ownership.

Review assignments and verdicts name the PR head SHA. A result for an older head cannot silently satisfy review of a newer candidate. The issue closes when the project's completion policy is satisfied.

These labels and handoff conventions ship with the starter workflow and remain customizable.

## Recovery and retries

Claims and outcomes live on GitHub so a restart can reconstruct the work. Start the loop again with:

```sh
ub-agent launch
```

Active claims prevent duplicate pickup. Interrupted assignments become eligible for recovery under the configured claim rules. Recovery starts a fresh session from the issue, PR, and recorded handoffs.

Failures are retried within configured limits. Authentication failures, runtime refusals, and exhausted attempts stop the affected work and report what needs attention. An unreadable GitHub response is a failure, not an empty queue. Attempt counts survive restarts through durable GitHub records.

Each agent has a deadline. Code-changing sessions can use private worktrees, and the launcher cleans up processes it owns when they finish or are interrupted. Runtime permissions remain explicitly configured by the operator.

## Standalone

ub-agents is a separate Python project exposing the `ub-agent` command. It has no dependency on Uberblick, its workspace services, or its application CLI. Uberblick is simply one consuming project with its own checked-in configuration and instructions.

The migration target is to remove `ub launch` and the agent-launching implementation from Uberblick. Agent execution belongs to `ub-agent launch`, with no permanent wrapper or duplicate launcher in Uberblick. Cutover happens once ub-agents can run the existing workflow; this README does not claim that removal has already shipped.

GitHub holds durable coordination state. Local logs are for diagnosis. There is no separate workflow database, required daemon, or web dashboard.

## License

MIT.
