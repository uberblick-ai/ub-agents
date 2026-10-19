# Approvals

Decides whose input reaches the agents. On by default for public repositories, off for private and internal ones.

Interactive [init](/docs/commands/init.html) shows the effective value and its source
on the "Who answers the agents' questions?" step. Without an explicit policy,
unreadable visibility leaves the value unresolved; run `ub-agents doctor` before launching.

```yaml
approvals: on
```

## Off

Any trigger label starts work. The current title and body are input. Comments and reviews count only from people with write access or higher.

```yaml
approvals: off
```

## On

A maintainer starts an issue by adding a trigger label. Re-applying a trigger label clears earlier edits and comments from outside the team for the agents.

Sub-issues can inherit their parent's start through an accepted [`sub-issues` outcome](/docs/configuration/outcomes.html). Later outside title or body edits suspend work; later outside comments wait for a maintainer to re-apply a trigger. See [inherited starts](https://github.com/uberblick-ai/ub-agents/blob/main/docs/approvals.md#start-suspension-and-comments) and [approval reads](https://github.com/uberblick-ai/ub-agents/blob/main/docs/approvals.md#approval-reads) for the detailed rules.

## Outside pull requests

A pull request from outside the team needs a maintainer's trigger label and an approving review of its exact head commit. Accepted agent revisions from an eligible head inherit eligibility in the base repository. Agents can review fork pull requests but never push to them.

## When something needs approval

The launcher adds the stop label and posts one **Action needed** comment with the steps. Follow them and remove the label.

## Trusted bots

Bot accounts whose comments and reviews count as feedback. They get no other authority.

```yaml
trusted-bots: [copilot-pull-request-reviewer, Copilot]
```
