# ub-agents

ub-agents is a naive, opinionated framework for running agents in engineering loops. It uses GitHub as the source of truth and lets each project define the agents, triggers, and workflow steps it needs, with basic recovery and retry mechanisms.

Install it on a machine, configure your project, and launch it. ub-agents watches GitHub state and automatically runs the appropriate agent when its trigger matches.

> The first standalone vertical slice is implemented: YAML configuration, serial
> GitHub pickup, cooperative leases, supervised argv/CLI execution, explicit durable
> outcomes, release, and bounded recovery. Native adapters are thin and have invocation
> tests; real coding-session/platform validation and published distribution remain
> roadmap work. This is an early implementation, not a demonstrated Uberblick cutover.

## Get going

1. Install ub-agents from a checkout on a machine where your agents will run, such as a remote Mac or Linux host. Python 3.11+ is required; execution uses POSIX process supervision.
2. Configure the available agents, their runtimes, and their GitHub triggers in a YAML file.
3. Launch the loop. Agents pick up work, record their outcomes on GitHub, and move it to the next configured step.

```sh
# In a checkout of this standalone repository:
pipx install .

cd your-project
ub-agent init
ub-agent check
ub-agent launch
```

`ub-agent init` writes a starter configuration and role instructions for you to customize. It refuses to overwrite them and adds `.ub-agent/` to `.gitignore`. Use `--repository owner/name` if repository inference through `gh` is unavailable, and `--runtime cli:model:effort` to select another starter runtime. Install and authenticate `git`, `gh`, and the agent CLIs you want to use on the same machine. Create the project's configured GitHub labels and configure runtime permissions explicitly before launching.

