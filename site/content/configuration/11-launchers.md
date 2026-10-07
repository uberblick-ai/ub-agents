# Launcher accounts

Launchers on different machines share one queue through GitHub. By default, any account with write, maintain or admin access can run one.

```yaml
launchers: [bot-a, alice]
```

## Default

Leave `launchers` out. Every account with write access or higher counts, so engineers can run launchers with their own `gh` logins.

## Narrow the list

List the accounts allowed to post claims and outcomes. Use the same list on every machine. Listed accounts still need write access.

```yaml
launchers: [ub-bot]
```

## Removing an account

Stop that account's launcher first. Removing it from the list, or lowering its role, ignores everything it posted, including open claims.

## Check

`ub-agents doctor` warns about listed accounts without write access and about a logged-in account that is not on the list.

```sh
ub-agents doctor
```
