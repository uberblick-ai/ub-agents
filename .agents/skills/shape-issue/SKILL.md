---
name: shape-issue
description: >-
  Turn a maintainer's idea for ub-agents into the smallest useful issue, placed in
  a milestone with a user benefit, through a short conversation. Use for "file an
  issue for X", "shape this idea" or "split this into milestones". Writes only after
  the maintainer confirms; preparation and implementation stay with the loop. Not
  for loop roles.
---

# Shape an issue

Shaping settles what to build and when. The issue-preparer adds acceptance criteria
and grounding later, so leave out architecture, file lists and edge cases.

1. **Understand the outcome.** Restate the problem and the smallest change a user
   would notice. Treat suggested mechanisms as clues, not requirements. Check open
   issues and recent merges for overlap (`gh issue list --search "WORDS"`,
   `git log origin/main --oneline --grep WORD`) and say if it is already delivered
   or filed. Ask one focused question only when its answer changes the outcome or
   scope.

2. **Cut to the MVP.** Propose the smallest scope that still delivers the benefit,
   and name what is deferred. Keep cohesive work in one issue. When an unknown
   decides the design, propose a spike that answers it first. When the work exceeds
   one reviewable PR, propose a sequence in which every step is useful on its own.

3. **Place it in milestones.** List the open milestones with
   `gh api 'repos/uberblick-ai/ub-agents/milestones?state=open' --jq '.[] | "\(.title): \(.description)"'`.
   - Every milestone delivers a user benefit. Internal work, such as a refactor,
     tests or CI, joins a milestone that also ships something users notice; never
     propose a milestone of internal work alone.
   - Follow the [release rules](../../../AGENTS.md#changelog): a patch milestone
     holds only fixes. Anything users would see under Added, Changed or Removed, a
     new config key or value, or an upgrade step beyond restarting launchers one at
     a time belongs in a minor milestone.
   - Prefer the active milestone when the work serves its goal, otherwise the
     next one that fits. When none fits, propose a new milestone: a version title
     that sorts in roadmap order, a one-line goal stating the user benefit, and the
     issues it holds.
   - Link a blocker only where one issue supplies behavior another needs, never
     for file overlap.

4. **Confirm.** Show each draft issue's title, body, milestone, priority and
   blockers. Use a priority only as the maintainer states it. Write only after an
   explicit yes; silence is not approval.

5. **File.** Title the issue with its user-visible change. Use this body:

   ```markdown
   ## What
   The change and the smallest observable outcome.

   ## Why
   The problem and who benefits.

   ## Not now
   Deferred scope; omit if none.
   ```

   Create a confirmed new milestone first
   (`gh api repos/uberblick-ai/ub-agents/milestones -f title=TITLE -f description=GOAL`),
   then `gh issue create --title TITLE --body-file PATH --milestone "MILESTONE"`.
   Add `needs-preparation` only when the maintainer asks to queue the issue; issues
   in later milestones stay unlabelled. Link blockers with
   `gh api repos/uberblick-ai/ub-agents/issues/N/dependencies/blocked_by -F issue_id=ID`,
   where ID comes from `gh api repos/uberblick-ai/ub-agents/issues/M --jq .id`.
   Report the issue and milestone links.
