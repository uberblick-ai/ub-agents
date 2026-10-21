# Loop internal commands

Agents run these commands themselves; the built-in run rules tell them when and how.

## report

Every run ends with `report`, which posts the result and moves the labels. When an agent needs a person, the item waits with an [Action needed](/docs/guides/stuck-items.html#needs-a-person) comment. A run that ends without a report counts as a failed attempt.

```text
{{help report}}
```

## read

Agents use `read` to look at another issue or pull request, filtered by [approvals](/docs/configuration/approvals.html) like their own assignment. Run `ub-agents read 143` to see #143 the way an agent sees it.

```text
{{help read}}
```

## retrospective

Agents post what slowed them down to their [retrospective board](/docs/best-practices/retrospectives.html). A failed post never blocks the run.

```text
{{help retrospective}}
```