Homebrew installation (`brew install uberblick-ai/tap/ub-agents`) and PyPI publication are planned in [#4](https://github.com/uberblick-ai/ub-agents/issues/4); they are not released yet. Prerequisite diagnostics through `ub-agent doctor` are planned in [#5](https://github.com/uberblick-ai/ub-agents/issues/5). `check` currently validates local YAML and instruction paths; it is not a complete machine preflight.

`ub-agent launch --once` observes once and executes at most one assignment. `ub-agent status` reads open matching work, ownership, attempt counts and reported outcomes; `--json` emits structured status. Use `ub-agent --config path/to/ub-agent.yaml ...` for an explicit configuration path. These commands do not require project-authored Python workflows.

The loop runs in the foreground. Use your normal terminal session manager to keep it running on a remote host. Ctrl-C stops the loop and its active agents.

## Configure the agents

Which workflow steps exist and which LLM runs each step are project configuration. ub-agents comes with a starter workflow for preparing issues, implementing changes, reviewing PRs, and integrating accepted work. You are encouraged to change it.

For example, `ub-agent.yaml` might contain:

```yaml
repository: your-org/your-project

agents:
  issue-preparer:
    runtime: "claude:opus:high"
    trigger: needs-preparation
    instructions: .agents/issue-preparer.md

  implementer:
    runtime: ["claude:opus:high", "codex:gpt-6.1-sol:high"]
    trigger: [ready, needs-changes]
    instructions: .agents/implementer.md

  reviewer:
    runtime: ["claude:opus:high", "codex:gpt-6.1-sol:high"]
    trigger: needs-review
    different-runtime-from: implementer
    instructions: .agents/reviewer.md

  integrator:
    runtime: "claude:opus:high"
    trigger: ready-to-merge
    instructions: .agents/integrator.md

limits:
  max-attempts: 5
  lease-minutes: 60
  renewal-minutes: 5
  agent-timeout-minutes: 180
  retry-backoff-seconds: 60
  max-backoff-seconds: 3600
```

A trigger names a GitHub label on an issue or PR. A list of triggers matches any listed label. A runtime names the agent CLI, model, and effort setting. Use concrete identifiers supported by your installed tools and account. The adapters pass `codex exec --model MODEL --config model_reasoning_effort='"EFFORT"'` or `claude --print --model MODEL --effort EFFORT`, with the project prompt on stdin. The starter defaults to one Codex runtime for all four steps; the mixed example above is optional. Invocation syntax was checked against installed CLI help and [Codex documentation](https://developers.openai.com/codex/cli/reference) / [Claude documentation](https://code.claude.com/docs/en/headless); model access still depends on your authentication.

A runtime list declares alternatives; the first eligible installed executable in declared order runs once. In this example, `different-runtime-from: implementer` requires a different CLI/provider and model from the one recorded as the implementer of the candidate. Effort settings do not count as a different runtime: changing `high` to `low` cannot satisfy the rule. If Claude implemented, Codex reviews; if Codex implemented, Claude reviews. If none is eligible, the loop reports the blockage rather than ignoring the restriction. Starting a fresh conversation on the author’s CLI/provider/model cannot satisfy independent review. Distinct runtime executions may share GitHub authentication; native approval eligibility is separate from model authorship. GitHub forbids a PR author from approving its own PR, so explicit outcomes do not depend exclusively on native approvals or bypass branch protection.

The instruction files explain what each agent should do, what constitutes a valid outcome, and which GitHub state to leave behind. The starter instructions establish those conventions; your project can replace them.

You can use only Codex, configure another agent CLI such as a Grok-based tool, mix providers, or remove review altogether. The framework does not require these four roles or this sequence.

Agent clocks override `limits` independently. `lease-minutes` describes ownership,
`renewal-minutes` is the launcher's heartbeat cadence, and `agent-timeout-minutes`
bounds runtime execution. The default timeout is three hours; choose a different
deadline for each step. Optional `kind: issue|pr|either` narrows where a trigger
matches. `cwd` is relative to the configuration's repository root, and `worktree:
true` requests a private checkout of the exact PR candidate (or a fresh issue branch).
`runtime-args` is an argv list for explicitly configured tool permissions/settings;
the framework never silently expands grants.

Coordination comments are trusted only from the authenticated GitHub account by
default. For machines using different accounts, explicitly list trusted peer logins
with `operators: [engineering-bot, review-bot]`. Public commenters cannot claim work,
reset budgets or supply candidate provenance. Malformed records from a trusted
operator block the affected item and remain visible in `status`.

Task instruction files are loaded from the operator's configuration checkout when
the launcher starts. A private PR candidate supplies the code to execute or inspect;
its edited instruction files cannot replace the configured task policy in that run.

Direct commands need no LLM session or instruction file:

```yaml
agents:
  investigate:
    command: [./scripts/investigate-issue]
    kind: issue
    trigger: investigate
    agent-timeout-minutes: 20
```

The command receives `UB_AGENT_CONTEXT` (a disposable JSON context path),
`UB_AGENT_REPOSITORY`, `UB_AGENT_ASSIGNMENT`, `UB_AGENT_RUN`, `UB_AGENT_LEASE_ID`,
`UB_AGENT_CANDIDATE_SHA`, and `UB_AGENT_BRANCH`. It performs project-authorized
GitHub transitions and calls `ub-agent report`. There is no shell interpolation.
`UB_AGENT_OPERATORS` carries the configured coordination trust scope into `report`.
Other authenticated CLIs can use a thin custom argv adapter:

```yaml
runtimes:
  example:
    provider: example-provider
    command: [example-cli, --model, "{model}", --effort, "{effort}"]
# Then an agent can use runtime: "example:your-model:high".
# Its instruction prompt arrives on stdin. Only model/effort placeholders expand.
```

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

Success handoffs to a draft PR are rejected with a blocked result, during both
normal completion and outcome-only recovery. The PR must be marked ready before
reporting a successful handoff; an unaccepted outcome supplies no review provenance.

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

`ub-agent report` records one explicit durable outcome from the supervised run:

```sh
ub-agent report --status success \
  --summary "Checks passed; candidate ready for review" --handoff 42
```

The comment leads with that summary and contains a versioned machine record in a
fenced `json` block. Stdout is JSON with `run`, `status`, and the comment `url`.
Statuses are `success`, `retry`, and `blocked`. Success is initially unaccepted:
the launcher verifies termination and resulting GitHub state before accepting it
and releasing ownership. Exit zero alone never establishes completion.

A completed issue-to-PR handoff suppresses duplicate initial work even if its
starting label remains. A PR returning to `needs-changes` runs a fresh assignment;
its existing item/agent attempt budget continues across head changes. An expired
run with an already-written outcome receives outcome-only recovery before any
command is reexecuted. Success without a PR handoff can run again when its trigger
is reapplied, using the existing attempt budget. Unavailable GitHub validation
reads leave the reported outcome pending for recovery after lease expiry; they
do not turn it into a failed acceptance check. Recovery does not consume command
attempts. Full record, assignment, race and recovery rules are in
[the coordination contract](docs/coordination.md).

## Recovery and retries

Claims and outcomes live on GitHub so a restart can reconstruct the work. Start the loop again with:

```sh
ub-agent launch
```

Unexpired claims exclude cooperative pickup; GitHub comments cannot guarantee exactly-once execution or strict write fencing. Interrupted assignments become eligible for recovery under the configured claim rules. Recovery starts a fresh session from the issue, PR, and recorded handoffs.

Explicit transient failures, timeouts, interruptions and missing outcomes after exit zero are retried within configured limits. Nonzero exits without an explicit retry outcome stop the affected work for operator attention, covering unclassified authentication failures and runtime refusals without interpreting prose. Exhausted attempts also stop the affected work. An unreadable GitHub response is a failure, not an empty queue. Attempt counts survive restarts through durable GitHub records.

Each agent has a deadline. Code-changing sessions can use private worktrees, and the launcher cleans up processes it owns when they finish or are interrupted. Runtime permissions remain explicitly configured by the operator.

Attempts count started runs per issue/PR and configured agent, including successful
PR revisions. Retries use durable exponential backoff. To retry stopped/exhausted
work after addressing its cause, explicitly record a reasoned reset:

```sh
ub-agent retry --number 42 --agent implementer --reason "Fixed the failing tool authentication"
```

This preserves history and refuses an unexpired claim. Uncertain process cleanup
stops the loop and preserves private artifacts. While ownership can still be
verified, the launcher durably marks cleanup as unconfirmed; expiry cannot promote
an earlier success report from that run. Released failures remain visible even
after their trigger is removed or the item closes. Restore an appropriate trigger
after inspecting the failure, and use a reasoned reset for blocked/exhausted work.
Recovery after machine/launcher
loss relies on expiry; expiry does not positively prove a surviving remote process
is dead. Cleanup covers the owned process group. Detached helpers that escape that group
are outside this first slice's attribution boundary; it does not perform cross-process
cwd sweeps. Broader host/runtime
evidence is tracked in [#2](https://github.com/uberblick-ai/ub-agents/issues/2).

Recovery discovery scans repository comments once at startup, then follows
updated/new comments with an overlapping `since` cursor. Each page has its own
20-second deadline. The in-memory index is disposable; ownership, attempts and
acceptance always reread the item's GitHub comments. Restarting rebuilds the index.

## Standalone

ub-agents is a separate Python project exposing the `ub-agent` command. It has no dependency on Uberblick, its workspace services, or its application CLI. Uberblick is simply one consuming project with its own checked-in configuration and instructions.

The migration target is to remove `ub launch` and the agent-launching implementation from Uberblick. Agent execution belongs to `ub-agent launch`, with no permanent wrapper or duplicate launcher in Uberblick. Cutover happens once ub-agents can run the existing workflow; this README does not claim that removal has already shipped.

GitHub holds durable coordination state. Local logs are for diagnosis. There is no separate workflow database, required daemon, or web dashboard.

## License

MIT.

## Development and next steps

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -v
```

Tests use recording fakes for GitHub coordination and real owned child processes
for supervision; they do not invoke paid models or production delivery loops.
On restricted hosts, process inspection (`ps`) must be available
to verify cleanup. Local validation currently covers macOS. CI exercises supported
Python versions on macOS and Linux; advertised runtime support still needs real
session evidence from the roadmap.

The [milestones](https://github.com/uberblick-ai/ub-agents/milestones) group the
first slice/doctor work, coding workflow pilot, and distribution. The remaining
work includes real adapter sessions and host failure evidence, a disposable
consuming-project pilot ([#3](https://github.com/uberblick-ai/ub-agents/issues/3)),
`doctor`, and published Homebrew/pipx distribution. Uberblick retirement remains
in its own repository after a demonstrated standalone cutover.
