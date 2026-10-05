"""Display-only run reduction from already-read coordination records."""

from .records import seconds
from .denials import denial_fields


def display_run(row):
    """Keep reduction identities and precedence metadata out of the snapshot."""
    return {key: row[key] for key in (
        "time", "agent", "summary", "host", "outcome", "acceptance", "human_blocker",
        "result", "state", "expires", "rejection", "denials", "denials_omitted") if key in row}


def run_key(row):
    return row.get("assignment"), row.get("agent"), row.get("run")


def merge_record(runs, record, stop_labels):
    if record["kind"] not in {"lease", "outcome"}:
        return
    key = run_key(record)
    row = next((row for row in runs if run_key(row) == key), None)
    if row is None:
        row = {key: record.get(key) for key in ("assignment", "agent", "run")}
        runs.append(row)
    if record["kind"] == "lease":
        row.update(claim_time=record.get("created"), expires=record.get("expires"),
                   state=record.get("state"), lease_result=record.get("result"),
                   lease_summary=record.get("summary"), claim_host=record.get("host"))
    else:
        row.pop("denials", None)
        row.pop("denials_omitted", None)
        row.update(denial_fields(record))
        finalized = bool(record.get("accepted") and record.get("transition_complete"))
        row.update(outcome_time=record.get("created"), report_result=record["status"],
                   report_summary=record["summary"], outcome=record.get("outcome"),
                   outcome_host=record.get("host"), rejection=record.get("rejected"),
                   acceptance=("rejected" if record.get("rejected") else "finalized" if finalized else
                               "accepted" if record.get("accepted") else "unaccepted"),
                   completed=finalized, target=record.get("handoff") or record["assignment"],
                   human_blocker=(sorted(set(record.get("transition", {}).get("add", ()))
                                         .intersection(stop_labels)) if finalized else None))
    row.update(time=row.get("outcome_time") or row.get("claim_time"),
               host=row.get("claim_host") or row.get("outcome_host"),
               result=row.get("lease_result") or row.get("report_result"),
               summary=row.get("lease_summary") or row.get("report_summary") or "")


def sort_runs(runs):
    runs.sort(key=lambda row: (seconds(row["time"]) if row.get("time") else 0, str(run_key(row))))


def observed_blockers(history, item, stop_labels):
    for run in history["runs"]:
        if run.get("completed") and run.get("target") == item.number:
            run["human_blocker"] = sorted(item.labels.intersection(stop_labels))
