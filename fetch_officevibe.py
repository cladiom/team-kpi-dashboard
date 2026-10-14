#!/usr/bin/env python3
"""
Build the team-health snapshot from the published Workleap export.

Another team already publishes the Officevibe scores as weekly JSON, so this
reads that rather than talking to Workleap: no API key, no browser, no
credentials of any kind. The export is keyed by ISO week, Monday to Sunday,
which is the same spine the rest of this dashboard uses — the two line up
without any date juggling.

    python3 fetch_officevibe.py                    # url from config.json
    python3 fetch_officevibe.py --weeks 26
    python3 fetch_officevibe.py --file sample.json # a copy on disk

Writes engagement.json next to index.html.

Suppressed weeks
----------------
Officevibe withholds a week's scores when too few people answered to keep them
anonymous. Those arrive as nulls with "suppressed": true, and are published as
nulls here too. That is not missing data — it is data that exists and may not
be shown — so it is counted and reported separately rather than quietly looking
like an outage.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# What the export calls each figure, and what we call it.
#
# Participants is the team's headcount — everyone the survey goes to — not
# `est_participants`, which is participation x headcount and so just restates
# the rate as a count. The spreadsheet's row is constant week to week, which is
# the giveaway: it is the denominator, not the response.
FIELDS = {
    "ovParticipants": "headcount",
    "ovParticipationRate": "participation_pct",
    "ovEnps": "enps",
    "ovEngagement": "engagement_score",
}

METRIC_DEFS = [
        {"key": "ovParticipants", "label": "# of participants",
         "short": "Participants", "unit": "", "section": "OfficeVibe Engagement",
         "mode": "weekly"},
        {"key": "ovParticipationRate", "label": "Participation rate",
         "short": "Participation", "unit": "%", "section": "OfficeVibe Engagement",
         "mode": "weekly"},
        {"key": "ovEnps", "label": "eNPS score", "short": "eNPS", "unit": "",
         "section": "OfficeVibe Engagement", "mode": "weekly"},
        {"key": "ovEngagement", "label": "Engagement score",
         "short": "Engagement", "unit": "", "section": "OfficeVibe Engagement",
         "mode": "weekly"},
]

# The scores Officevibe withholds when too few people answered. Headcount is
# not one of them, so a suppressed week still reports its team size.
SCORE_FIELDS = ("ovParticipationRate", "ovEnps", "ovEngagement")


def fetch(url, timeout):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"null"), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} from {url}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def build_weeks(count, tz):
    """Monday 00:00 to the following Monday, newest first — as everywhere else."""
    today = datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())
    out = []
    for i in range(count):
        start = monday - timedelta(days=7 * i)
        nxt = start + timedelta(days=7)
        shown_end = start + timedelta(days=6)
        iy, iw, _ = start.isocalendar()
        end_label = f"{shown_end.day:02d} {MONTHS[shown_end.month - 1]}"
        if shown_end.year != start.year:
            end_label += f" {shown_end.year}"
        out.append({
            "key": f"{iy}-W{iw:02d}",
            "isoLabel": f"W{iw:02d}",
            "isoYear": iy,
            "startLabel": f"{start.day:02d} {MONTHS[start.month - 1]}",
            "endLabel": end_label,
            "year": start.year,
            "startMs": int(datetime(start.year, start.month, start.day,
                                    tzinfo=tz).timestamp() * 1000),
            "endMs": int(datetime(nxt.year, nxt.month, nxt.day,
                                  tzinfo=tz).timestamp() * 1000),
            "current": i == 0,
            "partial": i == 0,
            "metrics": {},
        })
    return out


def week_key(row):
    """
    The export's own week, checked against its start date.

    `period` is already an ISO week string, but it is derived data; the start
    date is the fact. Recomputing from it catches a file that was generated
    with a different week convention instead of silently misfiling a year of
    numbers by one week.
    """
    start = row.get("start")
    if start:
        try:
            d = date.fromisoformat(start)
            iy, iw, _ = d.isocalendar()
            return f"{iy}-W{iw:02d}"
        except ValueError:
            pass
    return row.get("period")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "engagement.json"))
    ap.add_argument("--url", default=None, help="override officevibe.url")
    ap.add_argument("--file", default=None, help="read a local copy instead")
    ap.add_argument("--weeks", type=int, default=None)
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    ocfg = cfg.get("officevibe") or {}
    tz = ZoneInfo(cfg.get("timezone") or "Europe/Berlin")
    count = max(1, min(208, args.weeks or int(cfg.get("weeks") or 12)))

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            src = json.load(fh)
        where = args.file
    else:
        url = args.url or ocfg.get("url")
        if not url:
            sys.exit("Set officevibe.url in config.json, or pass --url / --file.")
        src, err = fetch(url, args.timeout)
        if err:
            sys.exit(f"Could not read the export: {err}\n"
                     "It is published inside the EQS network, so this has to run "
                     "somewhere that can reach it.")
        where = url

    rows = src.get("weekly") or []
    if not rows:
        sys.exit("That file has no 'weekly' array; nothing to import.")

    team = src.get("team") or {}
    by_key = {week_key(r): r for r in rows if week_key(r)}
    weeks = build_weeks(count, tz)

    kept = suppressed = 0
    for w in weeks:
        row = by_key.get(w["key"])
        if row is None:
            continue
        if row.get("suppressed"):
            suppressed += 1
        vals = {k: row.get(f) for k, f in FIELDS.items()}
        w["metrics"] = vals
        if all(vals[k] is None for k in SCORE_FIELDS):
            # The week existed and the team had a size; the answers are simply
            # not shown. Recorded as such, so it reads as anonymity rather than
            # as a week the export never covered.
            w["suppressed"] = bool(row.get("suppressed"))
            continue
        kept += 1

    if not kept:
        sys.exit(f"The export covers {len(by_key)} week(s), none of them in the last "
                 f"{count}. Try --weeks with a bigger number.")

    print(f"{team.get('name') or 'team'} · {where}")
    print(f"generated {src.get('generated_at')} · data as of {src.get('data_as_of')}\n")
    for w in weeks[:8]:
        m = w["metrics"]
        if not m:
            print(f"  {w['key']}  (not in the export)")
        elif all(m.get(k) is None for k in SCORE_FIELDS):
            print(f"  {w['key']}  withheld for anonymity "
                  f"(team of {m.get('ovParticipants')})")
        else:
            print(f"  {w['key']}  participants={m['ovParticipants']}  "
                  f"rate={m['ovParticipationRate']}%  eNPS={m['ovEnps']}  "
                  f"engagement={m['ovEngagement']}")
    if len(weeks) > 8:
        print(f"  ... {len(weeks) - 8} more")

    # Targets travel with the data, so the published page grades exactly as the
    # local one does — the same arrangement the New Relic snapshot uses.
    bands = ocfg.get("thresholds") or {}
    for m in METRIC_DEFS:
        t = bands.get(m["key"])
        if t and t.get("bands"):
            m["bands"] = t["bands"]

    out = {
        "generatedAt": datetime.now(tz).isoformat(timespec="seconds"),
        "source": src.get("source") or "Workleap (Officevibe)",
        "from": where,
        "dataAsOf": src.get("data_as_of"),
        "team": {"name": team.get("name"), "slug": team.get("slug"),
                 "headcount": team.get("headcount")},
        "metrics": METRIC_DEFS,
        "weeks": weeks,
        "weeksWithData": kept,
        "weeksSuppressed": suppressed,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))

    print(f"\nWrote {args.out} — {kept} week(s) with scores, "
          f"{suppressed} withheld for anonymity, {os.path.getsize(args.out):,} bytes")
    print("Participants is the team's headcount — everyone the survey goes to.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
