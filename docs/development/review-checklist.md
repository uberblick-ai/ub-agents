# Review checklist

Use this checklist while implementing and reviewing a change. Each blocking item
maps to the reviewer's P1: the PR must not merge until it is corrected. Recommended
items are P2 unless they also expose a blocking problem. The
[reviewer procedure](../../.agents/reviewer.md) defines the verdict and reporting
rules; this checklist links to the rules rather than replacing them.

| Level | Check | Rule |
| --- | --- | --- |
| Blocking (P1) | The candidate satisfies the assigned issue and accepted feedback, with every acceptance criterion checked or its gap explained. | [Acceptance evidence](../../.agents/reviewer.md) |
| Blocking (P1) | The review and check evidence applies to the assigned SHA, and the PR is ready for review. | [Exact candidate](../../.agents/reviewer.md) |
| Blocking (P1) | Required checks pass in the declared environment; failures are fixed or reported with evidence. | [Checks](../../AGENTS.md#checks), [check failures](../../.agents/implementer.md) |
| Blocking (P1) | The corpus is reachable, relevant decisions have been consulted, and conflicts with a decision in force have a maintainer answer. | [Corpus use and reachability](uberblick-corpus.md) |
| Blocking (P1) | Changed behavior has tests, and external calls use the project's fakes. | [Changed behavior](testing.md#changed-behavior) |
| Blocking (P1) | New and growing functions meet the complexity limits; obsolete suppressions are removed. | [Complexity baseline](checks.md) |
| Blocking (P1) | Runtime dependencies have maintainer agreement and GitHub calls use the existing client. | [Dependencies](principles.md#dependencies) |
| Blocking (P1) | New modules explain their purpose and usage; changed module docstrings remain accurate. | [Module docstrings](conventions.md#module-docstrings) |
| Blocking (P1) | Errors preserve failures and their causes rather than reporting empty results or success. | [Error handling](conventions.md#error-handling) |
| Blocking (P1) | Operator and contributor documentation remains accurate in its home, and a behavior change carries a corpus note. | [Documentation homes](../../AGENTS.md#documentation-homes), [corpus note](uberblick-corpus.md#corpus-note) |
| Blocking (P1) | The PR title describes its change, internal-only work is identified, and required upgrade instructions are in the PR body. | [Pull requests](changelog-and-releases.md#pull-requests) |
| Blocking (P1) | Release-prep work follows the release scope and version rules; loop roles leave publication to a maintainer. | [Release preparation](changelog-and-releases.md#release-preparation), [publication](changelog-and-releases.md#publishing-releases) |
| Blocking (P1) | Coordination and execution changes preserve ownership, recovery and process cleanup; bugs, security risks and data-loss risks are resolved. | [Review focus](../../.agents/ub_agents.md#review-focus), [P1 definition](../../.agents/reviewer.md) |
| Recommended (P2) | The change uses small modules and the simplest solution that meets its requirements. | [Small modules](principles.md#small-modules), [simplest solution](principles.md#simplest-solution-first) |
| Recommended (P2) | Tests stay short, at about one test per changed behavior. | [Test size](testing.md#changed-behavior) |
| Recommended (P2) | Comments explain why, and usage examples clarify entry points when useful. | [Comments](conventions.md#comments), [module docstrings](conventions.md#module-docstrings) |
| Recommended (P2) | Prose follows the linked corpus Writing style rules, and release bullets follow the changelog guidance. | [Writing style](uberblick-corpus.md#writing-style), [Changelog](changelog-and-releases.md#changelog) |
