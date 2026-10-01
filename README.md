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
    trigger: ready
    instructions: .agents/implementer.md

  reviewer:
    runtime: ["claude:opus-5.5:high", "codex:sol-6.1:high"]
    trigger: wants-review
    disqualify-runtime-from: implementer
    instructions: .agents/reviewer.md

  integrator:
    runtime: "claude:opus-5.5:high"
    trigger: wants-to-be-merged
    instructions: .agents/integrator.md

limits:
  max-attempts: 5
  agent-timeout-minutes: 30
```

A trigger names a GitHub label on an issue or PR. A runtime names the agent CLI, model, and effort setting. The runtime identifiers above are illustrative; use identifiers supported by your installed tools.

A runtime list declares the alternatives available for that step; it does not request a separate run on every model. In this example, `disqualify-runtime-from` excludes the runtime recorded as the implementer of the candidate, so review uses another configured runtime. If none is eligible, the loop reports the blockage rather than ignoring the restriction. The implementation author also cannot act as its own independent reviewer.

The instruction files explain what each agent should do, what constitutes a valid outcome, and which GitHub state to leave behind. The starter instructions establish those conventions; your project can replace them.

You can use only Codex, configure another agent CLI such as a Grok-based tool, mix providers, or remove review altogether. The framework does not require these four roles or this sequence.

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

In the starter workflow, preparation leaves an issue `ready`. Implementation creates a PR marked `wants-review`. Review either requests a revision or leaves it `wants-to-be-merged`. Integration runs the project's final checks and completes the work according to its merge policy.

Those labels and transitions are conventions of the starter workflow. They are not built-in role names or a mandatory development process. A different project might have just one agent that investigates labelled issues and posts findings.

## Recovery and retries

Claims and outcomes live on GitHub so a restart can reconstruct the work. Start the loop again with:

```sh
ub-agent launch
```

Active claims prevent duplicate pickup. Interrupted assignments become eligible for recovery under the configured claim rules. Recovery starts a fresh session from the issue, PR, and recorded handoffs.

Failures are retried within configured limits. Authentication failures, runtime refusals, and exhausted attempts stop the affected work and report what needs attention. An unreadable GitHub response is a failure, not an empty queue. Attempt counts survive restarts through durable GitHub records.

Each agent has a deadline. Code-changing sessions can use private worktrees, and the launcher cleans up processes it owns when they finish or are interrupted. Runtime permissions remain explicitly configured by the operator.

## Standalone

ub-agents is a separate Python project exposing the `ub-agent` command. It has no dependency on Uberblick or `ub launch`. Uberblick is simply one project that can configure and use it.

GitHub holds durable coordination state. Local logs are for diagnosis. There is no separate workflow database, required daemon, or web dashboard.

## License

MIT.
