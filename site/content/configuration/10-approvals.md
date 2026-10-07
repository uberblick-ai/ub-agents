# Approvals

Decides whose input reaches the agents. On by default for public repositories, off for private and internal ones.

```yaml
approvals: on
```

## Off

Any trigger label starts work. The current title and body are input. Comments and reviews count only from people with write access or higher.

```yaml
approvals: off
```

## On

A maintainer starts every issue by adding a trigger label. Edits and comments from outside the team need a maintainer's approval before an agent reads them.

```sh
ub-agents approve 214
```

## Outside pull requests

A pull request from outside the team needs a maintainer's trigger label and an approved head commit: `ub-agents approve N` or an approving review. Agents can review fork pull requests but never push to them.

## When something needs approval

The launcher adds the stop label and posts one **Action needed** comment with the steps. Follow them and remove the label.

## Trusted bots

Bot accounts whose comments and reviews count as feedback. They get no other authority.

```yaml
trusted-bots: [copilot-pull-request-reviewer, Copilot]
```
