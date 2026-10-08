"""Advisory comment presentation, isolated from coordination authority."""

import json
import re
import socket

from .errors import LostOwnership
from .records import MARKER, declared_transition, lease_by_id, records, reported_actions, resolve_transition, same_handoff
from .trust import LauncherTrust

ACTION_MARKER = "<!-- ub-agents:action-needed "


INLINE_CODE = re.compile(r"(?<![\\`])`[^`]+`(?!`)")
OPTION_COMMAND = re.compile(r"^(.*):\s+(`[^`]+`)$")


def ask_markdown(ask):
    """Keep single-backtick code; escape all other Markdown in an ask."""
    parts, start = [], 0
    for match in INLINE_CODE.finditer(ask):
        parts.extend((escape_ask(ask[start:match.start()]), match[0]))
        start = match.end()
    return ''.join(parts) + escape_ask(ask[start:])


def escape_ask(text):
    escaped = re.sub(r"([\\`*_{}\[\]()#+!|&<>~])", r"\\\1", text)
    return re.sub(r'^(\s*)(-|\d+\.)(?=\s)', lambda m: m[1] + m[2][:-1] + '\\' + m[2][-1], escaped)


def notice_reason(summary, limit=300):
    sentence = re.split(r'(?<=[.!?])\s+', ' '.join(summary.split()), maxsplit=1)[0]
    return sentence if len(sentence) <= limit else sentence[:limit - 1].rstrip() + '…'


def retry_command(number, agent):
    return (f"ub-agents retry {number} --agent {agent} "
            f"--reason {json.dumps('Human resolved the blocker')}")


def outcome_resume(number, agent, steps, *, is_pr=False):
    if is_pr:
        return (f"Merging or closing #{number} finishes this item; nothing else is needed.\n\n"
                f"<details>\n<summary>To send it back to {agent} instead</summary>\n\n"
                f"{steps}\n\n</details>")
    return f"Then resume {agent}:\n\n{steps}"


def action_body(marker, actions, details, *, options=(), reason='', resume=''):
    asks = [ask_markdown(ask.strip()) for ask in actions]
    lead = f"**{asks[0]}**" if len(asks) == 1 else "\n".join(f"- **{ask}**" for ask in asks)
    if options:
        lead = ask_markdown(notice_reason(reason)) + (f"\n\n{lead}" if asks else '')
        lead += '\n\nTo unblock, do one of:\n\n'
        for index, option in enumerate(options, 1):
            option = option.strip()
            command = OPTION_COMMAND.fullmatch(option)
            label = command[1].rstrip() if command else option
            lead += f"{index}. {ask_markdown(label)}" + (' (recommended)' if index == 1 else '') + '\n'
            if command:
                indent = ' ' * (len(str(index)) + 2)
                lead += f"\n{indent}```sh\n{indent}{command[2][1:-1]}\n{indent}```\n"
            lead += '\n'
    if resume:
        lead = lead.rstrip() + f'\n\n{resume}'
    return (f"{marker}\n**Action needed**\n\n{lead}\n\n"
            "<details>\n<summary>Reasoning and evidence</summary>\n\n"
            f"{details}\n\n</details>\n")


