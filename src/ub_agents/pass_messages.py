"""Buffer targeted refusals and coalesce repeated discovery messages."""


class PassMessages:
    def __init__(self, output):
        self.output = output
        self._declines = {}
        self._pass_declines = None
        self._item_refusals = None
        self.refusal_output = None
        self._shown = {}
        self._released_blockers = {}

    def begin_pass(self):
        self._pass_declines = {}

    def end_pass(self):
        self._declines = self._pass_declines
        self._pass_declines = None

    def declined(self, plan, reason, state="declined"):
        key = (plan.item.number, plan.agent.name)
        current = self._pass_declines if self._pass_declines is not None else self._declines
        value = (state, reason)
        if current.get(key, self._declines.get(key)) != value:
            self.output(f"#{plan.item.number} {plan.agent.name}: {state} — {reason}")
        current[key] = value

    def begin_item(self):
        self._item_refusals = {}

    def end_item(self):
        self._item_refusals = None

    def item_refusal(self, line, agent_name=None, *, on_success=True):
        line = " ".join(line.split())
        if self._item_refusals is None:
            self.output(line)
        else:
            self._item_refusals[agent_name] = (line, on_success)

    def flush_item_refusals(self, *, final=False):
        if not self._item_refusals:
            return
        lines = tuple(line for line, on_success in self._item_refusals.values() if final or on_success)
        self._item_refusals.clear()
        if final and self.refusal_output is not None:
            self.refusal_output(lines)
        else:
            for line in lines:
                self.output(line)

    def reached(self, plan):
        if plan.state in {"ready", "recover"}:
            self._shown.pop((plan.item.number, plan.agent.name), None)

    def waiting(self, plan):
        key, value = (plan.item.number, plan.agent.name), (plan.state, plan.reason)
        released = self._released_blockers.pop(key, None)
        announced = plan.state == "blocked" and released is not None and released in plan.reason
        if not plan.health_wait and not announced and self._shown.get(key) != value:
            self.output(f"#{plan.item.number} {plan.agent.name}: {plan.state} — {plan.reason}")
        self._shown[key] = value

    def prune(self, present):
        self._shown = {key: value for key, value in self._shown.items() if key in present}
        self._released_blockers = {key: value for key, value in self._released_blockers.items() if key in present}

    def released_blocker(self, plan, summary):
        self._released_blockers[(plan.item.number, plan.agent.name)] = summary
