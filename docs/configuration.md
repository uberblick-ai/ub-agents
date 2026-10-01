# Configuration reference

`ub-agent.yaml` sits at the root of the repository the agents work on. Unknown keys are
errors; `ub-agent check` validates the file.

## Top level

| Key | Meaning |
|---|---|
| `repository` | GitHub `owner/name`. It must match the checkout's `origin`. |
| `agents` | The agents, by name. |
| `runtimes` | Adapters for agent CLIs other than `codex` and `claude`. |
| `limits` | Default clocks and retry limits for every agent. |
| `poll-seconds` | How often an idle loop checks GitHub (default 30). |
| `stop-labels` | Labels that park an item (default `[needs-human]`). |
| `operators` | Other GitHub accounts whose claims and outcomes this launcher trusts. Only the authenticated account is trusted by default. |

## Agents

Each agent has exactly one of `runtime` or `command`.

| Key | Meaning |
|---|---|
| `trigger` | Label, or list of labels, that starts the agent. |
| `kind` | `issue`, `pr` or `either` (default). |
| `runtime` | `cli:model:effort`, or a list of alternatives tried in order. |
| `instructions` | The agent's task file. Required with `runtime`. |
| `command` | An argv list to run instead of an LLM session. |
| `different-runtime-from` | Another agent's name. This agent must run on a different CLI, provider and model from the one that produced the PR's current commit; a different effort doesn't count. |
| `worktree` | `true` runs in a private checkout: the PR's exact commit, or a fresh branch for an issue. |
| `cwd` | Directory to run in, relative to the repository root. |
| `runtime-args` | Extra arguments for the runtime CLI, such as permission flags. |
| Limit keys | Override `limits` for this agent. |

## Limits

| Key | Default | Meaning |
|---|---|---|
| `lease-minutes` | 60 | How long a claim lasts without renewal. |
| `renewal-minutes` | 5 | How often the launcher renews a running claim; less than half the lease. |
| `agent-timeout-minutes` | 180 | Deadline for one run. |
| `max-attempts` | 5 | Runs per item and agent before the item stops. |
| `retry-backoff-seconds` | 60 | First retry delay; it doubles with each attempt. |
| `max-backoff-seconds` | 3600 | Longest retry delay. |

## Built-in runtimes

The prompt, made of the assignment context and the agent's instructions, arrives on
stdin:

- `codex:MODEL:EFFORT` runs `codex exec --model MODEL --config model_reasoning_effort="EFFORT"`.
- `claude:MODEL:EFFORT` runs `claude --print --model MODEL --effort EFFORT`.

`runtime-args` are appended. They cannot change the model, the effort, or start from
an earlier session.

### Runtime permissions

The launcher adds no permission flags, so the CLIs start in their default headless
modes and usually cannot edit files or push. Grant each role what it needs:

```yaml
runtime-args: [--sandbox, danger-full-access]    # codex
runtime-args: [--permission-mode, acceptEdits, --permission-prompts, none,
               --allowedTools, "Bash(git *)", "Bash(gh *)", "Bash(ub-agent *)"]  # claude
```

`runtime-args` apply to every alternative in a runtime list, so use CLI-specific flags
only on agents with a single runtime. Codex's `workspace-write` sandbox cannot commit in
private worktrees, whose Git metadata lives in the main checkout.

## Other agent CLIs

```yaml
runtimes:
  example:
    provider: example-provider
    command: [example-cli, --model, "{model}", --effort, "{effort}"]
    check: [example-cli, auth, status]   # optional; doctor runs it
```

An agent then uses `runtime: "example:your-model:high"`. Only `{model}` and `{effort}`
are substituted, and no shell is involved. Without `check`, doctor reports the
runtime's authentication as not checkable.

## Commands instead of agents

```yaml
agents:
  investigate:
    command: [./scripts/investigate-issue]
    kind: issue
    trigger: investigate
    agent-timeout-minutes: 20
```

The command records its result with `ub-agent report`. It receives these environment
variables, as do LLM runtimes:

| Variable | Value |
|---|---|
| `UB_AGENT_CONTEXT` | Path to a JSON file describing the assignment |
| `UB_AGENT_REPOSITORY` | `owner/name` |
| `UB_AGENT_ASSIGNMENT` | Issue or PR number |
| `UB_AGENT_RUN` | Run id |
| `UB_AGENT_LEASE_ID` | The claim's comment id |
| `UB_AGENT_CANDIDATE_SHA` | The PR's head commit; empty for issues |
| `UB_AGENT_BRANCH` | The branch to work on, when known |
| `UB_AGENT_OPERATORS` | Trusted accounts, for `report` |

## Commands

- `ub-agent init [--repository owner/name] [--runtime cli:model:effort]` writes the
  starter `ub-agent.yaml` and `.agents/` files and adds `.ub-agent/` to `.gitignore`.
  It stops without writing anything if a starter file already exists.
- `ub-agent check` validates the configuration and instruction files.
- `ub-agent doctor [--json]` checks everything `check` does, plus Python, the platform,
  `git`, `gh`, GitHub access, runtimes and local state. It exits 1 when a required
  check fails; warnings and skips exit 0. The JSON has `version`, `ok` and `checks`,
  and each check has `id`, `status`, `required`, `agent`, `runtime`, `message` and
  `remedy`.
- `ub-agent launch [--once]` runs the loop in the foreground.
- `ub-agent status [--json]` shows matching work, claims, attempts and outcomes.
- `ub-agent report --status success|retry|blocked --summary TEXT [--handoff PR]`
  records the current run's outcome. It works only inside a supervised run.
- `ub-agent retry --number N --agent NAME --reason TEXT` resets one agent's attempts on
  an item once you have fixed the cause.
- `--config PATH` selects a different configuration file.
