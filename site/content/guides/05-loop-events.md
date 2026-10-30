# Run a command on loop events

The [events hook](/docs/configuration/events.html) runs a command of yours whenever a run starts or finishes. The command can do anything that should follow the loop's progress: post to chat, record run times and outcomes, update a board or dashboard, or start a job in another system. The Slack message at the end of this page is one example.

## How it works

You name one command and the events it wants in `ub-agents.yaml`:

```yaml
events:
  command: [mise, run, loop-event]
  on: [started, outcome]
```

- `started` arrives once a launcher has claimed an item and started a run on it.
- `outcome` arrives when the run finishes, after its labels have changed.
- Each event runs the command once, in the launcher's checkout of the default branch, with the event as one JSON object on stdin.

An `outcome` event looks like this:

```json
{
  "event": "outcome",
  "repository": "OWNER/REPO",
  "run": "fd23f152613b408ea39c1af5b3a01f8a",
  "agent": "integrator",
  "assignment": 42,
  "kind": "pr",
  "url": "https://github.com/OWNER/REPO/pull/42",
  "attempt": 1,
  "mode": null,
  "status": "success",
  "outcome": "merged",
  "summary": "Merged the candidate as a squash commit; issue #41 closed.",
  "actions": [],
  "options": [],
  "handoff": null,
  "labels_added": [],
  "labels_removed": ["ready-to-merge"]
}
```

A `started` event has only the fields down to `mode`. The [field reference](/docs/configuration/events.html#events) explains each one.

## Pick the events in your command

The launcher sends every event you subscribe to, and your command decides what to do with each. Check fields such as:

- `agent`, to react to one role, such as the issue preparer starting or the integrator finishing.
- `outcome`, the outcome the agent reported, such as `merged` or `changes-requested`. It is `null` when the run failed or timed out; `status` then says `retry` or `blocked`.
- `labels_added`, to catch any run that stops for a person with `needs-human`.

Exit without doing anything for the events you don't want.

## What the hook can't change

- **The loop never waits for it.** A slow, failing or missing command only prints an `Events hook:` notice in the launcher. The run, its labels and the next claims go ahead as if there were no hook.
- **Commands get 10 seconds by default.** Set `timeout-seconds` up to 60. A command still running then is stopped.
- **Each launcher sends its own runs' events,** in order. With several launchers, each one runs the command, so each needs the tools and secrets it uses.
- **An event can be lost or repeated** when a launcher crashes. If a repeat matters, remember each `(repository, run, event)` you've handled and skip it the second time.

## Keep secrets out of the configuration

The command inherits the launcher's environment. Don't put tokens or webhook URLs in `ub-agents.yaml` or the repository. Instead, have a [mise](https://mise.jdx.dev) task load them from your secret store just for this command, for example with fnox:

```toml
[tasks.loop-event]
description = "Handle a ub-agents loop event from stdin"
run = "fnox exec -- python3 scripts/loop-event.py"
```

For another secret store, use its own command runner in place of `fnox exec --`. Without one, set the variables in each launcher's environment, such as its shell profile or [service definition](/docs/guides/add-a-launcher.html#run-it-as-a-service), and restart the launcher.

## Try it and turn it on

Pipe a sample event into the command to test it before you merge anything:

```sh
echo '{"event": "outcome", "agent": "integrator", "assignment": 42,
  "url": "https://github.com/OWNER/REPO/pull/42", "status": "success",
  "outcome": "merged", "summary": "Merged."}' | mise run loop-event
```

Then:

- Upgrade every launcher of the project to 0.3.8 or later. Older launchers reject the `events` key.
- Merge the command and the configuration together. Launchers pick up configuration changes by themselves.
- Run `ub-agents doctor` to check the configuration.

## Example: post to Slack when the integrator finishes

This posts one Slack message each time the integrator finishes a run, saying what it did. Create a Slack app with an [incoming webhook](https://api.slack.com/messaging/webhooks) for the channel, and store its URL as `SLACK_WEBHOOK_URL` in your secret store.

Save this as `scripts/loop-event.py`:

```python
import json
import os
import sys
from urllib.request import Request, urlopen

event = json.load(sys.stdin)
if event["event"] != "outcome" or event["agent"] != "integrator":
    sys.exit()

result = event["outcome"] or event["status"]
summary = event["summary"].split(". ")[0]
text = f"Integrator {result} on <{event['url']}|#{event['assignment']}>: {summary}"

request = Request(
    os.environ["SLACK_WEBHOOK_URL"],
    data=json.dumps({"text": text}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
with urlopen(request, timeout=5) as response:
    response.read()
```

Use the `loop-event` task above, and subscribe to outcomes only:

```yaml
events:
  command: [mise, run, loop-event]
  on: [outcome]
```

A merge then posts a message such as "Integrator merged on #42: Merged the candidate as a squash commit; issue #41 closed." The script keeps only the summary's first sentence, because summaries can run long. When the integrator hands a PR to you or sends it back, the message says `maintainer-merge` or `changes-requested` instead.

A PR that you merge yourself has no integrator run, so it posts nothing.
