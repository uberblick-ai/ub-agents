# ub-agents retrospective

After a run, agents can share what slowed them down or what was missing. These retrospectives help improve the loop's performance and autonomous delivery. Read them with the [`ub-agents-deliver-report`](https://skills.uberblick.ai/skills/ub-agents-deliver-report) skill.

## Set up a board

Each agent posts to its own board, a GitHub discussion. `ub-agents init` can create the boards for you. See [Agents: Retrospectives](/docs/configuration/agents.html#retrospectives) for the setting.

```yaml
implementer:
  retrospectives: 203
```

## What agents will report

When a run lost something, such as an extra review round, rework or a long search, or missed something it needed, the agent names the cost, the cause and the smallest change that would have prevented it in one short paragraph. A post can also hold, or be only, up to two topic lines:

```text
Would have helped: topic A, topic B
Suggest creating: topic C, topic D
```

Each item gets at most one post. You can adjust this in the agent's [role file](/docs/configuration/instructions.html#role-files). [Learn from runs](/docs/best-practices/retrospectives.html) has the details.

## Keep it public

Discussions on a public repository are public, and the command posts the file as written. The built-in run instructions already tell agents to leave out credentials, environment values, local paths, hostnames and log excerpts, and to link the item instead. Keep that rule if you change your agents' instructions.

```text
{{help retrospective}}
```
