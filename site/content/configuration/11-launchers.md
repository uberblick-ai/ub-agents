# Launcher accounts

Launchers on different machines share one queue through GitHub. By default, any account with write, maintain or admin access can run one, so engineers can use their own `gh` logins.

## Recommended: an agent account

Give the agents their own GitHub account, such as `acme-agent`, with **write** access to only the repositories it works in. Write lets it push branches, open pull requests and comment. It cannot start or approve its own work, bypass branch protection or change settings. Log in with it on every launcher machine and list it:

```yaml
launchers: [acme-agent]
```

People keep their own accounts for starting work, approving input and the merges the integrator leaves to them. See [Give launchers their own account](/docs/best-practices/launcher-account.html).

One account supports up to ten launchers under the discovery budget. Busy capacity
also depends on requests per finished run; for many short runs, put launchers beyond
about six on a second account. See
[How many launchers one account supports](/docs/best-practices/account-capacity.html).

## Narrow the list

`launchers` limits whose claims and outcomes count. Use the same list on every machine. Listed accounts still need write access.

```yaml
launchers: [acme-agent, alice]
```

## Removing an account

Stop that account's launcher first. Removing it from the list, or lowering its role, ignores everything it posted, including open claims.

## Check

`ub-agents doctor` warns about listed accounts without write access, a logged-in account that is not on the list, and a launcher account with maintain or admin.

```sh
ub-agents doctor
```
