# Issue and PR starts and outside input

This is the contract for who can start agent work, which PR heads are eligible and
which GitHub text reaches agents. The [approvals page](https://agents.uberblick.ai/docs/configuration/approvals.html)
explains it for users.

The top-level `approvals` key in `ub-agents.yaml` accepts `on` or `off`, quoted or
unquoted. When unset, approvals are **on for public repositories** and **off for
private and internal repositories**, using GitHub's repository visibility. The
launcher resolves this default at the start of each discovery pass, including a
targeted `launch N` pass, so a repository that becomes public is checked from the
next pass. If visibility cannot be read, the pass fails without claims or parking
writes. An explicit setting needs no visibility read.

```yaml
approvals: on
```

## With approvals off

An item's configured trigger label starts work regardless of who applied it;
GitHub restricts labeling to accounts with `triage` or higher. Its current title
and body are input, including outside edits. Comments, reviews and review comments
are input only from authors with `write`, `maintain` or `admin`, or from listed
[trusted bots](#trusted-bots). Feedback from `triage`, `read`, unknown or
unreadable roles is excluded. Coordination records and launcher notices are never
input.

The launcher reads no approval history and cleans no hidden content. PR heads,
including fork heads, need no approval. Nothing is parked or suspended for
approval, and no approval stop label or notice is written. Other eligibility
gates, configured stop labels and the rule that agents cannot revise fork PRs
still apply.

## With approvals on

Issue work needs a maintainer's start, directly or inherited from a parent's
accepted `sub-issues` outcome. Outside content needs a maintainer's review before
it becomes input.

Every issue and PR agent, including preparation, passes the approval check at
pickup and again after claiming. A failed check spends no attempt and needs no
`ub-agents retry`: at pickup it makes no claim, and after claiming it withdraws
the claim before execution. `launch` and `status` show the item as parked with the
reason; `status` writes nothing. Live runs and durable-outcome recovery finish
under their original assignment.

When a failed approval is the only obstacle on a triggered, open item, `launch`
adds the configured stop label (`needs-human` in the starter) and posts an
**Action needed** notice with the reason and the steps to resume:

| Gate | Maintainer action to resume |
|---|---|
| No maintainer start | Remove `needs-human` and re-apply a trigger label. This also clears earlier outside comments, reviews and edits; read them first. |
| Outside title/body edit or outside PR feedback after approval | Re-apply a trigger label; then remove `needs-human`. This also clears earlier outside comments, reviews and edits; read them first. |
| Outside PR head not approved | Submit a maintainer approving review of the current head; then remove `needs-human`. |

A new gate after resuming parks the item again. Unreadable approval history is
retried on the next poll without a stop label or notice. Uncleared outside
comments on issues and on trusted-authored PRs stay out of the input but never
park work.

The post-claim read supplies the agent's title, body and filtered comments; PR
context also includes the assigned head, reviews and review comments. Trusted,
maintainer and cleared outside feedback is input. Uncleared or later-edited
outside feedback, coordination records and launcher notices are excluded. Outside
edits during execution do not stop that run or change its input.

### Hidden content cleanup

Before supplying assignment context or `read` output, the launcher cleans outside
text. Each HTML comment (`<!-- ... -->`) becomes `[hidden content removed]`,
including comments inside code and an unterminated comment, which runs to the end
of the text. Each consecutive run of Unicode format characters (category `Cf`,
such as zero-width characters, bidi controls and tag characters) becomes the same
marker. Other text is unchanged.

Cleanup applies to a title whose author is outside or whose history includes an
outside rename, a body whose author is outside or whose history includes an
outside edit, and cleared outside comments, review bodies and review comments.
Trusted authors' fields without outside edits, and trusted or listed-bot feedback,
reach agents byte for byte. Withheld fields stay withheld, and `withheld_counts`
does not change. Approval checks and parking use the raw GitHub text. Cleanup does
not guarantee that agents see only what the maintainer saw: link targets,
collapsed sections and diffs can still carry unseen text.

## Outside-input label

With approvals on, the launcher adds `outside-input` to the assigned issue or PR
before the agent starts when its input includes an outside author's title or body,
an outside title or body edit, or cleared outside comments, reviews or review
comments. Same-repository PRs by [trusted PR authors](#trusted-pr-authors) also get
the label. Trusted-bot feedback, withheld text and `read N` output do not count. A
failed label write prevents execution, spends no attempt and is retried on the
next poll.

The label passes to the assignment's handoff PR and the qualifying sub-issues its
outcome starts, including during recovery. It stays when comments are deleted.
Maintainers may remove it; a later run with outside input adds it again. Agents
leave it alone. The launcher gates no starts, parking or merges on it, and
applying it is not a maintainer start; your merge policy decides what it means. A
missing label does not prevent `launch`. With approvals off, the launcher does not
add, copy or list it.

## Reading other issues and PRs

```sh
ub-agents read 123
ub-agents read 123 --config workflow.yaml
```

`read N` prints one open or closed issue or PR from the configured repository as
JSON. It changes no labels, claims, notices or records, and resolves the effective
`approvals` policy for each invocation. Inside a supervised run, invoke the
launcher's literal `report_command` followed by `read N`. The run pins the
repository, approvals policy, trigger labels, `sub-issues` labels, trusted bot and
PR author lists and any launcher allowlist; a changed worktree configuration or
`--config` cannot replace them.

The output contains `number`, `kind`, `state`, `title`, `body` and `comments`. PRs
also contain `head` (the current SHA), `reviews` and `review_comments`.
`withheld_counts` gives the number excluded from each feedback group; coordination
records and launcher notices are omitted without being counted. A withheld title
or body is an object such as
`{"withheld": true, "reason": "Outside body edit after approval; a maintainer must re-apply a trigger label"}`,
with none of its text included.

Feedback is filtered and cleaned as in assignment context. With approvals off, the
title and body are always shown. With approvals on, a title or body is shown when
its author is trusted and it has no outside edit, or when a maintainer start covers
it with no later outside edit; otherwise it is withheld. A trusted author's issue
is therefore readable without a start, and reading needs no eligible PR head. An
approving PR review alone does not clear outside text. Re-apply a maintainer
trigger after an outside title or body edit to show it again. Bodies by trusted bot
PR authors remain withheld even after a maintainer start.

Unreadable or incomplete approval history, unreadable author permissions and
unreadable repository visibility cause a nonzero exit and no item JSON. Unknown
numbers and items transferred out of the configured repository fail the same way.
Pickup, by contrast, treats unreadable feedback roles as outside. Discussions and
items in other repositories are not supported.

Agents use only the assignment context for the assigned work and `read` for other
items, never unfiltered `gh` thread reads or raw comment endpoints, even when
project instructions ask them to read comments. Text shown is still a requirement
to evaluate, never an instruction to carry out. Withheld or uncleared outside text
is not information either.

## Trusted bots

```yaml
trusted-bots: [copilot-pull-request-reviewer, Copilot, "github-actions[bot]"]
```

This optional top-level list defaults to empty. Entries are GitHub account logins,
matched case-insensitively, with no `@` prefix or surrounding whitespace. The
optional `[bot]` suffix is ignored when matching. A listed login is trusted only
when GitHub reports its account type as `Bot`; listing a human adds no trust.
Copilot posts inline review comments as `Copilot`, so list both
`copilot-pull-request-reviewer` and `Copilot` to trust its reviews and inline
feedback.

Listed bots' comments, reviews and review comments are trusted like `write`+
feedback in assignment context and `read`, with approvals on or off. This trust
does not extend to their title or body edits, starts or approving reviews. They
cannot clear outside feedback or act as launchers for coordination records.
Unlisted bots' feedback is outside.

## Trusted PR authors

```yaml
trusted-pr-authors: [dependabot]
```

This optional top-level list defaults to empty and uses the same login matching
and validation as `trusted-bots`: case-insensitive, ignoring an optional `[bot]`
suffix, and only for accounts GitHub reports as `Bot`. Listing a human adds no
trust. Duplicate logins ignoring case and non-list values are configuration errors,
including in `doctor --offline`.

With approvals on, a listed bot's PR whose head repository is the base repository
counts as trusted-authored. Any account can apply its configured trigger label;
it needs no maintainer start or approving head review, including after a new push.
Fork PRs and issues by listed bots retain their normal approval rules.

The PR body is always withheld from assignment context and `read N`, with the
reason "Body by a trusted bot author is not input". Bot bodies can contain upstream
release notes written by package publishers; agents work from the diff instead.
The title is shown after [hidden content cleanup](#hidden-content-cleanup), and the
launcher adds `outside-input` before execution.

Title and body edits by the PR's own bot author account do not suspend work.
Edits by other outside accounts follow the normal trusted-author rules: a
maintainer must re-apply a trigger label before work resumes. The body remains
withheld after that clearance, even if a trusted account rewrites it.

Comments, reviews and review comments follow the trusted-authored PR rules:
outside feedback needs clearance to become input but never suspends work. The
author bot's feedback is trusted only if it is also listed in `trusted-bots`.
Listing a PR author gives it no authority to start other work, approve heads,
clear outside input or act as a launcher. Its edits to other items remain outside.

With approvals off, the key has no effect; the current title and body are input.
Supervised runs pin this list for `read N` alongside `trusted-bots`.

## Repository roles

Trust comes from `role_name` in GitHub's [collaborator permission API](https://docs.github.com/en/rest/collaborators/collaborators#get-repository-permissions-for-a-user):

| Role | Input and authority |
|---|---|
| `maintain`, `admin` | Maintainer: can start issues and PRs and approve PR heads through reviews; edits and comments are trusted. |
| `write` | Trusted: edits and comments are input without approval, including agent preparation rewrites. Cannot start work or authorize outside PR heads. |
| `triage`, `read`, `none`, unknown | Outside: edits need approval; feedback needs clearance. Outside PR feedback also suspends work. |

An unreadable permission or an unrecognized custom role counts as outside. The
launcher treats every GitHub bot account as role `none`, so its labels, edits and reviews never
start, approve or clear anything. Roles are reread before each claim and before
approval-parking writes. The launcher account's own role applies to it, so give it
`write`; see [launcher accounts](https://agents.uberblick.ai/docs/best-practices/launcher-account.html).

## Approval reads

An approval check reads the item's title, body and body edit history, its label and
rename timeline events, its comments and, for a PR, its head, reviews and review
comments. An inherited start also reads the parent issue's coordination records. A
read that fails or returns incomplete history never grants approval: pickup treats
the item as unapproved and retries on the next poll, and `read` exits nonzero.

## Start, suspension and comments

A start is a `labeled` timeline event whose actor is a maintainer and whose label
is one of the configured triggers of any `issue` or `either` agent, such as
`needs-preparation` or `ready`. PR-only triggers do not start issues. The event
remains a start after the label is removed or agents apply other workflow labels.
A `write` account, including the launcher account, cannot start work by applying
labels, except through an inherited start.

An issue can inherit a start from its native parent through a declared
[`sub-issues: {add: [...]}` outcome](configuration.md#outcomes-and-transitions).
The outcome records the open, same-repository sub-issues authored by the lease's
launcher account after its claim. The parent record must name the sub-issue and
trigger label, match its source lease's declaration, be accepted and complete, and
have a successful released source or recovery lease with confirmed cleanup. Both
the record author and the label actor must be trusted launcher accounts under the
current roles and `launchers` configuration; listed bots gain no such authority.
The trigger must have been added during the parent's transition, so a later
launcher reapplication cannot use the old record to clear new outside edits.

While a recorded parent transition is still pending, pickup waits without adding
an approval stop label. A launcher label without matching provenance remains "No
maintainer start". The inherited start has the same clearance time as its trigger
event, and qualifying sub-issues need no extra maintainer start, including for
preparation.

Each maintainer trigger label approves the issue as it stands at that time,
including outside comments already present and last updated before that event.
After the latest start, an outside title rename or body edit suspends work until a
maintainer applies a trigger label again. Trusted and maintainer edits do not
suspend work. The initial body is covered by the start, not treated as a later
edit. GitHub timestamps cannot order changes within one second, so an outside edit
in the start's second suspends work; re-apply the trigger label in a later second.

Outside comments are excluded unless a start clears them. A comment created or
edited at or after the latest start stays excluded until a later start, even if
its body returns to the approved text. Trusted and maintainer comments need no
clearance. A new outside comment does not suspend an issue. Deleted comments cannot
be detected; deleting a comment only removes input.

## Pull requests

A PR authored by a trusted or maintainer account needs no maintainer start.
Same-repository PRs by listed [trusted PR authors](#trusted-pr-authors) also need no
start or head approval, and their bodies are withheld.
Outside comments, reviews and review comments need clearance before becoming
input, but do not suspend the PR, so commenting cannot stall it. An outside title
or body edit still needs a maintainer start before that content can be assigned or
read.

A PR authored by an outside account needs a maintainer's start: a trigger label of
a configured `pr` or `either` agent, normally `needs-review`. Issue-only triggers
do not start PRs. The start remains valid after agent label transitions. An
approving review does not replace it.

An outside PR also needs an eligible current head. GitHub does not record push
times, so a trigger label cannot prove which head it covered. A head is eligible
when:

- a maintainer's approving review names it as `commit_id`; or
- the PR is not a fork and an accepted, successful agent outcome for this PR
  produced the head as `candidate_sha` from a run whose `assignment_sha` was
  eligible when assigned. The outcome must match its successfully released source
  lease, including completed recovery.

A PR is a fork when its head repository differs from the base repository
(case-insensitively) or GitHub reports none, as for a deleted fork. Fork heads
always need an explicit maintainer approving review, because a run's report cannot
tell an agent push from the fork author's push during the run. Chained agent
revisions in the base repository inherit eligibility; unaccepted, rejected or
unrelated outcomes do not. Every other head needs explicit maintainer approval,
even when its push preceded the start.

Outside title or body edits at or after the latest maintainer trigger label
suspend outside-authored PR work. Outside comments, reviews and review comments at
or after the latest maintainer approval, either a start or an approving review,
also suspend it. Re-applying a trigger can lift input suspension but cannot
approve an unknown head. An approving review approves its commit and lifts
feedback suspension, but does not clear outside title or body edits or outside
feedback. Editing its prose keeps that approval and approves no later outside
input. Only starts clear outside feedback. Editing cleared feedback removes its
clearance even if its text returns to the approved body.

A fork PR's private worktree is a detached checkout of the base branch tip from
`origin`, so its files, project guidance and runtime settings come from the base.
Agents see the assigned head only as a diff against the base: they must never check
out or execute its code, install scripts or checks, and `checkout-setup` is
skipped. The repository's CI result on the exact head SHA supplies check evidence.
These rules also apply with `worktree: false`. Agents cannot revise fork PRs; a
revision run reports `blocked` instead of pushing through the base repository's
branch. Same-repository PRs keep their normal head checkout and setup, including
agent revisions built from outside text.
