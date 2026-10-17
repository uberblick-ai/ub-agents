# ub-agents report

Agents run `report` to end a run. It posts the result on the issue or pull request and moves its labels. When an agent needs a person, the report says what to decide, and the item waits with an [Action needed](/docs/guides/stuck-items.html#needs-a-person) comment.

You don't run it yourself: the built-in run instructions tell agents when and how. A run that ends without a report counts as a failed attempt, so keep the report step if you rewrite an agent's instructions.

```text
{{help report}}
```
