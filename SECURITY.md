# Security

Thanks for looking. ub-agents is built by a small team, so this page is short.

## Reporting a vulnerability

Report it privately through
[GitHub private vulnerability reporting](https://github.com/uberblick-ai/ub-agents/security/advisories/new).
Please don't open a public issue, pull request or discussion.
If you can't use GitHub, email security@uberblick.ai.

A useful report has what you found, what an attacker gains, the version
(`ub-agents --version`) and steps to reproduce against a repository you own.

## What happens next

- We confirm we have your report within 3 working days.
- Within 10 working days we tell you whether we see a vulnerability and what we plan.
- We fix it in a release and publish a GitHub security advisory, with a CVE
  when it qualifies. We credit you unless you'd rather we didn't.
- Please wait for the fix, or 90 days, before you publish. If we go quiet, ping the report.

## Supported versions

Only the latest release gets security fixes. There are no backports:
updating is the fix.

## Scope

ub-agents runs coding agents on your machine with your GitHub and agent
credentials, and feeds them issue and pull request text. Text from people
outside your maintainers is untrusted. We especially want to hear about:

- outside text reaching an agent without a maintainer's approval while
  [approvals](https://agents.uberblick.ai/docs/configuration/approvals.html) are on,
- someone without the required GitHub role starting agent work,
- a pull request labelled `outside-input` getting merged past your merge policy,
- a token or other credential from the loop host ending up in a pull request,
  comment, log or session snapshot.

Out of scope:

- An agent doing what trusted input asked it to. Agents run with the
  permissions you give them.
- Outside issue and pull request titles and bodies reaching agents while
  approvals are off, which allows that input.
- Vulnerabilities in the agent CLIs themselves (Claude Code, Codex). Report
  those to their vendors.
- Versions older than the latest release.

## Good-faith research

If you follow this page, we won't take legal action against you or ask
GitHub to. Test only against repositories and machines you own. Don't
trigger agents on other people's repositories, and if you reach someone
else's data by accident, stop and tell us.
