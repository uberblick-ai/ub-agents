# Give launchers their own account

Run launchers under an account with write access, not maintain or admin. Agents can then push and label, but they cannot start or approve their own work.

## Why not admin

With approvals on, maintainers start work and approve outside input. A launcher with maintain or admin could do both for itself. `ub-agents doctor` warns about it.

```sh
ub-agents doctor
```

## People keep their roles

Maintainers use their own accounts to start work, approve input and merge what the integrator leaves to them.

## Several machines

Every launcher can use the same bot account, or each engineer their own. See [Launcher accounts](/docs/configuration/launchers.html).
