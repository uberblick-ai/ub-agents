# Learn from runs

Let agents tell you what slowed them down, then fix the cause once.

```yaml
implementer:
  retrospectives: 203
```

## A board per role

Enable GitHub Discussions, open one discussion per role and put its number on the agent as [`retrospectives`](/docs/configuration/agents.html#retrospectives). The agent gets a command to post there. Claude agents also need `"Bash({report_command} retrospective *)"` in [`--allowedTools`](/docs/configuration/permissions.html#claude-code), and `ub-agents doctor` checks that the board exists.

## Only real losses

Tell agents to post only when a run lost something, such as an extra review round or a denied command, and they can name the change that would have prevented it.

## Read them weekly

Group the posts, file issues for what repeats, then clear the board. Discussions on public repositories are public, so posts must not contain credentials, paths or log excerpts.
