#!/usr/bin/env python3
"""Collect one window of loop deliveries as JSON for the delivery-review skill.

Read-only. Uses `gh api` with repository-scoped REST endpoints, plus one GraphQL
read per retrospective board when GraphQL is available. Standard library only.

    python3 collect.py --repo OWNER/NAME [--since ISO] [--until ISO] > data.json
"""

import argparse
import base64
import datetime as dt
import json
import re
import subprocess
import sys

TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
RECORD = re.compile(r"<!-- ub-agents:v3 -->.*?```json\n(.*?)\n```", re.S)
NOTICE = "<!-- ub-agents:action-needed"
CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)


PATH = re.compile(r"(?<![\w.~])/(?:Users|home|srv|mnt|tmp|private|var|opt|root)/\S*")


def public(text, limit):
    """Shorten and drop local paths: reports may be shared, and boards are public."""
    return PATH.sub("<path>", " ".join((text or "").split()))[:limit]


def gh(path, **params):
    args = ["gh", "api", "--method", "GET", path]
    for key, value in params.items():
        args += ["-f", f"{key}={value}"]
    done = subprocess.run(args, capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"gh api {path}: {done.stderr.strip() or done.stdout.strip()}")
    return json.loads(done.stdout)


def pages(path, **params):
    """Page with explicit page numbers; Link headers are not portable to every proxy."""
    page = 1
    while True:
        rows = gh(path, per_page=100, page=page, **params)
        yield from rows
        if len(rows) < 100:
            return
        page += 1


