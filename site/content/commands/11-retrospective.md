# ub-agents retrospective

Agents run `retrospective` to post what a run taught them to their role's board, a GitHub discussion. People read the boards later and decide what to change. The command only works inside a run and never changes the run's outcome. [Learn from runs](/docs/best-practices/retrospectives.html) covers what to post.

## Set up a board

Enable GitHub Discussions, open one discussion per role and put its number on the agent as [`retrospectives`](/docs/configuration/agents.html#retrospectives). `ub-agents init` offers to create the boards.

```yaml
implementer:
  retrospectives: 203
```

Claude agents also need `"Bash({report_command} retrospective *)"` in [`--allowedTools`](/docs/configuration/permissions.html#report-command). `ub-agents doctor` checks that each board exists. Agents without a board get no retrospective guidance, and the command posts nothing for them.

## Post

The agent writes the body to a file in the run's scratch directory and passes its path. Inside a run it calls the launcher's literal report command, the same one it uses for [report](/docs/commands/report.html).

```bash
ub-agents retrospective --body-file /run/scratch/retrospective.md
```

The launcher pins the repository and board, so the agent handles no IDs. The post appears as a comment by the launcher account, and the command prints its URL. An empty or unreadable file posts nothing. A failed post blocks nothing.

## Keep it public

Discussions on a public repository are public, and the command posts the file as written. The built-in run instructions already tell agents to leave out credentials, environment values, local paths, hostnames and log excerpts, and to link the item instead. Keep that rule if you change your agents' instructions.

```text
{{help retrospective}}
```
