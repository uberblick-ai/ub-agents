"""Advisory comment presentation, isolated from coordination authority."""

import json
import socket

from .records import own_comment, records

ACTION_MARKER = "<!-- ub-agent:action-needed "


class Notices:
    def __init__(self, github, actor, output=print):
        self.github, self.actor, self.output = github, actor, output

    def advisory(self, operation, action):
        try:
            return action()
        except Exception as exc:
            self.output(f"Advisory {operation} failed: {' '.join(str(exc).split())}")
            return None

    def minimize(self, comment):
        if not comment.get("isMinimized"):
            self.advisory(f"minimize comment {comment['id']}",
                          lambda: self.github.minimize_comment(comment))

    def resumed(self, number):
        def minimize_actions():
            for comment in self.github.comments(number):
                if own_comment(comment, self.actor) and comment.get("body", "").startswith(ACTION_MARKER):
                    self.minimize(comment)
        self.advisory(f"resume notices on #{number}", minimize_actions)

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
            for comment in comments:
                if comment["id"] in outdated:
                    self.minimize(comment)
        self.advisory(f"superseded records on #{number}", minimize_records)

    def released(self, lease, outcome, summary, parking_outcome=None):
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
        if lease["result"] == "blocked" or parked:
            self.advisory(f"Action needed post on #{target}",
                          lambda: self.post_action(target, lease, reported, summary, stops if parked else ()))

    def post_action(self, number, lease, outcome, summary, stops):
        marker = f"{ACTION_MARKER}{lease['run']} -->"
        comments = self.github.comments(number)
        if any(own_comment(c, self.actor) and c.get("body", "").startswith(marker) for c in comments):
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
