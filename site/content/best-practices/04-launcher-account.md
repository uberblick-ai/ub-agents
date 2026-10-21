# Give launchers their own account

Run the loop with a dedicated GitHub account for your agents, with write access to this repository only.

## Why not admin

Write access is enough to push branches, open pull requests and set labels. With [approvals on](/docs/configuration/approvals.html#on), outside work waits for a maintainer, and an agent account with maintain or admin counts as one, so it could approve that work itself. [`ub-agents doctor`](/docs/commands/doctor.html) warns about it.

```sh
ub-agents doctor
```

## Limit what agents can reach

Agents run with the launcher account and inherit the launcher's environment. Keep secrets you don't want them to use out of that environment, and protect the default branch with a ruleset: required checks, no force pushes.

## Several loops

Several loops can share one agent account, up to ten, or about seven when runs are short. See [How many launchers one account supports](/docs/best-practices/account-capacity.html).
