#!/usr/bin/env python3
"""
Build a static snapshot of the weekly New Relic numbers.

Runs the same NRQL the live dashboard uses, but server-side, so the result can
be published to GitLab Pages as plain JSON. The API key is read from the
environment and never appears in the output.

    python3 fetch_snapshot.py --out public/data.json
    NEW_RELIC_API_KEY=NRAK-... python3 fetch_snapshot.py   # or pass it in

Weeks run Monday 00:00:00.000 to Sunday 23:59:59.999 Europe/Berlin, matching
the dashboard exactly, including the 167- and 169-hour DST weeks.
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

ENDPOINTS = {
    "US": "https://api.newrelic.com/graphql",
    "EU": "https://api.eu.newrelic.com/graphql",
}

GQL = """query W($id: Int!, $qUptime: Nrql!, $qResp: Nrql!, $qErrors: Nrql!, $qRate: Nrql!,
                 $qAlerts: Nrql!, $qHosts: Nrql!, $qHostErrors: Nrql!, $qHostAlerts: Nrql!,
                 $qTopErrors: Nrql!) {
  actor { account(id: $id) {
    uptime:        nrql(query: $qUptime)     { results }
    responseMs:    nrql(query: $qResp)       { results }
    errors:        nrql(query: $qErrors)     { results }
    errorRate:     nrql(query: $qRate)       { results }
    alerts:        nrql(query: $qAlerts)     { results }
    perHost:       nrql(query: $qHosts)      { results }
    perHostErrors: nrql(query: $qHostErrors) { results }
    perHostAlerts: nrql(query: $qHostAlerts) { results }
    topErrors:     nrql(query: $qTopErrors)  { results }
  } }
}"""

UPTIME_SYNTHETIC = ("SELECT percentage(count(*), WHERE result = 'SUCCESS') AS value "
                    "FROM SyntheticCheck WHERE monitorName IN ({{MONITORS}}) {{RANGE}}")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# --------------------------------------------------------------------- weeks

KEY_PATH = os.path.join(BASE_DIR, "api_key.txt")


def load_api_key():
    """
    The key, from the environment or from api_key.txt beside this script.

    The same two places server.py looks, and in the same order. Reading only
    the environment meant this script failed on a laptop that was otherwise
    set up and working, which is a confusing way to be told to configure CI.
    """
    key = (os.environ.get("NEW_RELIC_API_KEY") or "").strip()
    if key:
        return key
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, encoding="utf-8") as fh:
            raw = fh.read()
        # ignore the shipped placeholder and any commented-out lines
        key = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("#")).strip()
        if key and "PASTE_YOUR" not in key:
            return key
    return ""


def wall_to_ms(d: date, tz: ZoneInfo) -> int:
    """UTC epoch milliseconds for 00:00 local time on `d`."""
    return int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp() * 1000)


def build_weeks(count: int, tz: ZoneInfo):
    today = datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())
    weeks = []
    for i in range(count):
        start = monday - timedelta(days=7 * i)
        nxt = start + timedelta(days=7)
        shown_end = start + timedelta(days=6)
        iso_year, iso_week, _ = start.isocalendar()
        end_label = f"{shown_end.day:02d} {MONTHS[shown_end.month - 1]}"
        if shown_end.year != start.year:
            end_label += f" {shown_end.year}"
        weeks.append({
            "key": f"{iso_year}-W{iso_week:02d}",
            "isoLabel": f"W{iso_week:02d}",
            "isoYear": iso_year,
            "startMs": wall_to_ms(start, tz),
            "endMs": wall_to_ms(nxt, tz),
            "startLabel": f"{start.day:02d} {MONTHS[start.month - 1]}",
            "endLabel": end_label,
            "year": start.year,
            "current": i == 0,
            "partial": i == 0,
            "metrics": {},
            "hosts": [],
        })
    return weeks


# ---------------------------------------------------------------------- nrql

def quote_list(values):
    vals = values or [""]
    return ", ".join("'" + str(v).replace("'", "\\'") + "'" for v in vals)


def render_nrql(tpl, week, cfg, app):
    return (str(tpl or "")
            .replace("{{RANGE}}", f"SINCE {week['startMs']} UNTIL {week['endMs']}")
            .replace("{{GUIDS}}", quote_list(app.get("entityGuids")))
            .replace("{{MONITORS}}", quote_list(cfg.get("monitorNames")))
            .replace("{{HOST_ATTR}}", app.get("hostAttribute")
                                     or cfg.get("hostAttribute") or "entity.name")
            .strip())


def first_value(results):
    if not results:
        return None
    row = results[0] or {}
    if "value" in row:
        return None if row["value"] is None else float(row["value"])
    nums = [v for v in row.values() if isinstance(v, (int, float))]
    return float(nums[0]) if nums else None


def facet_key(row, host_attr):
    facet = row.get("facet")
    if facet is not None:
        return " / ".join(map(str, facet)) if isinstance(facet, list) else str(facet)
    return str(row.get(host_attr) or row.get("entity.name") or "unknown")


def top_errors(results, app, limit=6):
    """
    The error groups behind a week's count, biggest first.

    Stored per provider as well as for the week, so the tooltip on a provider
    row shows that provider's errors rather than the application's.

    The message is the useful part — "Company with id [...] not found" — and the
    class name alone rarely is, so both are kept and the message leads.
    """
    host_attr = app.get("hostAttribute") or "entity.name"
    out = []
    for row in (results or []):
        facets = row.get("facet")
        if not isinstance(facets, list):
            facets = [facets]
        message = str(facets[0]) if len(facets) > 0 and facets[0] is not None else ""
        name = str(facets[1]) if len(facets) > 1 and facets[1] is not None else ""
        host = str(facets[2]) if len(facets) > 2 and facets[2] is not None else ""
        # Each row carries the entity it came from, so the link goes to the
        # right provider. Matching providers to GUIDs by position would be a
        # guess: the facet order is New Relic's, not config's.
        guid = str(facets[3]) if len(facets) > 3 and facets[3] is not None else ""
        count = row.get("count")
        if count is None:
            for k, v in row.items():
                if k != "facet" and isinstance(v, (int, float)):
                    count = v
                    break
        if not (message or name):
            continue
        out.append({"message": message[:400], "name": name[:200],
                    "host": host, "guid": guid, "count": count})
    out.sort(key=lambda e: (e["count"] or 0), reverse=True)
    return out[:limit]


def merge_hosts(base, errs, alerts, cfg, app):
    labels = app.get("hostLabels") or cfg.get("hostLabels") or {}
    host_attr = app.get("hostAttribute") or cfg.get("hostAttribute") or "entity.name"
    out = {}

    def blank(raw):
        return {"raw": raw, "name": labels.get(raw, raw),
                "throughput": None, "responseMs": None, "errors": None, "alerts": None}

    for row in base or []:
        raw = facet_key(row, host_attr)
        h = blank(raw)
        h["throughput"] = row.get("throughput")
        h["responseMs"] = row.get("responseMs")
        h["errors"] = row.get("errorEvents")
        out[raw] = h

    for row in errs or []:
        raw = facet_key(row, host_attr)
        h = out.setdefault(raw, blank(raw))
        if row.get("errors") is not None:
            h["errors"] = row["errors"]

    # A faceted alert count only returns applications that had an incident, so
    # everything else is a real zero rather than missing data.
    if alerts is not None:
        for row in alerts:
            raw = facet_key(row, host_attr)
            h = out.setdefault(raw, blank(raw))
            if row.get("alerts") is not None:
                h["alerts"] = row["alerts"]
        for h in out.values():
            if h["alerts"] is None:
                h["alerts"] = 0

    hosts = []
    for h in out.values():
        thr, errors = h["throughput"], h["errors"]
        h["errorRate"] = (errors / thr) * 100 if thr and errors is not None else None
        h["uptime"] = None if h["errorRate"] is None else 100 - h["errorRate"]
        hosts.append(h)
    hosts.sort(key=lambda h: h["name"])
    return hosts


# --------------------------------------------------------------------- fetch

def nerdgraph(url, api_key, variables, timeout):
    body = json.dumps({"query": GQL, "variables": variables}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "API-Key": api_key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    if payload.get("errors"):
        raise RuntimeError("; ".join(e.get("message", "?") for e in payload["errors"]))
    account = (payload.get("data") or {}).get("actor", {}).get("account")
    if not account:
        raise RuntimeError("NerdGraph returned no account — check the account ID, "
                           "the region, and that the key has access")
    return account


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "public", "data.json"))
    ap.add_argument("--weeks", type=int, default=None, help="override config.weeks")
    ap.add_argument("--timeout", type=int, default=90)
    args = ap.parse_args()

    api_key = load_api_key()
    if not api_key:
        sys.exit("No New Relic key.\n"
                 "  Locally : put your User key in api_key.txt next to this script "
                 "(it is git-ignored).\n"
                 "  In CI   : add NEW_RELIC_API_KEY as a masked, protected variable.")

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)

    account_id = str(cfg.get("accountId") or "").strip()
    if not account_id:
        sys.exit("config.json has no accountId.")

    svcs = [a for a in (cfg.get("applications") or [])
            if a.get("enabled", True)]
    if not svcs:
        sys.exit("config.json has no applications. Each needs an id, a name and entityGuids.")
    bad = [s.get("id") or "?" for s in svcs if not s.get("entityGuids")]
    if bad:
        sys.exit(f"These applications have no entityGuids: {', '.join(bad)}")

    region = (cfg.get("region") or "EU").upper()
    url = ENDPOINTS.get(region, ENDPOINTS["EU"])
    tz = ZoneInfo(cfg.get("timezone") or "Europe/Berlin")
    count = max(1, min(104, args.weeks or int(cfg.get("weeks") or 12)))

    team = (cfg.get("team") or {}).get("name") or "weekly ops"
    print(f"{team} · account {account_id} ({region}) · {len(svcs)} application(s) · "
          f"{count} weeks · {tz}")

    out_services = {}
    total_ok = 0

    for svc in svcs:
        sid = svc["id"]
        guids = svc["entityGuids"]
        nrql = svc.get("nrql") or cfg.get("nrql") or {}
        uptime_tpl = nrql.get("uptime")
        print(f"\n{svc.get('name') or sid} — {svc.get('kind','?')}, {len(guids)} entities")
        weeks = build_weeks(count, tz)
        failures = []

        for i, w in enumerate(weeks, 1):
            variables = {
                "id": int(account_id),
                "qUptime":     render_nrql(uptime_tpl, w, cfg, svc),
                "qResp":       render_nrql(nrql.get("responseMs"), w, cfg, svc),
                "qErrors":     render_nrql(nrql.get("errors"), w, cfg, svc),
                "qRate":       render_nrql(nrql.get("errorRate"), w, cfg, svc),
                "qAlerts":     render_nrql(nrql.get("alerts"), w, cfg, svc),
                "qHosts":      render_nrql(nrql.get("perHost"), w, cfg, svc),
                "qHostErrors": render_nrql(nrql.get("perHostErrors"), w, cfg, svc),
                "qHostAlerts": render_nrql(nrql.get("perHostAlerts"), w, cfg, svc),
                # Asked for every week, but only ever read when a number is in
                # the red: a count on its own says a week was bad, not what
                # went wrong, and that is the first thing anyone asks.
                # Non-null in the query document, so an application with
                # neither template must still send something harmless rather
                # than failing the whole week.
                "qTopErrors":  render_nrql(nrql.get("topErrors")
                                           or nrql.get("perHostErrors")
                                           or "SELECT count(*) FROM Metric LIMIT 1",
                                           w, cfg, svc),
            }
            try:
                acct = nerdgraph(url, api_key, variables, args.timeout)
                rs = lambda k: (acct.get(k) or {}).get("results")
                w["metrics"] = {
                    "uptime":     first_value(rs("uptime")),
                    "responseMs": first_value(rs("responseMs")),
                    "errors":     first_value(rs("errors")),
                    "errorRate":  first_value(rs("errorRate")),
                    "alerts":     first_value(rs("alerts")),
                }
                if w["metrics"]["uptime"] is None and w["metrics"]["errorRate"] is not None:
                    w["metrics"]["uptime"] = 100 - w["metrics"]["errorRate"]
                w["hosts"] = merge_hosts(rs("perHost"), rs("perHostErrors"),
                                         rs("perHostAlerts"), cfg, svc)
                w["topErrors"] = top_errors(rs("topErrors"), svc)
                print(f"  [{i}/{count}] {w['key']}  ok  ({len(w['hosts'])} applications)")
            except (urllib.error.URLError, RuntimeError, ValueError, TimeoutError) as exc:
                failures.append(f"{w['key']}: {exc}")
                print(f"  [{i}/{count}] {w['key']}  FAILED  {exc}", file=sys.stderr)

        ok = [w for w in weeks if any(v is not None for v in w["metrics"].values())]
        total_ok += len(ok)
        print(f"  {len(ok)}/{count} weeks with data")
        out_services[sid] = {
            "name": svc.get("name") or sid,
            "entityCount": len(guids),
            # the page needs each application's own bands and labels to grade
            # and name rows exactly as the live view does
            "meta": {
                "kind": svc.get("kind"),
                "role": svc.get("role"),
                "source": svc.get("source"),
                "hostAttribute": svc.get("hostAttribute"),
                "hostLabels": svc.get("hostLabels") or {},
                "thresholds": svc.get("thresholds") or cfg.get("thresholds") or {},
                # The page links a red number to the application in New Relic.
                # An entity GUID is not a secret — it is in every URL the UI
                # produces — and without it the tooltip can name an error but
                # not take you to it.
                "entityGuids": guids,
            },
            "weeks": weeks,
            "failures": failures,
        }

    if not total_ok:
        sys.exit("Every week of every service failed, refusing to publish an empty snapshot.")

    snapshot = {
        "generatedAt": datetime.now(tz).isoformat(timespec="seconds"),
        "applications": out_services,
        # Rendering settings only — no key, no NRQL, no GUIDs.
        "config": {
            "accountId": account_id,
            "region": region,
            "timezone": str(tz),
            "team": cfg.get("team") or {},
            "defaultApplication": cfg.get("defaultApplication")
                                  or ("all" if len(svcs) > 1 else svcs[0]["id"]),
            "weeks": count,
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, separators=(",", ":"))

    size = os.path.getsize(args.out)
    all_failures = sum(len(v["failures"]) for v in out_services.values())
    print(f"\nWrote {args.out} — {len(out_services)} service(s), "
          f"{total_ok} week-records with data, {size:,} bytes")
    if all_failures:
        print(f"{all_failures} week(s) failed; the snapshot was still published.")


if __name__ == "__main__":
    main()
