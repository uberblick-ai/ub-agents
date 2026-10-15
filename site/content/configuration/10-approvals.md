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

A maintainer starts an issue by adding a trigger label. Re-applying a trigger label clears earlier edits and comments from outside the team for the agents.

Sub-issues can inherit their parent's start through an accepted
[`sub-issues` outcome](/docs/configuration/outcomes.html). The launcher labels open
native sub-issues in the same repository, authored by its lease account after the
claim. The parent's completed outcome must record that sub-issue and trigger,
match its lease declaration, and have a successful released source or recovery
lease with confirmed cleanup. Current launcher trust applies to the record author
and label actor. Pickup waits for a pending parent transition without parking the child.

The label event starts the sub-issue, including preparation. Later outside title
or body edits suspend work; later outside comments are withheld until a maintainer
re-applies a trigger. A launcher label without a matching parent record is still
"No maintainer start", and a later launcher reapplication cannot clear new outside
edits through an old record. With approvals off, any trigger still starts work.

Cold status and queue-view observation passes read content, body revisions and label/title events in GraphQL batches of up to 50 items whose plans need approval checks. Claiming passes read one reached item at a time and stop at the first claim. Incomplete histories, unsafe event mappings and title-edit gates fall back to the full per-item reads; an unusable batch can add one attempted call. Unchanged discovery inputs stay cached. Only launcher events for labels declared in configured `sub-issues` outcomes require fresh parent lookups; other launcher-applied triggers add no parent reads. In-run `read` pins that label set and the launcher allowlist. Fresh claim and approval-parking checks, `read`, role checks and comment reads keep their per-item paths.

## Outside pull requests

A pull request from outside the team needs a maintainer's trigger label and an approving review of its exact head commit. Accepted agent revisions from an eligible head inherit eligibility in the base repository. Agents can review fork pull requests but never push to them.

## When something needs approval

The launcher adds the stop label and posts one **Action needed** comment with the steps. Follow them and remove the label.

## Trusted bots

Bot accounts whose comments and reviews count as feedback. They get no other authority.

```yaml
trusted-bots: [copilot-pull-request-reviewer, Copilot]
```
