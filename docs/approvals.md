# Issue and PR starts and outside input

Issue work needs a maintainer's start. Once started, the pipeline can move its own
labels and accept trusted edits without further human involvement. Outside content
needs a maintainer's review before it becomes input.

Every issue agent, including preparation, and every PR agent passes the approval
check at pickup and again after claiming. There is no setting to disable it.
`launch` and `status` show disallowed input as parked with the reason: missing
maintainer start, outside changes or unreadable approval history. Pickup spends no
attempt and makes no claim. When a failed approval is the only obstacle on a
triggered, open item, `launch` adds the configured stop label (`needs-human` in the
starter) and posts one **Action needed** notice with the reason and resume steps:

| Gate | Maintainer action to resume |
|---|---|
| No maintainer start | Remove `needs-human` and re-apply a trigger label. Approval alone does not start work. |
| Outside title/body edit or outside PR feedback after approval | Re-apply a trigger label, or run `ub-agent approve --number N`; then remove `needs-human`. |
| Outside PR head not approved | Run `ub-agent approve --number N` or submit an approving review of the current head; then remove `needs-human`. Re-applying a trigger does not approve a head. |

Use the project's configured stop and trigger labels when they differ from the
starter. Each unresolved gate gets the label and notice at most once; repeated
polls add nothing. A new gate after resuming parks the item again. When the item
is next claimed, the launcher minimizes the notice. Label and notice writes are
advisory: failures are logged without coordination records, claims or attempt
changes. Uncleared outside comments on issues and trusted-authored PRs only lose
input clearance and do not park work. Unreadable approval history and input that
changes during the read are retried on the next poll without parking writes.
`ub-agent status` remains read-only.

A failed post-claim check withdraws the
claim before execution, spends no attempt and needs no `ub-agent retry`; the item
becomes eligible when its input is approved. Existing live runs and durable-outcome
recovery finish under their original assignment.

The post-claim read supplies the agent's title, body and filtered comments; PR
context also includes the assigned head, reviews and review comments. Trusted and
maintainer feedback and cleared outside feedback are input. Uncleared or
later-edited outside feedback, coordination records, launcher notices and approval records are
excluded. The launcher prompt directs agents to use this context as assignment
input; other GitHub comments are not input, even when project instructions ask
agents to read comments. Outside edits during execution do not stop that run or
change its input. Starter instruction changes are tracked in #40.

## Repository roles

Trust comes from `role_name` in GitHub's [collaborator permission API](https://docs.github.com/en/rest/collaborators/collaborators#get-repository-permissions-for-a-user):

| Role | Input and authority |
|---|---|
| `maintain`, `admin` | Maintainer: can start and approve issues and PRs; edits and comments are trusted. |
| `write` | Trusted: edits and comments are input without approval, including agent preparation rewrites. Cannot start or approve. |
| `triage`, `read`, `none`, unknown | Outside: edits need approval; feedback needs clearance. Outside PR feedback also suspends work. |

An unreadable permission or an unrecognized custom role counts as outside. Roles are
reread before claims and approval-parking writes. Unchanged discovery inputs,
including readable permissions, are cached per item between polls; repeated
accounts also share one read within an observation.
There is no approver list or launcher-account setting. Give every launcher the same
GitHub account with `write`. `doctor` warns when that account has `maintain` or `admin`,
because agents could start and approve their own work. An unreadable launcher role
also produces a warning.

## Start, suspension and comments

A start is a `labeled` timeline event whose actor is a maintainer and whose label is
one of the configured triggers of any `issue` or `either` agent. It normally starts
with `needs-preparation`. A maintainer applying `ready` also starts an issue if that
is an issue agent's trigger. PR-only triggers do not start issues. The event remains
a start after the label is removed or agents apply other workflow labels. A `write`
account, including the launcher account, cannot start work by applying labels.
An approval record alone does not replace the required start.

Each maintainer trigger label approves the issue as it stands at that time, including
outside comments already present and last updated before that event. After the latest
approval (a maintainer trigger label or valid approval record), an outside title rename
or body edit suspends work. A later maintainer trigger label or valid record lifts
that suspension. Trusted and maintainer edits do not suspend work.

Body revisions use GraphQL `userContentEdits` editor, `editedAt` and revision body
(`diff`); title renames use REST `renamed` timeline events. The initial body is intake,
covered by the start, rather than a later edit. The check fails closed on unreadable
or incomplete history. GitHub timestamps cannot order changes in the same second;
ambiguous records are ignored and an outside edit in the approval's second suspends
work. Reapprove in a later second.

If the latest body revision at posting shares its second with another revision,
the record cannot prove which body was current, even when posted later. Apply a
maintainer trigger label again, or create a later body revision before reapproving.

Outside comments are excluded unless cleared by a start or a valid record. Records
clear exactly their listed comment IDs whose body digests still match. A comment
created or edited after clearance is excluded again, even if its body later returns
to the approved text. Trusted and maintainer comments need no clearance. Comment
clearance is independent of suspension: a new outside comment does not suspend the
issue's title and body. Existing valid records can clear comments even while the
issue waits for a start or reapproval.

