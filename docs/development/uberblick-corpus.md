# Uberblick corpus

## Sources and scope

Two MCP servers reach two corpora. Use `uberblick-agents` for this framework's
workflow context and `uberblick-product` for current Uberblick product context,
when relevant. Discover documents through the servers rather than relying on
copied corpus content. The [project MCP configuration](../../.mcp.json) pins each
server's workspace and hub. Credentials remain in each machine's local Uberblick
configuration.

Start with [UB Agents start here](6b2c3094-ff5f-4d97-af81-c637cbacb2f9) and its
[Where truth lives table](6b2c3094-ff5f-4d97-af81-c637cbacb2f9), which identifies
the authoritative source for each kind of question. Corpus links use document
UUIDs: read them with `get_doc` on `uberblick-agents`.

Repository behavior and the assigned issue define the implementation scope.
The standalone product does not depend on these servers at runtime. The
development requirements below apply to contributors to this repository and its
loop roles. They do not change starter templates or other projects' optional MCP
use.

## Use and reachability

The corpus must always be reachable for anyone working on this repository.
Check access when starting work. An unreachable server is a setup problem:
report it in the results and name the access or setup fix needed. Do not work
from memory or treat the missing source as permission to bypass its rules.

Search the relevant corpus's decisions before changing behavior. Read a matching
decision and its record in force, rather than inferring approval from a search
snippet or a superseded record. When a request conflicts with a decision in
force, ask a maintainer to resolve the conflict before implementing it.

Good: read the decision about queue selection before changing its ordering.
Bad: assume a remembered decision still applies, or copy its text into a new
repository guide.

## Who writes the corpus

The integrator, through the corpus pass below, and maintainers write corpus pages.
Other roles read them. Corpus edits fall outside the reviewed diff and can describe a
candidate that changes or never merges, so a change records what the corpus needs in
its PR, and the pass applies it once the PR has passed its gates.

Decisions come from a person, and a record shows who suggested the answer. Any role
may propose a decision record by creating it `open` with a recommendation, tagged
`agent-proposal`, and linking it from its issue or PR. Work may go ahead on an agent's
proposal only where that is easy to undo. Work may build on a person's suggestion, an
`open` record with no such tag, unless that is hard to undo. Only a maintainer answers
a record: no role moves one to `decided`, including as a first-record agent stance.

### Corpus note

Every PR that changes behavior ends its body with a `Corpus note`: the pages it
affects, each with its UUID and the claim or reason involved, or `none` with a
short reason. Add any reasoning that a later writer can't find in the issue or PR.
Don't draft page text unless a short paragraph says it best. The note is a hint for
the pass, not a list of what the pass may check.

```text
Corpus note:
- Terminal interface direction (dc396c72-408d-4b10-88fe-b87064064065): tabs now name
  the repository. Reason: see the issue's Problem section.
```

### Corpus pass

The integrator owns the pass. It runs it once its own merge of a PR has landed, before
reporting `merged`, or against the candidate before reporting `maintainer-merge`, so
the corpus is done before a maintainer merges. Most PRs need no page change, so a small
PR gets a small pass.

1. Fetch `main` and compare pages with that commit, plus the candidate when the PR is
   waiting for a maintainer, so overlapping passes from several launchers settle on
   current code.
2. Find the checkpoint: go through merged PRs newest first, reading their comments for
   `Corpus pass complete at SHA` from an account with write access. Stop at the first
   you find or at `v0.5.3`. If a read fails, report it rather than treating it as no
   checkpoint.
3. Take the PRs merged after the checkpoint, oldest first and at most 20, so PRs that
   a maintainer merged or whose pass didn't finish are covered too, and the PR you're
   handling. Read their corpus notes and the open questions the checkpoint comment lists.
4. Search the corpus by each changed command, option or feature's name, and read each
   page that covers it as a whole. The notes are hints: they don't limit what you check.
5. Fix what is wrong and add reasoning that a note supplies. Leave anything that needs
   a decision as a question, or propose a decision record.
6. Remove any stale status narration from the pages you read.
7. Record a `code_check` at the fetched commit on each page compared.
8. Comment `Corpus pass complete at SHA` on the PR you're handling, naming the pages
   changed and the questions still open. SHA is the merge commit of the last merged PR
   covered, so a PR waiting for a maintainer is checked again after it merges.

An open question doesn't make the run blocked: name it in the report and the next
pass raises it again. An unreachable corpus never holds up a merge or a handoff: report
it as the setup problem it is and leave no comment. A pass that stops also leaves no
comment, and after a merge the launcher posts an **Action needed** notice. The next pass
covers those PRs, but only an integrator run starts one, so a maintainer can run the
pass when none is coming. If a PR closes unmerged after a handoff pass, the pages its
comment names need correcting by whoever closes it.

`edit_block` splices only unmarked text: to change inline code or links, insert a new
block with `inline` runs and delete the old one.

### What pages hold

A page holds a lasting reason or promise and links to where the rest lives. Reshape
a page rather than appending to it; it grows only when it gains a new promise. Pages
follow `main`, so they can be ahead of the latest release, and ahead of `main` for a PR
waiting for a maintainer. A target that `main` doesn't
deliver yet says it is a target, links its issue and records `ahead` in `code_check`.
Leave out:

- status narration such as "pending review", "not yet on main" or "not in a release",
  because GitHub and the release notes hold that state;
- version samples, commit footers and source paths;
- pasted help and option lists, because [site command pages](../../site/content/commands/)
  generate help from the CLI.

## Writing style

Follow [Writing style](224ba734-e5c9-4318-8ac0-20a7ebc1567e) for docs, help text
and corpus pages. Rules kept in the corpus stay there: link to them instead of
copying them into contributor guidance.
