# ub-agents retry

Run `ub-agents retry` once you've fixed why an item stopped. It resets the item's failure count, records your reason on the issue or pull request, and tells you what's next. Labels stay as they are.

```sh
ub-agents retry 143 --reason "Blocker resolved"
ub-agents retry 150 --agent reviewer --reason "Findings resolved"
```

Without `--agent`, retry picks the first agent that handles that kind of item. Counts live on GitHub, so restarting or switching launchers doesn't reset them. Retry refuses while a launcher is working on the item.

## Review rounds

Retry also resets the agent's [round limit](/docs/configuration/outcomes.html#limit-rounds). To give the starter reviewer three new rounds, run `retry` with `--agent reviewer`, then replace `needs-human` with `needs-review`. To allow just one more review, only swap the labels.

```text
{{help retry}}
```