Deleted comments cannot be detected. Deleting a comment only removes input. Deleting
an approval comment removes its authority; a hidden body revision cannot prove the
content digest of a record posted during that revision.

## Pull requests

A PR authored by a trusted or maintainer account needs no maintainer start.
Outside comments, reviews and review comments require clearance before becoming
input, but do not suspend this PR. Commenting cannot stall trusted-authored PRs.

A PR authored by an outside account needs a maintainer's start: a trigger label
of a configured `pr` or `either` agent, normally `needs-review`. Issue-only triggers
do not start PRs. The start remains valid after agent label transitions. An
approval record or an approving review does not replace this required start.

An outside PR also needs an eligible current head. GitHub does not record push
times, so applying a trigger label cannot prove which head it covered. A head is
eligible when a valid PR approval record pins it, a maintainer's approving review
names it as `commit_id`, or an accepted successful agent outcome produced it as
`candidate_sha` from a run whose `assignment_sha` was eligible when assigned.
The outcome must belong to this PR and match its successfully released source
lease (including completed recovery), and the PR's head repository must be the
base repository. Only trusted accounts can push there. A report records the head
observed at reporting time and cannot distinguish an agent push from an outside
fork author's push during a run. Fork heads therefore always need an explicit
approval record or maintainer approving review. A missing head repository cannot
grant inherited eligibility. Chained agent revisions in the base repository inherit
eligibility; unaccepted, rejected or unrelated outcomes do not. Every other head
needs explicit maintainer approval, even when its push preceded the start.

Outside title or body edits, comments, reviews and review comments at or after the
latest maintainer approval suspend outside-authored PR work. The latest approval
is a maintainer trigger label, valid PR approval record or approving review.
Reapplying a trigger can lift input suspension but cannot approve an unknown head.
An approving review lifts suspension and approves its commit but does not clear
outside feedback. Editing its prose preserves that submission approval and does
not create a new approval of later outside input. Only starts and approval records clear feedback, with separate
ID and body-digest lists for comments, reviews and review comments. Editing cleared
feedback removes its clearance even if its text returns to the approved body.

Fork PRs may be reviewed on their approved head. Agents cannot revise fork PRs;
revision runs must report `blocked` instead of attempting a handoff through the
base repository's branch. Every changed fork head needs explicit maintainer approval,
including a head observed by an accepted successful run.

## Approving current input

Run from a project configured with `ub-agent.yaml`:

```sh
ub-agent approve --number 123
```

The command refuses without posting if the authenticated `gh` account is not a
maintainer. For issues it prints the current title, body and outside comments.
For PRs it also prints the head SHA, outside reviews and outside review comments.
It posts one approval comment and prints its URL. Running the command
expresses approval; there is no interactive confirmation. It refuses when the input
changes between display and the final read. Changes during posting are handled by
record validation. The command does not change labels.

The exact version 1 comment format is the marker, a blank line, one JSON fence and
a trailing newline:

````text
<!-- ub-agent:approval:v1 -->

```json
{
  "issue": 123,
  "content_sha256": "<64 lowercase hex characters>",
  "comments": [
    {"id": 456, "body_sha256": "<64 lowercase hex characters>"}
  ]
}
```
````

`content_sha256` hashes the UTF-8 bytes of the JSON array `[title, body]`, produced by
Python `json.dumps([title, body], ensure_ascii=False, separators=(",", ":"))`.
No whitespace is added between array elements; quotes, backslashes and control
characters are JSON escaped. Unicode is not normalized, and newlines, carriage
returns and trailing whitespace are preserved. A missing issue body is the empty
string. Comment `body_sha256` hashes the exact UTF-8 body bytes without JSON wrapping.
Comment IDs are positive GitHub REST integer IDs, sorted by ID when the command
writes the record. The marker versions this encoding; unsupported or malformed
records grant no authority.

A record is valid only when GitHub identifies its author as a maintainer, it has
not been edited since posting, its issue or PR number matches, and its digest matches the
title and body **at posting time**. Validation reconstructs that content from body
revisions and title renames; it does not compare with the latest content. A later
trusted rewrite therefore preserves the approval. A stale digest does not become
valid when someone later restores the old text. Forged records by `write` or outside
accounts grant no approval.

PR records use the same version 1 envelope with `pr` instead of `issue`, plus
`head_sha` (40 lowercase hexadecimal characters), `reviews` and `review_comments`.
Each feedback list uses the same `{id, body_sha256}` encoding; IDs are scoped to
that feedback type. An issue record cannot approve a PR or vice versa. A record
for an older head never makes a new head eligible. The record explicitly pins the
reviewed head; it does not rely on a push timestamp.

For callers, `ub_agents.approvals.check_issue(github, number, trigger_labels)` and
`check_pr(github, number, trigger_labels, actor)` return `allowed`, a `reason`,
`cleared_comment_ids` (outside comments only) and the filtered `snapshot`.
PR checks also return `cleared_review_ids` and `cleared_review_comment_ids`.
Supply the union of triggers for issue/either or pr/either agents, respectively,
not just the item's current labels. The PR `actor` is the authenticated launcher
account whose coordination records can supply agent ancestry. Checks perform
reads only and do not claim work or change workflow labels.