def when(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None


def iso(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def minutes(start, end):
    return round((when(end) - when(start)).total_seconds() / 60, 1) if start and end else None


def inside(text, since, until):
    moment = when(text)
    return moment is not None and since <= moment < until


def config(repo):
    """Retrospective boards and stop labels from the repository's ub-agents.yaml."""
    try:
        text = base64.b64decode(gh(f"repos/{repo}/contents/ub-agents.yaml")["content"]).decode()
    except RuntimeError:
        return {}, []
    boards, agent = {}, None
    for line in text.splitlines():
        found = re.match(r"^  ([\w-]+):\s*$", line)
        if found:
            agent = found.group(1)
        found = re.match(r"^\s+retrospectives:\s*(\d+)", line)
        if found and agent:
            boards[agent] = int(found.group(1))
    stops = re.search(r"^stop-labels:\s*\[(.*?)\]", text, re.M)
    return boards, [s.strip() for s in stops.group(1).split(",")] if stops else []


def item_comments(repo, number):
    records, notices, other = [], [], []
    for comment in pages(f"repos/{repo}/issues/{number}/comments"):
        body, trusted = comment["body"] or "", comment["author_association"] in TRUSTED
        found = RECORD.search(body)
        if found and trusted:
            try:
                record = json.loads(found.group(1))
            except ValueError:
                continue
            record["url"] = comment["html_url"]
            records.append(record)
        elif body.startswith(NOTICE) and trusted:
            ask = body.split("**Action needed**", 1)[-1].strip().split("\n", 1)[0]
            notices.append({"created": comment["created_at"], "url": comment["html_url"], "ask": public(ask, 400)})
        elif comment["user"]["type"] != "Bot":
            other.append({"created": comment["created_at"], "url": comment["html_url"],
                          "author": comment["user"]["login"], "trusted": trusted,
                          "excerpt": public(body, 300)})
    return records, notices, other


def runs_from(records):
    """One row per run: the lease joined with its outcome; handoff copies collapse by run id."""
    leases, outcomes, resets = {}, {}, []
    for record in records:
        kind, run = record.get("kind"), record.get("run")
        if kind == "lease":
            leases.setdefault(run, record)
        elif kind == "outcome":
            outcomes.setdefault(run, record)
        elif kind == "reset":
            resets.append({"agent": record.get("agent"), "created": record.get("created"),
                           "reason": public(record.get("summary"), 300), "url": record.get("url")})
    runs = []
    for run in leases.keys() | outcomes.keys():
        lease, outcome = leases.get(run, {}), outcomes.get(run, {})
        if lease.get("result") == "withdrawn" or lease.get("state") == "withdrawn":
            continue
        status = outcome.get("status")
        result = outcome.get("outcome") if status == "success" else status
        runs.append({
            "run": run[:8],
            "agent": lease.get("agent") or outcome.get("agent"),
            "runtime": lease.get("runtime") or outcome.get("runtime"),
            "item": lease.get("assignment") or outcome.get("assignment"),
            "started": lease.get("created") or outcome.get("created"),
            "ended": outcome.get("created"),
            "minutes": minutes(lease.get("created"), outcome.get("created")),
            "result": result or lease.get("result") or "no report",
            "accepted": outcome.get("accepted"),
            "attempt": lease.get("attempt"),
            "denials": len(outcome.get("denials") or []) + int(outcome.get("denials_omitted") or 0),
            "denied": [public(f'{d.get("tool")}: {d.get("command") or ""}', 160) for d in outcome.get("denials") or []],
            "actions": [public(a, 300) for a in outcome.get("actions") or ([outcome["action"]] if outcome.get("action") else [])],
            "summary": public(outcome.get("summary") or lease.get("summary"), 600),
            "url": outcome.get("url") or lease.get("url"),
        })
    runs.sort(key=lambda r: r["started"] or "")
    return runs, sorted(resets, key=lambda r: r["created"] or "")


def retrospectives(repo, boards):
    owner, name = repo.split("/")
    query = """query($owner:String!,$name:String!,$number:Int!,$after:String){
      repository(owner:$owner,name:$name){discussion(number:$number){comments(first:100,after:$after){
        pageInfo{hasNextPage endCursor}
        nodes{url createdAt authorAssociation author{login} body}}}}}"""
    found, errors = [], []
    for agent, number in sorted(boards.items()):
        after = None
        while True:
            args = ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}",
                    "-f", f"name={name}", "-F", f"number={number}"]
            if after:
                args += ["-f", f"after={after}"]
            done = subprocess.run(args, capture_output=True, text=True)
            if done.returncode:
                errors.append(f"{agent} board #{number}: {(done.stderr or done.stdout).strip().splitlines()[-1][:160]}")
                break
            data = json.loads(done.stdout)["data"]["repository"]["discussion"]["comments"]
            for node in data["nodes"]:
                if node["authorAssociation"] in TRUSTED:
                    found.append({"agent": agent, "board": number, "created": node["createdAt"],
                                  "url": node["url"], "body": public(node["body"], 1200),
                                  "items": sorted({int(n) for n in re.findall(r"(?:#|/(?:issues|pull)/)(\d+)", node["body"])})})
            if not data["pageInfo"]["hasNextPage"]:
                break
            after = data["pageInfo"]["endCursor"]
    return found, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--until", help="window end, ISO 8601 UTC (default now)")
    parser.add_argument("--since", help="window start (default 24 hours before --until)")
    args = parser.parse_args()
    until = when(args.until) if args.until else dt.datetime.now(dt.timezone.utc)
    since = when(args.since) if args.since else until - dt.timedelta(hours=24)
    repo = args.repo
    boards, stop_labels = config(repo)

    touched = list(pages(f"repos/{repo}/issues", state="all", since=iso(since)))
    merged, closed, opened = {}, {}, {"issues": 0, "prs": 0}
    for row in touched:
        if inside(row["created_at"], since, until):
            opened["prs" if "pull_request" in row else "issues"] += 1
        if "pull_request" in row:
            if inside(row["pull_request"].get("merged_at"), since, until):
                merged[row["number"]] = row
        elif inside(row.get("closed_at"), since, until):
            closed[row["number"]] = row

    # Items whose runs started in the window but are not delivered yet.
    active = set()
    for comment in pages(f"repos/{repo}/issues/comments", since=iso(since)):
        found = RECORD.search(comment["body"] or "")
        if found and comment["author_association"] in TRUSTED:
            try:
                record = json.loads(found.group(1))
            except ValueError:
                continue
            if record.get("kind") == "lease" and inside(record.get("created"), since, until):
                active.add(int(comment["issue_url"].rsplit("/", 1)[1]))

    cache = {}

    def load(number):
        if number not in cache:
            issue = gh(f"repos/{repo}/issues/{number}")
            records, notices, other = item_comments(repo, number)
            cache[number] = (issue, records, notices, other)
        return cache[number]

    def is_issue(number):
        try:
            return "pull_request" not in load(number)[0]
        except RuntimeError:
            return False

    # Group each issue with the PRs that close it or that its runs handed off to.
    groups, owner_of = {}, {}

    def attach(issue_number, pr_number=None):
        group = groups.setdefault(issue_number, {"issue": None, "prs": set()})
        if pr_number:
            group["prs"].add(pr_number)
            owner_of[pr_number] = issue_number

    for number in sorted(set(merged) | set(closed) | active):
        issue, records, _, _ = load(number)
        if "pull_request" in issue:
            targets = [int(n) for n in CLOSES.findall(issue.get("body") or "")]
            targets = [n for n in targets if is_issue(n)]
            if targets:
                for target in targets:
                    attach(target, number)
            else:
                attach(number)
                groups[number]["standalone_pr"] = True
                groups[number]["prs"].add(number)
        else:
            attach(number)
            for record in records:
                if record.get("handoff"):
                    attach(number, int(record["handoff"]))

    retros, retro_errors = retrospectives(repo, boards) if boards else ([], ["no retrospectives boards in ub-agents.yaml"])

    deliveries = []
    for key, group in sorted(groups.items()):
        numbers = sorted({key} | group["prs"])
        records, notices, other, prs = [], [], [], []
        head, _, _, _ = load(key)
        for number in numbers:
            issue, item_records, item_notices, item_other = load(number)
            records += item_records
            notices += [dict(n, item=number) for n in item_notices]
            other += [dict(o, item=number) for o in item_other]
            if "pull_request" in issue:
                pr = gh(f"repos/{repo}/pulls/{number}")
                prs.append({"number": number, "title": pr["title"], "url": pr["html_url"],
                            "state": "merged" if pr.get("merged_at") else pr["state"],
                            "created": pr["created_at"], "merged": pr.get("merged_at"),
                            "merged_by": (pr.get("merged_by") or {}).get("login"),
                            "additions": pr["additions"], "deletions": pr["deletions"],
                            "files": pr["changed_files"], "commits": pr["commits"]})
        runs, resets = runs_from(records)
        merged_by_loop = {r["item"] for r in runs if r["agent"] == "integrator" and r["result"] == "merged"}
        for pr in prs:
            pr["merged_by_loop"] = pr["number"] in merged_by_loop
        is_issue = not group.get("standalone_pr")
        finished = [p["merged"] for p in prs if p["merged"]] + ([head.get("closed_at")] if is_issue and head.get("closed_at") else [])
        done_at = max(finished) if finished else None
        delivered = any(inside(p["merged"], since, until) for p in prs) or (is_issue and inside(head.get("closed_at"), since, until))
        agents = {r["agent"] for r in runs}
        good = {r["agent"] for r in runs if r["accepted"]}
        deliveries.append({
            "key": key,
            "kind": "issue" if is_issue else "pr",
            "title": head["title"],
            "url": head["html_url"],
            "state": head["state"],
            "state_reason": head.get("state_reason"),
            "labels": [label["name"] for label in head.get("labels", [])],
            "milestone": (head.get("milestone") or {}).get("title"),
            "delivered": delivered,
            "created": head["created_at"],
            "done": done_at,
            "lead_minutes": minutes(head["created_at"], done_at),
            "loop_minutes": minutes(runs[0]["started"], done_at) if runs else None,
            "agent_minutes": round(sum(r["minutes"] or 0 for r in runs), 1),
            "prs": prs,
            "runs": runs,
            "extra_runs": max(0, len(runs) - len(agents)),
            "failed_runs": sum(1 for r in runs if not r["accepted"]),
            "changes_requested": sum(1 for r in runs if r["result"] == "changes-requested"),
            "roles_without_success": sorted(agents - good),
            "resets": resets,
            "notices": notices,
            "other_comments": other,
            "denials": sum(r["denials"] for r in runs),
            "retrospectives": [r for r in retros if set(r["items"]) & set(numbers)],
        })

    window_runs = [r for d in deliveries for r in d["runs"] if inside(r["started"], since, until)]
    by_agent = {}
    for run in window_runs:
        row = by_agent.setdefault(run["agent"], {"runs": 0, "accepted": 0, "minutes": 0.0, "results": {}})
        row["runs"] += 1
        row["accepted"] += bool(run["accepted"])
        row["minutes"] = round(row["minutes"] + (run["minutes"] or 0), 1)
        row["results"][run["result"]] = row["results"].get(run["result"], 0) + 1

    waiting = [{"number": row["number"], "title": row["title"], "url": row["html_url"]}
               for row in pages(f"repos/{repo}/issues", state="open", labels=stop_labels[0])] if stop_labels else []
    releases = [{"tag": r["tag_name"], "url": r["html_url"], "published": r["published_at"]}
                for r in gh(f"repos/{repo}/releases", per_page=20) if inside(r.get("published_at"), since, until)]
    delivered = [d for d in deliveries if d["delivered"]]
    merged_prs = list({p["number"]: p for d in deliveries for p in d["prs"] if inside(p["merged"], since, until)}.values())

    json.dump({
        "repo": repo,
        "since": iso(since),
        "until": iso(until),
        "totals": {
            "prs_merged": len(merged_prs),
            "prs_merged_by_loop": sum(p["merged_by_loop"] for p in merged_prs),
            "additions": sum(p["additions"] for p in merged_prs),
            "deletions": sum(p["deletions"] for p in merged_prs),
            "files": sum(p["files"] for p in merged_prs),
            "commits": sum(p["commits"] for p in merged_prs),
            "issues_closed": sum(1 for n in closed),
            "issues_completed": sum(1 for row in closed.values() if row.get("state_reason") == "completed"),
            "issues_opened": opened["issues"],
            "prs_opened": opened["prs"],
            "releases": releases,
            "runs": len(window_runs),
            "runs_accepted": sum(1 for r in window_runs if r["accepted"]),
            "agent_hours": round(sum(r["minutes"] or 0 for r in window_runs) / 60, 1),
            "first_pass": sum(1 for d in delivered if d["runs"] and not d["extra_runs"] and not d["resets"]),
            "outside_loop": sum(1 for d in delivered if not d["runs"]),
            "deliveries": len(delivered),
            "in_flight": len(deliveries) - len(delivered),
            "resets": sum(1 for d in deliveries for r in d["resets"] if inside(r["created"], since, until)),
            "notices": sum(1 for d in deliveries for n in d["notices"] if inside(n["created"], since, until)),
        },
        "by_agent": by_agent,
        "waiting_on_people": waiting,
        "stop_labels": stop_labels,
        "retrospectives": {"boards": boards, "errors": retro_errors,
                           "in_window": [r for r in retros if inside(r["created"], since, until)]},
        "deliveries": deliveries,
    }, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
