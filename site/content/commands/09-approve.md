# ub-agents approve

Run `ub-agents approve` to read an item's current input and approve it for the agents. Needs maintain or admin access; changes no labels.

The command displays the title, body and outside feedback; for PRs it also shows
the head and reviews. Invocation itself expresses approval, without a second
prompt. Input changed during display is refused. The record pins the PR head and
cleared feedback; it does not replace a maintainer start. See
[approval rules](https://github.com/uberblick-ai/ub-agents/blob/main/docs/approvals.md).

```text
{{help approve}}
```
