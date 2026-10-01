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
| `queue` | Priority ranking, dependency waits and an optional milestone gate (defaults to FIFO, waiting for blockers, with milestones ignored). |

## Queue

```yaml
queue:
  priority:
    labels: [priority:urgent, priority:high, priority:normal, priority:low]
    default: priority:normal
  milestones: gate
  dependencies: wait
```

`priority.labels` is a nonempty list of unique, nonempty label names, highest first.
An item with several configured labels takes the highest. An item with none takes
`priority.default`, which must be one of the configured labels. If `default` is
omitted, unlabeled items rank below every configured label. Without `priority`, all
items have equal priority. Unknown keys in `queue` or `priority` are errors.
The launcher reads priority labels and never changes them.

A PR's effective priority is the highest of its own configured priority and the
effective priority of the open local issues it closes. Closing keywords in its
body (such as `Closes #21`, `Fixes: owner/repo#21` or `Resolves` followed by a local
issue URL) identify those issues; each reference requires a keyword. Closed issues,
PR references and references to other repositories do not contribute. This PR
rule applies in both dependency modes: `ignore` disables issue dependency
inheritance, but PRs still inherit their closing issues' own configured priority.

`milestones` accepts only `gate` or `ignore` and defaults to `ignore`. In `gate`
mode, new issues wait for the oldest open milestone with open issues or PRs to
close or empty; PR work and recovery remain eligible.
In `ignore` mode, planning and claiming do not read milestones. This repository
explicitly sets `gate`.

`dependencies` accepts only `wait` or `ignore` and defaults to `wait`, including
without a `queue` block. In `wait` mode, an issue cannot start preparation or
implementation while any GitHub blocked-by issue is open, including blockers
in other repositories. Closed blockers do not gate it. The gate is rechecked
before claiming. An open local issue inherits the highest effective priority
of its open local dependents, directly or transitively, without changing labels.
Cycles terminate and share the highest reachable priority. With `ignore`, links
affect neither eligibility nor priority, and dependency reads are skipped.
PR work, recovery and completion of started runs remain ungated. With milestone gating, a new issue must pass both gates.
A failed or unreadable dependency read stops selection visibly.
Planning skips link reads only when the issue list's dependency summary reliably
reports zero total blockers; missing or malformed summaries require a full read.
The claim-time recheck always reads the selected new issue's blocker links.

`ub-agent init` writes `queue: {milestones: ignore, dependencies: wait}`. Without a
`queue` block, priorities are unconfigured, milestones are ignored and dependency
waits apply.

Within each work class, priority is followed by item creation time and then item
number. See [selection order](coordination.md#selection-order) for eligibility and
PR precedence. `ub-agent status` and `status --json` use the same rank order and
show each item's effective priority (`none` in text, `null` in JSON when no label
or default applies). Waiting issues name the active milestone number and open
blockers, using `owner/repo#N` for external blockers. Inherited priority names its
origin, for example `priority:urgent (inherited from #21)`. JSON includes
`priority_inherited_from` (the source issue number, or `null`) and `open_blockers`
(a list of issue references). PRs name the issue they close, for example
`priority:urgent (from closed issue #21)`; that wording identifies a closing
reference to an issue that is still open. JSON records it as `priority_from_issue`
(the issue number, or `null`). An item's own priority wins ties; among equally
urgent inherited sources the lowest issue number is shown.

## Agents

Each agent has exactly one of `runtime` or `command`.

| Key | Meaning |
|---|---|
| `trigger` | Label, or list of labels, that starts the agent. |
| `outcomes` | Named successful outcomes and their project-defined label transitions. Optional for legacy agents. |
| `kind` | `issue`, `pr` or `either` (default). |
| `runtime` | `cli:model:effort`, or a list of alternatives tried in order. |
| `instructions` | The agent's task file. Required with `runtime`. |
| `command` | An argv list to run instead of an LLM session. |
| `different-runtime-from` | Another agent's name. This agent must run on a different CLI, provider and model from the one that produced the PR's current commit; a different effort doesn't count. |
| `worktree` | `true` runs in a private checkout: the PR's exact commit, or a fresh branch for an issue. |
| `cwd` | Directory to run in, relative to the repository root. |
| `runtime-args` | Extra arguments for the runtime CLI, such as permission flags. |
| Limit keys | Override `limits` for this agent. |

## Outcomes and transitions

```yaml
agents:
  implementer:
    runtime: "codex:gpt-6.1-sol:high"
    trigger: [ready, needs-changes]
    instructions: .agents/implementer.md
    outcomes:
      handed-off: {add: [needs-review], remove: [old-workflow-state]}
  integrator:
    runtime: "claude:opus:high"
    trigger: ready-to-merge
    instructions: .agents/integrator.md
    outcomes:
      merged: {}
      maintainer-merge: {add: [needs-human]}
```

`outcomes` must be a nonempty mapping of names to transitions. A transition accepts
only `add` and `remove`, each a list of nonempty string labels (empty lists are
allowed). Omitted lists are empty. `check` rejects unknown keys, non-string labels,
adding the agent's own trigger, and removing a stop label, including through an
agent's trigger. Adding a stop label is allowed as a human gate.