class Notices:
    def __init__(self, github, actor, output=print, trusted=None, on_action=None):
        self.github, self.actor, self.output = github, actor, output
        self.trusted = trusted or LauncherTrust(github)
        self._approval_attempted = set()
        self.on_action = on_action

    def remember(self, number, comment):
        login = (comment.get("user") or {}).get("login") if comment else None
        if self.on_action is not None and (comment is None or
                isinstance(login, str) and login.casefold() == (self.actor or "").casefold()):
            self.on_action(number, comment)

    def comments(self, number):
        trusted = self.trusted.observation()
        return [c for c in self.github.comments(number)
                if (c.get("body") or "").startswith((MARKER, ACTION_MARKER))
                and trusted(c.get("user"))]

    def post_once(self, number, text, marker):
        # As with claims, simultaneous posters elect the lowest comment ID.
        # A loser removes only the advisory comment it just posted; durable
        # coordination records are never deleted.
        created = self.github.create_comment(number, text)
        self.remember(number, created)
        matches = [c for c in self.comments(number) if c["body"].startswith(marker)]
        if matches and min(c["id"] for c in matches) < created["id"]:
            self.github.delete_comment(created["id"])
            self.remember(number, None)
            self.remember(number, min(matches, key=lambda c: c["id"]))

    def advisory(self, operation, action):
        try:
            return action()
        except LostOwnership:
            raise
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
        self.remember(number, None)
        def minimize_actions():
            self.minimize([comment for comment in self.comments(number)
                           if (comment.get("body") or "").startswith(ACTION_MARKER)])
        self.advisory(f"resume notices on #{number}", minimize_actions)
        self._approval_attempted = {key for key in self._approval_attempted if key[0] != number}

    def approval(self, number, check, stops, triggers):
        if not stops:
            return
        comments = self.comments(number)
        # Claims/resets define a new parking episode even if advisory notice
        # minimization failed. Presentation state must not suppress a later gate.
        epoch = max((r["id"] for r in records(comments)
                     if r["assignment"] == number and r["kind"] in {"lease", "reset"}), default=0)
        key = (number, check.gate_key, epoch)
        if key in self._approval_attempted:
            return
        marker = f"{ACTION_MARKER}approval-{check.gate_key}-{epoch} -->"
        existing = next((c for c in comments if (c.get("body") or "").startswith(marker)), None)
        if existing:
            self.remember(number, existing)
            self._approval_attempted.add(key)
            return
        # Failed writes are advisory and are not retried for this gate in this
        # session. A successfully posted notice also deduplicates after restart.
        self._approval_attempted.add(key)
        labels = ", ".join(f"`{label}`" for label in stops)
        trigger_text = ", ".join(f"`{label}`" for label in triggers)
        if check.gate == "start":
            action = "Maintainer: authorize starting this item and clear its human hold."
            resume = (f"A maintainer must remove the stop label(s) {labels} and re-apply a trigger label: "
                      f"{trigger_text}. An approval alone does not start work.")
        elif check.gate == "head":
            action = "Maintainer: approve this PR's current head and clear its human hold."
            resume = (f"A maintainer must run `ub-agents approve {number}` or submit an approving "
                      f"review of the current head; then remove the stop label(s) {labels}. "
                      "Re-applying a trigger label does not approve a head.")
        else:
            action = "Maintainer: approve the updated input and clear its human hold."
            resume = (f"A maintainer must re-apply a trigger label ({trigger_text}), or run "
                      f"`ub-agents approve {number}`; then remove the stop label(s) {labels}.")
        self.advisory(f"approval stop label on #{number}", lambda: self.github.add_labels(number, stops))
        self.advisory(f"Action needed post on #{number}", lambda: self.post_once(
            number, action_body(marker, [action], check.reason, resume=f"Then resume:\n\n{resume}"), marker))

    def superseded(self, number, agent, run):
        def minimize_records():
            comments = self.comments(number)
            history = [r for r in records(comments)
                       if r["agent"] == agent and r["kind"] in {"lease", "outcome"}]
            current = [r["id"] for r in history if r["run"] == run]
            if not current:
                return
            latest = {r["kind"]: r for r in history}
            outdated = {r["id"] for r in history
                        if r["id"] < min(current) and r["run"] != latest[r["kind"]]["run"]}
            self.minimize([comment for comment in comments if comment["id"] in outdated])
        self.advisory(f"superseded records on #{number}", minimize_records)

    def election_lost(self, lease):
        self.advisory(f"withdrawn election lease on #{lease['assignment']}", lambda:
                      self.minimize([c for c in self.comments(lease["assignment"])
                                     if c["id"] == lease["id"]]))

    def superseded_candidates(self, outcome):
        number = outcome["handoff"]

        def minimize_records():
            comments = self.comments(number)
            history = records(comments)
            copies = [r for r in history if r["kind"] == "outcome"
                      and r["lease_id"] == outcome["lease_id"] and same_handoff(r, outcome)
                      and r.get("candidate_sha") == outcome["candidate_sha"] and r["accepted"]]
            if not copies:
                return
            cutoff = max(r["id"] for r in copies)
            latest_action = max((c for c in comments if c["body"].startswith(ACTION_MARKER)),
                                key=lambda c: c["id"], default=None)
            linked = {r["id"] for r in history if latest_action and r["url"]
                      and f"]({r['url']})" in latest_action["body"]}
            outdated = set()
            for record in history:
                sha = record.get("candidate_sha") or record.get("assignment_sha")
                approval_withdrawal = (record["kind"] == "lease" and record["state"] == "withdrawn"
                                       and record["attempt_effect"] == "unchanged")
                if (record["kind"] in {"lease", "outcome"} and record["id"] < cutoff
                        and sha and sha != outcome["candidate_sha"]
                        and record["run"] != outcome["run"] and not approval_withdrawal
                        and record["id"] not in linked):
                    outdated.add(record["id"])
            self.minimize([comment for comment in comments if comment["id"] in outdated])

        self.advisory(f"superseded candidates on #{number}", minimize_records)

    def released(self, lease, outcome, summary, parking_outcome=None, max_attempts=None):
        reported = parking_outcome or outcome
        target = lease["assignment"]
        transition = reported.get("transition", {}) if reported else {}
        if transition:
            source = lease if reported["lease_id"] == lease["id"] else lease_by_id(
                records(self.comments(reported["assignment"])), reported["lease_id"])
            transition = resolve_transition(transition, declared_transition(source, reported["outcome"]))
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
                          lambda: self.post_action(target, lease, reported, summary, stops if parked else (),
                                                   transition.get("triggers", ())))
        # A parking handoff can post a new notice. Preserve its evidence rather
        # than the links of the notice it just superseded.
        if (lease["result"] == "success" and reported and reported.get("accepted")
                and not reported.get("rejected") and reported.get("handoff") and reported.get("candidate_sha")):
            self.superseded_candidates(reported)

    def post_action(self, number, lease, outcome, summary, stops, resume_triggers=()):
        marker = f"{ACTION_MARKER}{lease['run']} -->"
        comments = self.comments(number)
        existing = next((c for c in comments if (c.get("body") or "").startswith(marker)), None)
        history = records(comments)
        anchors = [r["id"] for r in history if r["run"] == lease["run"]
                   or (outcome and r["run"] == outcome["run"])]
        if anchors and any(r["kind"] in {"lease", "reset"} and r["id"] > max(anchors) for r in history):
            return  # A later claim/reset already resumed this item.
        if existing:
            self.remember(number, existing)
            return
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
            triggers = ", ".join(f"`{label}`" for label in resume_triggers)
            resume = f"Remove the stop label(s) {labels}, then apply a trigger to resume {lease['agent']}: {triggers}."
        else:
            command = retry_command(number, lease['agent'])
            triggers = ", ".join(f"`{label}`" for label in lease.get("triggers", ()))
            resume = f"```sh\n{command}\n```"
            if triggers:
                resume += f"\n\nRestore a matching trigger if absent: {triggers}; remove any stop label."
        resume = (f"{resume}\n\n"
                  f"Use these steps only when resuming the same role (`{lease['agent']}`). "
                  "If a different role must act next, follow the project's documented correction "
                  "or handoff route instead.")
        resume = outcome_resume(number, lease['agent'], resume, is_pr=item is not None and item.kind == 'pr')
        extra = ""
        if lease.get("unreported") or outcome is None:
            extra = (f"\n\nLauncher host: `{lease.get('host') or socket.gethostname()}`. "
                     f"Run log directory: `{lease.get('log_dir') or 'unavailable'}`.")
        actions = reported_actions(outcome) if outcome and not outcome.get("rejected") else []
        options = outcome.get("options", ()) if outcome and not outcome.get("rejected") else ()
        if not actions and not options:
            actions = ["Maintainer: review the blocker details and decide the next step."]
        self.post_once(number, action_body(marker, actions,
            f"{summary.strip()}\n\n{evidence}\n\n{links}{extra}",
            options=options, reason=summary, resume=resume), marker)
