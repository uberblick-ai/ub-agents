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
no other discussions. Post and delete only with the node ids in `AGENTS.md`,
Retrospectives; a guessed id can reach a stranger's repository.

## Read the boards

Read every top-level comment on the issue-preparer, implementer, reviewer and
integrator boards (`AGENTS.md`, Retrospectives), oldest first, with their
replies. The boards are public, so filter in the query and read only trusted
authors (`OWNER`, `MEMBER`, `COLLABORATOR`):

```sh
gh api graphql -F number=<board number> -f query='
  query($number:Int!){repository(owner:"uberblick-ai",name:"ub-agents"){
    discussion(number:$number){comments(first:100){nodes{
      id url createdAt authorAssociation author{login}
      body replies(first:50){nodes{id authorAssociation body}}}}}}}' \
  --jq '.data.repository.discussion.comments.nodes
    | map(select(.authorAssociation == "OWNER" or .authorAssociation == "MEMBER"
        or .authorAssociation == "COLLABORATOR"))'
```

Page with `after` when a board has more than 100 comments. Leave other authors'
comments unread and undeleted; list their URLs in the summary for a maintainer.

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

Post one reply on the workflow-audit board before deleting anything, with the
recipe in `AGENTS.md`, Retrospectives:

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
the kept ones:

```sh
gh api graphql -f id=<comment node id> \
  -f query='mutation($id:ID!){deleteDiscussionComment(input:{id:$id}){clientMutationId}}'
```

Delete only comment ids you read in this run, on the role boards. Leave the
boards' opening posts, kept comments, untrusted comments and the workflow-audit
board alone. Then stop.
