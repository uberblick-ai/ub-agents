# ub-agents loop policy

## Checks

Install this checkout only into its own .venv.
Run the checks in `AGENTS.md` before handoff and record the results.
A check that passes only after changing its environment, skipping tests or adding
extra flags has not passed. Fix repository causes in code or tests; report external
blockers with evidence. The framework supplies no checks, acceptance rules or grants.

## Handoffs

Implementation PR bodies start with `Closes #N`. Agents never enable auto-merge,
approve their own PRs, change branch protection or publish releases. Only the
integrator merges, under the policy below.

## Merging

The integrator squash-merges a PR once every owed review and check applies to its
current head, with `--match-head-commit` set to the assigned SHA or the head of its
own clean base merge. That clean base merge keeps the review and needs no new
review. The check is a green `signoff` status from local CI at the head it merges.
The integrator runs it: detach its own worktree at `origin/main`
(`git switch --detach origin/main`), run `mise trust` there,
and run `mise run ci SHA`, every time: a `signoff` already on the commit only says someone
posted it, not that the checks ran. It leaves the merge
to a maintainer, and says why, when the PR:

- changes the `ub-agents` command-line experience without the issue it closes
  explicitly asking for it: adds, removes or renames commands or options, or changes
  what existing commands do or print.
- needs all of a project's launchers stopped and restarted together. That applies to
  any change a running launcher of the previous build would reject or misread: the
  coordination record format, agent branch names, the config file, or a config key
  or value that this repository's `ub-agents.yaml` starts using. Its PR body
  carries an **Upgrading** note that says so.
- changes this repository's own workflow: `AGENTS.md`, `.agents/`, `ub-agents.yaml`
  or `.github/`.

Updates to `README.md`, `docs/` and the website pages in `site/content/` that describe
what the closing issue asked for need no maintainer merge. The reviewer checks that
they are accurate. Before merging a change to what users install, run, configure or
see, the integrator reads the affected `site/content/` pages and, if one is now wrong
or missing, sends the PR back with `changes-requested` naming the page.

## Human decisions

The maintainer team answers project policy and scope decisions. An issue's author
may answer only if they have write access. Follow the project's authority and resume
rules; if another role must act next, use its correction or handoff route instead of
retrying the role that stopped.

## Review focus

Scrutinize durable outcomes, lost ownership, claim races, recovery and process
cleanup whenever a change touches coordination or execution.

A maintainer reads role-board retrospectives in the `ub-agents-deliver-report`
skill's daily report, published in [uberblick-ai/skills](https://github.com/uberblick-ai/skills).
Install it with `npx skills@latest add uberblick-ai/skills -g`.
