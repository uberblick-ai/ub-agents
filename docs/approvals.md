# Issue starts and outside input

Issue work needs a maintainer's start. Once started, the pipeline can move its own
labels and accept trusted edits without further human involvement. Outside content
needs a maintainer's review before it becomes input.

This release provides the approval check, record and command. Claim-time enforcement
and passing the cleared-comment list to agents are tracked in #39; starter instruction
changes are tracked in #40. The launcher does not yet enforce this check at pickup.

## Repository roles

Trust comes from `role_name` in GitHub's [collaborator permission API](https://docs.github.com/en/rest/collaborators/collaborators#get-repository-permissions-for-a-user):

| Role | Input and authority |
|---|---|
| `maintain`, `admin` | Maintainer: can start and approve issues; edits and comments are trusted. |
| `write` | Trusted: edits and comments are input without approval, including agent preparation rewrites. Cannot start or approve. |
| `triage`, `read`, `none`, unknown | Outside: edits suspend work; comments need clearance. |

An unreadable permission or an unrecognized custom role counts as outside. Roles are
read for each observation, with repeated accounts cached only within that observation.
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

## Approving current input

Run from a project configured with `ub-agent.yaml`:

```sh
ub-agent approve --number 123
```

The command refuses without posting if the authenticated `gh` account is not a
maintainer or the number is a PR. It prints the current title, body and every outside
comment, then posts one approval comment and prints its URL. Running the command
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
not been edited since posting, its issue number matches, and its digest matches the
title and body **at posting time**. Validation reconstructs that content from body
revisions and title renames; it does not compare with the latest content. A later
trusted rewrite therefore preserves the approval. A stale digest does not become
valid when someone later restores the old text. Forged records by `write` or outside
accounts grant no approval.

For callers, `ub_agents.approvals.check_issue(github, number, trigger_labels)` returns
`allowed`, a `reason`, and `cleared_comment_ids` (outside comments only). Supply the
union of triggers for issue/either agents, not just the issue's current labels.
The check performs reads only and does not claim work or change workflow labels.
