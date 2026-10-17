# ub-agents retry

Run `ub-agents retry` once you've fixed why an item stopped. It resets the item's failure count, records your reason on the issue or pull request and switches the labels back: stop labels such as `needs-human` come off, and the agent's trigger label goes on. A running launcher picks the item up on its next poll.

```sh
ub-agents retry 143 --reason "Blocker resolved"
ub-agents retry 150 --agent reviewer --reason "Findings resolved"
```

Without `--agent`, retry picks the first agent that handles that kind of item. Counts live on GitHub, so restarting or switching launchers doesn't reset them. Retry refuses while a launcher is working on the item. On a closed item it records the reset and leaves the labels alone. If a label change fails, it names the labels to change by hand.

## Review rounds

Retry also resets the agent's [round limit](/docs/configuration/outcomes.html#limit-rounds). To give the starter reviewer three new rounds, run `retry` with `--agent reviewer`. To allow just one more review without resetting the count, replace `needs-human` with `needs-review` yourself.

```text
{{help retry}}
```
