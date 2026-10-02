"""Advisory comment presentation, isolated from coordination authority."""

import json
import socket

from .records import own_comment, records

ACTION_MARKER = "<!-- ub-agent:action-needed "


class Notices:
    def __init__(self, github, actor, output=print):
        self.github, self.actor, self.output = github, actor, output
        self._approval_attempted = set()

    def advisory(self, operation, action):
        try:
            return action()
        except Exception as exc:
            self.output(f"Advisory {operation} failed: {' '.join(str(exc).split())}")
            return None

    def minimize(self, comments):
        if not comments:
            return
        pending = self.advisory("comment minimization state read",
                                lambda: self.github.unminimized_comments(comments))
        for comment in pending or ():
            self.advisory(f"minimize comment {comment['id']}",
                          lambda: self.github.minimize_comment(comment))

    def resumed(self, number):
        def minimize_actions():
            self.minimize([comment for comment in self.github.comments(number)
                           if own_comment(comment, self.actor)
                           and (comment.get("body") or "").startswith(ACTION_MARKER)])
        self.advisory(f"resume notices on #{number}", minimize_actions)
        self._approval_attempted = {key for key in self._approval_attempted if key[0] != number}

    def approval(self, number, check, stops, triggers):
        if not stops:
            return
        key = (number, check.gate_key)
        if key in self._approval_attempted:
            return
        marker = f"{ACTION_MARKER}approval-{check.gate_key} -->"
        matching = [c for c in self.github.comments(number) if own_comment(c, self.actor)
                    and (c.get("body") or "").startswith(marker)]
        if matching and self.github.unminimized_comments(matching):
            self._approval_attempted.add(key)
            return
        # Failed writes are advisory and are not retried for this gate in this
        # session. A successfully posted notice also deduplicates after restart.
        self._approval_attempted.add(key)
        labels = ", ".join(f"`{label}`" for label in stops)
        trigger_text = ", ".join(f"`{label}`" for label in triggers)
        if check.gate == "start":
            resume = (f"A maintainer must remove the stop label(s) {labels} and re-apply a trigger label: "
                      f"{trigger_text}. An approval alone does not start work.")
        elif check.gate == "head":
            resume = (f"A maintainer must run `ub-agent approve --number {number}` or submit an approving "
                      f"review of the current head; then remove the stop label(s) {labels}. "
                      "Re-applying a trigger label does not approve a head.")
        else:
            resume = (f"A maintainer must re-apply a trigger label ({trigger_text}), or run "
                      f"`ub-agent approve --number {number}`; then remove the stop label(s) {labels}.")
        self.advisory(f"approval stop label on #{number}", lambda: self.github.add_labels(number, stops))
        self.advisory(f"Action needed post on #{number}", lambda: self.github.create_comment(
            number, f"{marker}\n**Action needed**\n\n{check.reason}\n\n{resume}\n"))

    def superseded(self, number, agent, run):
        def minimize_records():
            comments = self.github.comments(number)
            history = [r for r in records(comments, self.actor)
                       if r["agent"] == agent and r["kind"] in {"lease", "outcome"}]
            current = [r["id"] for r in history if r["run"] == run]
            if not current:
                return
            latest = {r["kind"]: r for r in history}
            outdated = {r["id"] for r in history
                        if r["id"] < min(current) and r["run"] != latest[r["kind"]]["run"]}
            self.minimize([comment for comment in comments if comment["id"] in outdated])
        self.advisory(f"superseded records on #{number}", minimize_records)

    def released(self, lease, outcome, summary, parking_outcome=None, max_attempts=None):
        reported = parking_outcome or outcome
        target = lease["assignment"]
        transition = reported.get("transition", {}) if reported else {}
        stops = sorted(set(transition.get("add", ())).intersection(transition.get("stop_labels", ())))
        parked = (lease["result"] == "success" and reported
                  and reported.get("accepted") and reported.get("transition_complete") and stops)
        if parked:
            target = reported.get("handoff") or target
        self.superseded(lease["assignment"], lease["agent"], lease["run"])
        if reported and reported.get("handoff") and reported["handoff"] != lease["assignment"]:
            self.superseded(reported["handoff"], lease["agent"], reported["run"])
        exhausted = (lease["result"] == "retry" and lease.get("unreported")
                     and lease.get("attempt_effect") == "failure" and max_attempts is not None
                     and lease["attempt"] >= max_attempts)
        if exhausted:
            summary = f"Attempt limit exhausted (max-attempts: {max_attempts}). {summary}"
        if lease["result"] == "blocked" or parked or exhausted:
            self.advisory(f"Action needed post on #{target}",
                          lambda: self.post_action(target, lease, reported, summary, stops if parked else ()))

    def post_action(self, number, lease, outcome, summary, stops):
        marker = f"{ACTION_MARKER}{lease['run']} -->"
        comments = self.github.comments(number)
        if any(own_comment(c, self.actor) and (c.get("body") or "").startswith(marker) for c in comments):
            return
        history = records(comments, self.actor)
        anchors = [r["id"] for r in history if r["run"] == lease["run"]
                   or (outcome and r["run"] == outcome["run"])]
        if anchors and any(r["kind"] in {"lease", "reset"} and r["id"] > max(anchors) for r in history):
            return  # A later claim/reset already resumed this item.
        sha = (outcome.get("candidate_sha") if outcome else None) or lease.get("assignment_sha")
        evidence = f"Candidate: `{sha}`." if sha else "Candidate: no PR SHA recorded (issue assignment)."
        item = self.advisory(f"evidence item read on #{number}", lambda: self.github.item(number))
        if item is None or item.kind == "pr":
            result = (self.advisory(f"candidate evidence read on #{number}",
                                   lambda: self.github.candidate_evidence(number, sha)) if sha else None)
            review, ci = result or ("unavailable", "unavailable")
            evidence += f" Review decision: {review}. CI for this SHA: {ci}."
        links = f"[Claim]({lease['url']})"
        if outcome:
            links += f" · [Outcome]({outcome['url']})"
        else:
            links += " · No outcome was reported."
        if stops:
            labels = ", ".join(f"`{label}`" for label in stops)
            triggers = ", ".join(f"`{label}`" for label in outcome["transition"]["triggers"])
            resume = f"Remove the stop label(s) {labels}, then apply a trigger to resume {lease['agent']}: {triggers}."
        else:
            command = (f"ub-agent retry --number {number} --agent {lease['agent']} "
                       f"--reason {json.dumps('Human resolved the blocker')}")
            triggers = ", ".join(f"`{label}`" for label in lease.get("triggers", ()))
            resume = f"After resolving the blocker, run:\n\n```sh\n{command}\n```"
            if triggers:
                resume += f"\n\nRestore a matching trigger if absent: {triggers}; remove any stop label."
        extra = ""
        if lease.get("unreported") or outcome is None:
            extra = (f"\n\nLauncher host: `{lease.get('host') or socket.gethostname()}`. "
                     f"Run log directory: `{lease.get('log_dir') or 'unavailable'}`.")
        reason = " ".join(summary.split())
        self.github.create_comment(number,
            f"{marker}\n**Action needed**\n\n{reason}\n\n{evidence}\n\n{links}{extra}\n\n{resume}\n")
