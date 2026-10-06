#!/usr/bin/env python3
"""Render the delivery-review report and slides from collect.py data and the reviewer's notes.

    python3 render.py data.json notes.json OUT_DIR

Writes OUT_DIR/report.html and OUT_DIR/slides.html: self-contained pages with no
scripts beyond slide navigation. Standard library only.
"""

import datetime as dt
import html
import json
import sys
from pathlib import Path

ROLE = {"issue-preparer": "P", "implementer": "I", "reviewer": "R", "integrator": "G"}
FORWARD = {"prepared", "handed-off", "approved", "merged"}
HANDED = {"maintainer-merge"}
BACK = {"changes-requested"}
LEVERS = {
    "authority": "Clearer authority",
    "wording": "Better wording",
    "fewer-instructions": "Fewer instructions",
    "autonomy": "More autonomy",
}

STYLE = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: one reading column; numbers first, then lessons, then the per-item ledger. */
:root {
  --bg: #f6f7f8; --panel: #ffffff; --fg: #1d2329; --muted: #5d6874; --line: #dde2e7;
  --accent: #2f6f8f; --ok: #3f8a5a; --back: #b7791f; --fail: #c4473f; --human: #6d5bb5;
  --ok-bg: #e5f2e9; --back-bg: #f8edd9; --fail-bg: #f8e3e1; --human-bg: #ece8f7;
  --display: "Bricolage Grotesque", "Avenir Next", system-ui, sans-serif;
  --body: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #12161a; --panel: #1a2026; --fg: #e4e8ec; --muted: #9aa5b1; --line: #2c343c;
  --accent: #6fb3d2; --ok: #8fc29f; --back: #e0b462; --fail: #e8776f; --human: #b4a6ec;
  --ok-bg: #1f3327; --back-bg: #3a2f1a; --fail-bg: #3d2220; --human-bg: #2a2542; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #12161a; --panel: #1a2026; --fg: #e4e8ec; --muted: #9aa5b1; --line: #2c343c;
  --accent: #6fb3d2; --ok: #8fc29f; --back: #e0b462; --fail: #e8776f; --human: #b4a6ec;
  --ok-bg: #1f3327; --back-bg: #3a2f1a; --fail-bg: #3d2220; --human-bg: #2a2542; color-scheme: dark }
body { background: var(--bg); color: var(--fg); font: 15px/1.55 var(--body); }
a { color: var(--accent); }
h1, h2, h3 { font-family: var(--display); text-wrap: balance; line-height: 1.15; margin: 0; }
.num, td.n, .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.eyebrow { text-transform: uppercase; letter-spacing: .08em; font-size: 12px; color: var(--muted); font-weight: 600; }
.chip { display: inline-block; font: 500 11px/1 var(--mono); padding: 4px 5px; border-radius: 4px; margin: 1px; }
.c-ok { background: var(--ok-bg); color: var(--ok); }
.c-back { background: var(--back-bg); color: var(--back); }
.c-fail { background: var(--fail-bg); color: var(--fail); }
.c-human { background: var(--human-bg); color: var(--human); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; color: var(--muted); font-size: 13px; }
"""

REPORT_STYLE = """
.wrap { max-width: 1080px; margin: 0 auto; padding-inline: 20px; padding-block: 36px 64px; display: grid; gap: 40px; }
header { display: grid; gap: 10px; }
header h1 { font-size: clamp(28px, 4vw, 40px); }
.headline { font-size: 18px; max-width: 68ch; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; display: grid; gap: 2px; }
.tile .num { font-size: 26px; font-weight: 500; }
.tile .sub { color: var(--muted); font-size: 13px; }
section { display: grid; gap: 14px; min-width: 0; }
section > h2 { font-size: 22px; }
.lessons { display: grid; gap: 12px; }
.lesson { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px 18px; display: grid; gap: 8px; }
.lesson h3 { font-size: 17px; }
.lesson .change { max-width: 75ch; }
.lesson .meta { color: var(--muted); font-size: 13px; }
.tablebox { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th { text-align: left; font-weight: 600; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
th, td { padding: 9px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
td.n, th.n { text-align: right; white-space: nowrap; }
td.title { min-width: 220px; }
td.note { min-width: 240px; color: var(--muted); }
.strip { white-space: nowrap; }
ul.plain { margin: 0; padding-left: 18px; display: grid; gap: 6px; }
.retro { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 16px; }
footer { color: var(--muted); font-size: 13px; }
"""

SLIDE_STYLE = """
html, body { height: 100%; }
body { overflow: hidden; }
.deck { height: 100%; overflow-y: auto; scroll-snap-type: y mandatory; }
.slide { height: 100%; scroll-snap-align: start; display: grid; place-items: center; padding-inline: 16px; padding-block: 24px; box-sizing: border-box; }
.frame { width: min(100%, 1100px); aspect-ratio: 16 / 9; max-height: 100%; background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: clamp(18px, 4vw, 56px); box-sizing: border-box; display: grid; grid-template-rows: auto 1fr auto; gap: clamp(10px, 2vw, 24px); overflow: hidden; }
.frame h2 { font-size: clamp(22px, 3.6vw, 44px); }
.frame .foot { color: var(--muted); font-size: clamp(11px, 1.2vw, 14px); display: flex; justify-content: space-between; gap: 12px; }
.big { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: clamp(10px, 2vw, 28px); align-content: center; }
.big .num { font-size: clamp(30px, 5.5vw, 68px); line-height: 1; }
.big .sub { color: var(--muted); font-size: clamp(12px, 1.4vw, 16px); margin-top: 6px; }
.points { margin: 0; padding-left: 1.1em; display: grid; gap: clamp(8px, 1.6vw, 18px); align-content: center; font-size: clamp(14px, 2vw, 24px); }
.points .meta { display: block; color: var(--muted); font-size: .7em; }
.bars { display: grid; gap: clamp(8px, 1.4vw, 16px); align-content: center; font-size: clamp(12px, 1.6vw, 18px); }
.bar { display: grid; grid-template-columns: minmax(90px, 10em) 1fr auto; gap: 12px; align-items: center; }
.track { display: flex; height: clamp(14px, 2.2vw, 26px); border-radius: 4px; overflow: hidden; background: var(--bg); }
.track span { display: block; height: 100%; }
.s-ok { background: var(--ok); } .s-back { background: var(--back); } .s-fail { background: var(--fail); } .s-human { background: var(--human); }
.cover { align-content: center; display: grid; gap: 16px; }
.cover h1 { font-size: clamp(30px, 5vw, 64px); }
.cover p { font-size: clamp(15px, 2vw, 24px); max-width: 40ch; margin: 0; }
@media (prefers-reduced-motion: no-preference) { .deck { scroll-behavior: smooth; } }
"""

SLIDE_SCRIPT = """
<script>
(() => {
  const deck = document.querySelector('.deck');
  const slides = [...document.querySelectorAll('.slide')];
  const at = () => Math.round(deck.scrollTop / deck.clientHeight);
  const go = (i) => slides[Math.max(0, Math.min(slides.length - 1, i))].scrollIntoView();
  document.addEventListener('keydown', (e) => {
    if (['ArrowRight', 'ArrowDown', 'PageDown', ' '].includes(e.key)) { e.preventDefault(); go(at() + 1); }
    if (['ArrowLeft', 'ArrowUp', 'PageUp'].includes(e.key)) { e.preventDefault(); go(at() - 1); }
  });
})();
</script>
"""


def esc(value):
    return html.escape(str(value), quote=True)


def stamp(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%b %-d, %H:%M UTC")


def span(minutes):
    if minutes is None:
        return "–"
    if minutes < 90:
        return f"{minutes:.0f}m"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def tone(run):
    if run["result"] in FORWARD and run["accepted"]:
        return "ok"
    if run["result"] in HANDED:
        return "human"
    if run["result"] in BACK:
        return "back"
    return "fail"


def strip(runs):
    chips = []
    for run in runs:
        label = ROLE.get(run["agent"], (run["agent"] or "?")[:1].upper())
        tip = f'{run["agent"]} · {run["result"]} · {span(run["minutes"])}'
        chips.append(f'<a class="chip c-{tone(run)}" href="{esc(run["url"])}" title="{esc(tip)}">{label}</a>')
    return "".join(chips) or '<span class="eyebrow">outside the loop</span>'


def link(repo, number):
    return f'<a href="https://github.com/{esc(repo)}/issues/{int(number)}">#{int(number)}</a>'


LEGEND = """<div class="legend"><span><span class="chip c-ok">P</span> preparer
<span class="chip c-ok">I</span> implementer <span class="chip c-ok">R</span> reviewer
<span class="chip c-ok">G</span> integrator</span>
<span><span class="chip c-ok">·</span> moved forward</span><span><span class="chip c-back">·</span> sent back</span>
<span><span class="chip c-fail">·</span> blocked, retried or no report</span><span><span class="chip c-human">·</span> handed to a person</span></div>"""


def tiles(data):
    t = data["totals"]
    released = "".join(f' · released {r["tag"]}' for r in t["releases"])
    rows = [
        (t["prs_merged"], "PRs merged", f'{t["prs_merged_by_loop"]} by the integrator{released}'),
        (f'+{t["additions"]:,}', "lines added", f'−{t["deletions"]:,} removed · {t["files"]} files'),
        (t["issues_closed"], "issues closed", f'{t["issues_opened"]} opened · {t["prs_opened"]} PRs opened'),
        (t["runs"], "agent runs", f'{t["runs_accepted"]} accepted · {t["agent_hours"]}h agent time'),
        (f'{t["first_pass"]}/{t["deliveries"]}', "first pass", "each role ran once, no reset"),
        (t["resets"] + t["notices"], "human touches", f'{t["resets"]} resets · {t["notices"]} notices'),
    ]
    return rows


def report(data, notes):
    repo, t = data["repo"], data["totals"]
    items = notes.get("items", {})
    out = [f'<title>Delivery review {esc(data["until"][:10])}</title>', STYLE, REPORT_STYLE, "</style>", '<div class="wrap">']
    out.append(f"""<header><div class="eyebrow">{esc(repo)} · {esc(stamp(data["since"]))} to {esc(stamp(data["until"]))}</div>
<h1>Delivery review</h1><p class="headline">{esc(notes.get("headline", ""))}</p></header>""")
    out.append('<section><h2>Delivered</h2><div class="tiles">')
    for value, label, sub in tiles(data):
        out.append(f'<div class="tile"><span class="eyebrow">{esc(label)}</span><span class="num">{esc(value)}</span><span class="sub">{esc(sub)}</span></div>')
    out.append("</div></section>")

    if notes.get("lessons"):
        out.append('<section><h2>What to change</h2><div class="lessons">')
        for lesson in notes["lessons"]:
            evidence = " ".join(link(repo, n) for n in lesson.get("evidence", []))
            lever = LEVERS.get(lesson.get("lever"), lesson.get("lever", ""))
            out.append(f"""<div class="lesson"><span class="eyebrow">{esc(lever)}</span><h3>{esc(lesson["title"])}</h3>
<div class="change">{esc(lesson["change"])}</div>
<div class="meta">{esc(lesson.get("where", ""))}{" · " if lesson.get("where") else ""}{esc(lesson.get("cost", ""))}{" · " if evidence else ""}{evidence}</div></div>""")
        out.append("</div></section>")

    if notes.get("causes"):
        out.append('<section><h2>Where extra runs went</h2><div class="tablebox"><table><tr><th>Cause</th><th class="n">Cost</th><th>Items</th><th>State</th></tr>')
        for cause in notes["causes"]:
            out.append(f'<tr><td>{esc(cause["cause"])}</td><td class="n">{esc(cause["cost"])}</td><td>{" ".join(link(repo, n) for n in cause.get("items", []))}</td><td>{esc(cause.get("state", ""))}</td></tr>')
        out.append("</table></div></section>")

    out.append(f'<section><h2>Each delivery</h2>{LEGEND}<div class="tablebox"><table><tr><th>Item</th><th>Runs</th><th class="n">Extra</th><th class="n">Lines</th><th class="n">Lead</th><th class="n">Agent</th><th>Note</th></tr>')
    for d in sorted(data["deliveries"], key=lambda d: (not d["delivered"], -d["extra_runs"], d["key"])):
        note = items.get(str(d["key"]), "")
        prs = " ".join(f'<a href="{esc(p["url"])}">PR #{p["number"]}</a>' for p in d["prs"] if p["number"] != d["key"])
        lines = sum(p["additions"] + p["deletions"] for p in d["prs"])
        extras = []
        if d["resets"]:
            extras.append(f'{len(d["resets"])} reset')
        if d["retrospectives"]:
            extras.append(" ".join(f'<a href="{esc(r["url"])}">retro</a>' for r in d["retrospectives"]))
        if not d["delivered"]:
            extras.append("in flight")
        out.append(f"""<tr><td class="title"><a href="{esc(d["url"])}">#{d["key"]}</a> {esc(d["title"])}<br><span class="eyebrow">{prs} {" · ".join(extras)}</span></td>
<td class="strip">{strip(d["runs"])}</td><td class="n">{d["extra_runs"]}</td><td class="n">{lines:,}</td>
<td class="n">{span(d["lead_minutes"])}</td><td class="n">{span(d["agent_minutes"])}</td><td class="note">{esc(note)}</td></tr>""")
    out.append("</table></div></section>")

    out.append('<section><h2>Runs by role</h2><div class="tablebox"><table><tr><th>Role</th><th class="n">Runs</th><th class="n">Accepted</th><th>Results</th><th class="n">Time</th></tr>')
    for agent, row in sorted(data["by_agent"].items(), key=lambda kv: list(ROLE).index(kv[0]) if kv[0] in ROLE else 9):
        results = ", ".join(f"{k} {v}" for k, v in sorted(row["results"].items(), key=lambda kv: -kv[1]))
        out.append(f'<tr><td>{esc(agent)}</td><td class="n">{row["runs"]}</td><td class="n">{row["accepted"]}</td><td>{esc(results)}</td><td class="n">{span(row["minutes"])}</td></tr>')
    out.append("</table></div></section>")

    retro = data["retrospectives"]
    out.append("<section><h2>Retrospectives</h2>")
    for r in retro["in_window"]:
        out.append(f'<div class="retro"><span class="eyebrow">{esc(r["agent"])} · <a href="{esc(r["url"])}">board post</a></span><div>{esc(r["body"])}</div></div>')
    if not retro["in_window"] and not retro["errors"]:
        out.append("<p>No retrospectives were posted in this window.</p>")
    if retro["errors"]:
        out.append(f'<p>Boards not read: {esc(retro["errors"][0][:160])}</p>')
    out.append("</section>")

    out.append("<section><h2>Waiting on people</h2>")
    if data["waiting_on_people"]:
        out.append('<ul class="plain">' + "".join(f'<li><a href="{esc(w["url"])}">#{w["number"]}</a> {esc(w["title"])}</li>' for w in data["waiting_on_people"]) + "</ul>")
    else:
        out.append(f'<p>No open item carries {esc(", ".join(data["stop_labels"]) or "a stop label")}.</p>')
    out.append("</section>")
    out.append(f'<footer>Generated by the delivery-review skill from launcher records, PRs and issues in {esc(repo)}. Runs and lines count the whole life of each delivered item; the tiles count only this window.</footer></div>')
    return "\n".join(out)


def slides(data, notes):
    t, repo = data["totals"], data["repo"]
    window = f'{stamp(data["since"])} to {stamp(data["until"])}'
    frames = []

    def frame(title, body, n):
        frames.append(f'<section class="slide"><div class="frame"><h2>{esc(title)}</h2>{body}<div class="foot"><span>{esc(repo)} · {esc(window)}</span><span>{n}</span></div></div></section>')

    frames.append(f'<section class="slide"><div class="frame"><div></div><div class="cover"><span class="eyebrow">{esc(repo)}</span><h1>Delivery review</h1><p>{esc(notes.get("headline", ""))}</p></div><div class="foot"><span>{esc(window)}</span><span>1</span></div></div></section>')
    big = "".join(f'<div><div class="num">{esc(v)}</div><div class="sub">{esc(label)}<br>{esc(sub)}</div></div>' for v, label, sub in tiles(data)[:6])
    frame("What shipped", f'<div class="big">{big}</div>', 2)

    rows, most = [], max([row["runs"] for row in data["by_agent"].values()] or [1])
    for agent in [a for a in ROLE if a in data["by_agent"]]:
        tally = {"ok": 0, "back": 0, "fail": 0, "human": 0}
        for d in data["deliveries"]:
            for run in d["runs"]:
                if run["agent"] == agent and data["since"] <= (run["started"] or "") < data["until"]:
                    tally[tone(run)] += 1
        total = sum(tally.values()) or 1
        segs = "".join(f'<span class="s-{k}" style="width:{100 * v / most:.1f}%"></span>' for k, v in tally.items() if v)
        rows.append(f'<div class="bar"><span>{esc(agent)}</span><div class="track">{segs}</div><span class="num">{total}</span></div>')
    frame("Where the runs went", f'<div class="bars">{"".join(rows)}{LEGEND}</div>', 3)

    causes = notes.get("causes", [])[:4]
    if causes:
        points = "".join(f'<li>{esc(c["cause"])}<span class="meta">{esc(c["cost"])} · {esc(c.get("state", ""))}</span></li>' for c in causes)
        frame("What cost extra runs", f'<ul class="points">{points}</ul>', len(frames) + 1)
    lessons = notes.get("lessons", [])[:4]
    if lessons:
        points = "".join(f'<li>{esc(l["title"])}<span class="meta">{esc(LEVERS.get(l.get("lever"), ""))} · {esc(l.get("where", ""))}</span></li>' for l in lessons)
        frame("What to change", f'<ul class="points">{points}</ul>', len(frames) + 1)
    waiting = data["waiting_on_people"]
    nxt = notes.get("next") or [f'#{w["number"]} {w["title"]}' for w in waiting] or ["Nothing waits on a person."]
    frame("Next", '<ul class="points">' + "".join(f"<li>{esc(x)}</li>" for x in nxt[:5]) + "</ul>", len(frames) + 1)
    return "\n".join([f'<title>Delivery slides {esc(data["until"][:10])}</title>', STYLE, SLIDE_STYLE, "</style>", '<main class="deck">', *frames, "</main>", SLIDE_SCRIPT])


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__.strip().split("\n\n")[1])
    data = json.loads(Path(sys.argv[1]).read_text())
    notes = json.loads(Path(sys.argv[2]).read_text())
    out = Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.html").write_text(report(data, notes) + "\n")
    (out / "slides.html").write_text(slides(data, notes) + "\n")
    print(out / "report.html")
    print(out / "slides.html")


if __name__ == "__main__":
    main()