An agent with outcomes reports `ub-agent report --outcome NAME --summary TEXT
[--handoff PR]`. This reports success; an unknown name or `--status success` is
rejected. The prompt lists the declarations and directs the agent to leave workflow
labels alone. Direct commands follow the same contract. The running lease snapshots
the declarations; candidate configuration edits do not change the current run.

After ownership, candidate SHA and issue-link validation, the runner removes all
of this agent's trigger labels and any `remove` labels from the assignment. It adds
`add` labels to the named handoff PR, or to the assignment if none is named. Labels
outside those lists stay unchanged. When the destination is the assignment and a
label appears in both lists, `add` defines its final state. `--status retry|blocked`,
execution failure, execution timeout, execution interruption and invalid success
reports cause no transition.

Before starting, the runner rereads the assignment: a vanished trigger blocks the
transition. A stop label on either the assignment or handoff PR pauses it with no
label changes. The outcome remains unaccepted and is never applied later. After
unpausing, a person sets the desired workflow labels or uses `ub-agent retry` to
rerun the role. A stop label added after transition start stays in place while the
recorded transition completes, parking the item for subsequent pickup.

The outcome stores its name, resolved changes and start marker. Recovery completes
only missing changes from that record, even after configuration changes, without
rerunning the role or spending an attempt. Started transitions have already passed
success validation, so recovery does not recheck the candidate SHA, issue link,
trigger or pause state. It finishes the recorded changes even if the PR head moves,
preserving provenance for the original SHA. Assignment removals precede destination
additions; incomplete transitions create no cross-item reservations. A crash between
those steps leaves items idle when no other trigger is present. An operator reset
supersedes the old recovery; inspect both items and restore the desired triggers to
resume. See [recovery](coordination.md#recovery) for the operator path.

Without `outcomes`, the agent retains the legacy contract: change labels itself,
report `--status success`, and let the runner validate trigger consumption (or
closure). A valid issue-to-PR handoff retains the legacy exception permitting the
issue trigger to remain. No runner label transition is applied.

## Limits

| Key | Default | Meaning |
|---|---|---|
| `agent-timeout-minutes` | 180 | Deadline for one run. A claim's lease lasts this long plus fifteen minutes for setup and completion; the launcher never renews it. |
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

`init` includes the matching example, commented out, for every starter agent using
the CLI selected by `--runtime`. Uncomment or customize it before unattended work.
The Codex example grants full access, including writes to Git metadata for commits
and commands for pushing. The Claude example grants file edits and the listed
Git, GitHub and report commands without permission prompts. Add permissions for
your project's check commands as needed. `doctor` warns, without failing, for each
runtime agent with no `runtime-args`; it does not test whether supplied arguments
grant sufficient permissions.

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
| `UB_AGENT_CANDIDATE_SHA` | The PR's head commit; empty for issue work |
| `UB_AGENT_BRANCH` | The branch to work on, when known |
| `UB_AGENT_OPERATORS` | Trusted accounts, for `report` |

## Commands

- `ub-agent init [--repository owner/name] [--runtime cli:model:effort]` writes the
  starter `ub-agent.yaml`, shared `AGENTS.md` and `.agents/` files next to the selected
  `--config` file, and adds `.ub-agent/` to `.gitignore`. Fill in the shared guidance's
  project-check placeholders. An existing `AGENTS.md` is kept unchanged and reported;
  any existing configuration or role starter file stops init before any file writes.
  In an interactive terminal, init reads repository labels and explains each missing
  trigger, outcome `add`/`remove`, and stop label. It creates only those labels after
  an explicit `y` or `yes`; the default is no. Declining makes no GitHub writes and
  prints one runnable `gh label create` command per missing label. Without a terminal
  (including CI and piped input), or when labels cannot be read, it prints commands
  for all configured workflow labels and explains why. Noninteractive init makes no
  GitHub reads or writes beyond repository inference when `--repository` is omitted.
  It never changes or deletes existing labels or uses `--force`. Local starter files
  are written regardless of the label-creation answer. Each agent includes commented
  permission arguments matching `--runtime`; see [Runtime permissions](#runtime-permissions).
- `ub-agent check` validates the configuration and instruction files.
- `ub-agent doctor [--json]` checks everything `check` does, plus Python, the platform,
  `git`, `gh`, GitHub access, configured workflow labels, runtimes and local state.
  Missing trigger or outcome transition labels are required failures naming their
  agents; missing stop labels are warnings. Both give a `gh label create` remedy.
  Label matching is case-insensitive and an unreadable label list is a required
  failure. Runtime agents without `runtime-args` produce a warning linking the
  permission guidance. Doctor makes no writes. It exits 1 when a required
  check fails; warnings and skips exit 0. The JSON has `version`, `ok` and `checks`,
  and each check has `id`, `status`, `required`, `agent`, `runtime`, `message` and
  `remedy`.
- `ub-agent launch [--once]` runs the loop in the foreground.
- `ub-agent status [--json]` shows matching work, claims, attempts and outcomes.
- `ub-agent report --outcome NAME --summary TEXT [--handoff PR]` records a declared
  successful outcome. Use `--status retry|blocked` for failures; `--status success`
  is only for agents without outcomes. It works only inside a supervised run.
- `ub-agent retry --number N --agent NAME --reason TEXT` resets one agent's attempts on
  an item once you have fixed the cause.
- `--config PATH` selects a different configuration file.
