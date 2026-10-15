# Give launchers their own account

We recommend launching the loop with a dedicated GitHub account for your agents, with write access to this repository. It needs no access to other repositories and no higher role such as maintain or admin.

## Why not admin

Write access is enough to push branches, open pull requests and set labels. With [`approvals: on`](/docs/configuration/approvals.html#on), issues and pull requests from people outside your team wait until a maintainer approves them. An agent account with maintain or admin counts as a maintainer, so it could approve that outside work itself. [`ub-agents init`](/docs/commands/init.html) and [`ub-agents doctor`](/docs/commands/doctor.html) warn about it.

```sh
ub-agents doctor
```

## Several loops

Several loops can share one agent account. GitHub's API limits apply per account, but up to ten loops on one account work fine. See [How many launchers one account supports](/docs/best-practices/account-capacity.html) and [Launcher accounts](/docs/configuration/launchers.html).
