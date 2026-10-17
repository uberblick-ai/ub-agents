# Loop internal commands

Agents run these commands themselves during a run. The built-in run instructions tell them when and how, so you rarely need them. If you rewrite an agent's instructions, keep these steps.

## report

Every run ends with `report`. It posts the result on the issue or pull request and moves its labels. When an agent needs a person, the report says what to decide, and the item waits with an [Action needed](/docs/guides/stuck-items.html#needs-a-person) comment. A run that ends without a report counts as a failed attempt.

```text
{{help report}}
```

## read

Agents use `read` to look at another issue or pull request. It shows only what they would see in their own assignment, so text that [approvals](/docs/configuration/approvals.html) don't trust stays out. Run `ub-agents read 143` yourself to see #143 the way an agent sees it.

```text
{{help read}}
```

## retrospective

Agents post what slowed them down to their [retrospective board](/docs/best-practices/retrospectives.html). A failed post never blocks the run.

```text
{{help retrospective}}
```
