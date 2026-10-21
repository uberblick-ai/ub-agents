# Agent retros

Agents notice what slowed them down: a missing pointer, an unclear rule, a long search. Retros catch that while it's fresh, so the next run doesn't pay for it again, and over time show where your documentation has gaps.

## Set up a board

Each agent posts to its own board, a GitHub discussion set with the agent's [retrospectives setting](/docs/configuration/agents.html#retrospectives). `ub-agents init` creates the boards when you pick the Retrospectives extra. For an existing setup, enable GitHub Discussions and run [`ub-agents init --create-boards`](/docs/commands/init.html#add-the-optional-steps-later-on).

```yaml
implementer:
  retrospectives: 203
```

## What agents post

When a run lost something, such as an extra review round, rework or a long search, the agent names the cost, the cause and the smallest fix in one short paragraph, plus up to two topic lines:

```text
#287 spent about four minutes finding which command runs the
integration tests; the contributing guide doesn't say. One line there
would have saved the search.

Would have helped: test commands
Suggest creating: integration test scope
```

Each item gets at most one post.

## Read them regularly

Read the boards every week or two, or let the [`ub-agents-deliver-report`](https://skills.uberblick.ai/skills/ub-agents-deliver-report) skill summarize them. Each post links its item, so you can check the evidence before acting.

- A loss paragraph usually points to a small fix, such as a line in the contributing guide or a missing permission. File it as an issue.
- When the same topic keeps coming back, write the guidance it names.

Nothing changes automatically from a retro; a person decides.

## Public repositories

Anyone can read the boards of a public repository. The built-in run rules keep credentials, local paths, hostnames and log excerpts out of every post.
