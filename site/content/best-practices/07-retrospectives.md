# Agent retros

Agents notice what slowed them down: a missing pointer, an unclear rule, a long search. Retros catch that while it's fresh, so the next run doesn't pay for it again. Over time they also show where your documentation has gaps or contradicts itself.

## Set up a board

Each agent posts to its own board, a GitHub discussion set with the agent's [retrospectives setting](/docs/configuration/agents.html#retrospectives). `ub-agents init` creates the boards when you pick the Retrospectives extra. For an existing setup, enable GitHub Discussions and run [`ub-agents init --create-boards`](/docs/commands/init.html#add-the-optional-steps-later-on).

```yaml
implementer:
  retrospectives: 203
```

## What agents post

When a run lost something, such as an extra review round, rework or a long search, or missed something it needed, the agent names the cost, the cause and the smallest change that would have prevented it in one short paragraph. A post can also hold, or be only, up to two topic lines. For example:

```text
#287 spent about four minutes finding which command runs the
integration tests; the contributing guide doesn't say. One line there
would have saved the search.

Would have helped: test commands
Suggest creating: integration test scope
```

Each item gets at most one post. You can adjust this in the agent's [role file](/docs/configuration/instructions.html#role-files).

## Read them regularly

Read the boards every week or two, or let the [`ub-agents-deliver-report`](https://skills.uberblick.ai/skills/ub-agents-deliver-report) skill summarize them. Each post links its item, so you can check the evidence before acting.

- A loss paragraph usually points to a small fix, such as a line in the contributing guide, a clearer role file or a missing permission. File it as an issue.
- Topic lines add up over time. When the same topic keeps coming back, write the guidance or record it names. One costly choice, such as a compatibility or security decision, can be enough on its own.

Nothing changes automatically from a retro. A person decides what to change.

## Public repositories

On a public repository anyone can read the boards. The built-in run instructions already keep credentials, environment values, local paths, hostnames and log excerpts out of every post. Keep that rule if you change your agents' instructions.
