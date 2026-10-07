# Add a launcher

Run more launchers on other machines, or let teammates run one. They share one queue through GitHub, and a claim keeps two launchers off the same item.

## On the new machine

Install, clone and log in with an account that has write access.

```sh
brew install uberblick-ai/tap/ub-agents
gh auth login
git clone https://github.com/your-org/your-project
cd your-project
```

## Check and launch

```sh
ub-agents doctor
ub-agents launch
```

## Which accounts count

By default every account with write access or higher can run a launcher. To allow only some, list them, and use the same list on every machine. See [Launcher accounts](/docs/configuration/launchers.html).

```yaml
launchers: [ub-bot, alice]
```

## Keep versions together

When a release note says launchers must be upgraded together, stop every launcher for the project before upgrading any. See [Upgrading](/docs/upgrading.html).
