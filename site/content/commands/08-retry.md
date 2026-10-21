# ub-agents retry

Run `ub-agents retry` once you've fixed why an item stopped. It resets the item's failure count, records your reason on the issue or pull request and switches the labels back: stop labels such as `needs-human` come off, and the agent's trigger label goes on. A running launcher picks the item up on its next poll.

```sh
ub-agents retry 143 --reason "Blocker resolved"
ub-agents retry 150 --agent reviewer --reason "Findings resolved"
```

Without `--agent`, retry picks the first agent that handles that kind of item. It refuses while a launcher is working on the item, and leaves a closed item's labels alone.

## Review rounds

Retry also resets the agent's [round limit](/docs/configuration/outcomes.html#limit-rounds): `--agent reviewer` gives the starter reviewer three new rounds. For just one more review, swap `needs-human` for `needs-review` yourself.

```text
{{help retry}}
```
