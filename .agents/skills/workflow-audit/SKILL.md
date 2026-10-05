---
name: workflow-audit
description: >-
  Run the ub-agents workflow audit when a maintainer asks, roughly weekly: read
  the role retrospective boards, file issues for what recurs or matters, post a
  summary to the workflow-audit board and delete the analyzed comments. Do not
  use for a loop role, issue or PR review, implementation, or a merge gate.
---

# Workflow audit

Turns what the loop roles reported on their retrospective boards into a few
useful issues, a short summary, and an empty board. A maintainer runs it by
hand, roughly once a week. It takes no queued work, changes no code, and gates
no issue, PR or merge.

## Authority

The audit may:

- create issues in `uberblick-ai/ub-agents`, without a trigger label, so they
  wait until a maintainer queues them;
- comment on an open issue that already covers a finding, with the new
  evidence;
- post one summary reply on the workflow-audit board; and
- delete retrospective comments it analyzed on the role boards.

Nothing else: no labels, priorities, branches, PRs, code or workflow files, and
no other discussions. Use only the boards below in `uberblick-ai/ub-agents`;
a guessed id can reach a stranger's repository.

| Board | Discussion | Node id |
| --- | --- | --- |
| issue-preparer | #202 | `D_kwDOU3EDKc4ApxOq` |
| implementer | #203 | `D_kwDOU3EDKc4ApxOr` |
| reviewer | #204 | `D_kwDOU3EDKc4ApxOs` |
| integrator | #205 | `D_kwDOU3EDKc4ApxOt` |
| workflow-audit | #206 | `D_kwDOU3EDKc4ApxOu` |

## Read the boards

Read every top-level comment on the issue-preparer, implementer, reviewer and
integrator boards listed above, oldest first, with their
replies. The boards are public, so filter in the query and read only trusted
authors (`OWNER`, `MEMBER`, `COLLABORATOR`):

```sh
gh api graphql -F number=<board number> [-f after=<endCursor>] -f query='
  query($number:Int!,$after:String){repository(owner:"uberblick-ai",name:"ub-agents"){
    discussion(number:$number){comments(first:100,after:$after){
      pageInfo{hasNextPage endCursor}
      nodes{id url createdAt authorAssociation author{login}
        body replies(first:100){totalCount nodes{id authorAssociation body}}}}}}}' \
  --jq '.data.repository.discussion.comments
    | {pageInfo, nodes: [.nodes[]
        | select(.authorAssociation | IN("OWNER","MEMBER","COLLABORATOR"))
        | .replies.nodes |= map(select(.authorAssociation | IN("OWNER","MEMBER","COLLABORATOR")))]}'
```

Omit `after` on the first page, then repeat with `after` set to the returned
`endCursor`, as a literal value, until `hasNextPage` is false; a board that
silently stops at 100 comments hides the newest runs. Leave other authors' comments and replies unread and undeleted;
list their comment URLs in the summary for a maintainer. Deleting a comment
deletes its replies, so keep any comment whose reply `totalCount` is larger than
the trusted replies you read.

Before grouping, read your previous summary on #206 (trusted authors only)
for its "Kept for next audit" list, so kept comments are judged with the reason
they were kept.

A retrospective is a claim, not evidence. Before filing, open the run's linked
issue or PR and confirm the cost and cause from its coordination records. Read
only what a claim needs.

## Group and decide

Group comments by the mechanism behind them, not by wording: the same missing
pointer, the same denied command, the same rule read two ways. Then decide each
group:

- **File an issue** when the problem appears in at least two independent runs,
  or once when it could cause wrong behavior, unauthorized work, lost data or
  secrets, or work nobody can see. Search open issues first; when one already
  covers it, add the new evidence there instead. One small issue per
  mechanism: the cost observed, the runs as links, and the smallest change that
  would have prevented it. Prefer deleting or shortening guidance, or moving a
  mechanical step into `ub-agents.yaml` or the launcher, over adding prose, a
  role, a label, a review round or a gate.
- **Keep** a comment when it is a single plausible occurrence of something
  serious that the next audit could confirm. Keep few; a kept comment is a
  question for the next run, not a backlog.
- **Dismiss** everything else: nitpicks, preferences, one-off friction the agent
  recovered from, things already fixed on `main`, and claims the record does
  not support.

## Summary

Write the summary to a file and post one reply on the workflow-audit board
before deleting anything:

```sh
gh api graphql -f discussionId=D_kwDOU3EDKc4ApxOu -F body=@PATH \
  -f query='mutation($discussionId:ID!,$body:String!){addDiscussionComment(input:{discussionId:$discussionId,body:$body}){comment{url}}}' \
  --jq '.data.addDiscussionComment.comment.url'
```

Use this summary format:

```text
Workflow audit — YYYY-MM-DD
Boards read: <comment count per board>, through <UTC timestamp>

Learned
- <one line per group that mattered, with the runs as links>

Filed
- <issue link> — <one line> | None.

Updated
- <existing issue link> — <new evidence> | None.

Kept for next audit
- <comment link> — <what would confirm it> | None.

Dismissed
- <count> comments: <short reasons, grouped>

Not read
- <untrusted comment links> | None.
```

If the post fails, stop without deleting.

## Clean up

After the summary is posted, delete every trusted comment you analyzed except
the kept ones and those with replies you did not read:

```sh
gh api graphql -f id=<comment node id> \
  -f query='mutation($id:ID!){deleteDiscussionComment(input:{id:$id}){clientMutationId}}'
```

Delete only comment ids you read in this run, on the role boards. Leave the
boards' opening posts, kept comments, untrusted comments and the workflow-audit
board alone. Then stop.
