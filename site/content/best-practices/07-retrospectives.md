# Learn from runs

Let agents report losses and topics for later human review.

```yaml
implementer:
  retrospectives: 203
```

## A board per role

Enable GitHub Discussions and run `ub-agents init --create-boards` to add missing
boards to every role in an existing setup. It reuses matching discussions, adds
[`retrospectives`](/docs/configuration/agents.html#retrospectives) and updates
affected Claude [`--allowedTools`](/docs/configuration/permissions.html#claude-code)
lists. You can also set the discussion numbers manually; Claude agents then need
`"Bash({report_command} retrospective *)"` in `--allowedTools`. The agent gets a
command to post there, and `ub-agents doctor` checks that the board exists.

## When to post

A loss paragraph needs a real loss (an extra run or review round, rework, or about fifteen minutes on a denied command, a long search or a missing pointer), or something the run needed but missed, and a change that would have prevented it. State the cost, cause and smallest useful change in one short paragraph.

A post may instead consist of, or end with, at most two optional topic lines, even when the run lost nothing:

```text
Would have helped: topic A, topic B
Suggest creating: topic C, topic D
```

`Would have helped` names existing guidance, records or pointers the run lacked or had to reconstruct. `Suggest creating` names lasting choices no record covers, left for later human review instead of being recorded during the run, subject to project rules. Omit empty lines. Use short comma-separated topic names and reuse a name from an existing record or earlier post when it fits. Include no per-topic rationale, scores, identifiers or other structure; link the item, which holds the evidence.

No post is required on every run. Post at most once per item, without repeating an earlier run's post. Discussions on public repositories are public: never include credentials, environment values, local paths, hostnames or log excerpts. Write the body with a file-writing tool in the run's scratch directory and post before reporting; a failed post blocks nothing.

## Review topics over several weeks

A person groups topic lines over several weeks and reads the linked items for rationale before deciding which records to create. Repetition is evidence, not a creation threshold: one consequential compatibility or security choice can merit a record. Records are never created automatically; this route uses no scoring, analytics service or scheduler. Loss paragraphs can also point to fixes worth filing as issues.

## Adopt the route in project rules

A project's own rules decide when a run creates a record. They can defer optional new records to topic lines, so a missing proposed record alone does not block completion. Binding decisions, behavior guarantees and human-approval gates stay in force; this framework guidance does not override project rules.
