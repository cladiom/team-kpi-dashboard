#!/usr/bin/env python3
"""
Build the weekly Jira delivery snapshot.

Counts, for each Monday-to-Sunday week, the metrics defined in config.json
under "jira". Writes delivery.json, which the dashboard's Delivery tab reads.

    python3 fetch_jira.py                      # delivery.json beside this file
    python3 fetch_jira.py --weeks 12 --out public/delivery.json

Credentials come from jira_token.txt (email on line 1, API token on line 2) or
from JIRA_EMAIL and JIRA_API_TOKEN. The token is never written to the output.

Timezone, which is the easy thing to get wrong: the weeks are Monday 00:00 to
Sunday 23:59:59.999 Europe/Berlin, but JQL reads bare date literals in the
*calling account's* timezone. So this script asks Jira which timezone the
account uses and converts each Berlin boundary into it. A Berlin Monday 00:00
becomes "22:00 the previous day" for a GMT account — and if someone changes the
account's timezone later, the conversion follows automatically.
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(BASE_DIR, "jira_token.txt")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def credentials():
    email = (os.environ.get("JIRA_EMAIL") or "").strip()
    token = (os.environ.get("JIRA_API_TOKEN") or "").strip()
    if not (email and token) and os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
        if len(lines) == 1 and ":" in lines[0]:
            email, token = lines[0].split(":", 1)
        elif len(lines) >= 2:
            email, token = lines[0], lines[1]
    if not (email and token):
        sys.exit("Put your Atlassian email and API token in jira_token.txt, or set "
                 "JIRA_EMAIL and JIRA_API_TOKEN.")
    return email.strip(), token.strip()


def api(site, path, email, token, timeout=30, body=None):
    """GET, or POST when `body` is given."""
    url = site.rstrip("/") + path
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {"Authorization": "Basic " + auth, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(body)
        except ValueError:
            body = body[:400]
        return exc.code, body
    except Exception as exc:
        return 0, str(exc)


def build_weeks(count, tz):
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
            "_start": datetime(start.year, start.month, start.day, tzinfo=tz),
            "_end": datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz),
            "current": i == 0,
            "partial": i == 0,
            "metrics": {},
            "jql": {},
        })
    return out


def _detail(status, body):
    text = body if isinstance(body, str) else json.dumps(body)
    return f"HTTP {status}: {text[:200]}"


def count_jql(site, jql, email, token, timeout):
    """
    How many issues a JQL matches.

    Jira Cloud's current search endpoint returns no `total` at all, and rejects
    maxResults=0, so counting needs the dedicated endpoint. Three routes, best
    first, because instances differ:

      1. /search/approximate-count — one cheap POST, exact at these volumes
      2. /search/jql paged — exact, but one request per 100 issues
      3. /search?maxResults=1 — the pre-2025 endpoint, which did return a total
    """
    status, body = api(site, "/rest/api/3/search/approximate-count",
                       email, token, timeout, {"jql": jql})
    if 200 <= status < 300 and isinstance(body, dict) and "count" in body:
        return int(body["count"]), None
    first = _detail(status, body)

    total, page_token, pages = 0, None, 0
    while pages < 60:                       # 6,000 issues is far beyond a week
        params = {"jql": jql, "maxResults": 100, "fields": "id"}
        if page_token:
            params["nextPageToken"] = page_token
        st, b = api(site, "/rest/api/3/search/jql?" + urllib.parse.urlencode(params),
                    email, token, timeout)
        if not (200 <= st < 300 and isinstance(b, dict)):
            break
        total += len(b.get("issues") or [])
        page_token = b.get("nextPageToken")
        pages += 1
        if not page_token:
            return total, None              # only trust a count we finished

    st, b = api(site, "/rest/api/3/search?" + urllib.parse.urlencode(
        {"jql": jql, "maxResults": 1}), email, token, timeout)
    if 200 <= st < 300 and isinstance(b, dict) and "total" in b:
        return int(b["total"]), None

    return None, first


_TZ_SUFFIX = re.compile(r"([+-]\d{2})(\d{2})$")


def _iso_ms(text):
    """
    Jira hands back several spellings of the same instant:
    '2026-09-22T09:14:03.000Z', '...+0000' and '...+02:00'.

    The middle one is the trap. fromisoformat only learned to read an offset
    without a colon in Python 3.11, and this runs on 3.9 — where every changelog
    timestamp would silently fail to parse and the metric built on it would come
    back empty rather than wrong, which is harder to notice.
    """
    if not text:
        return None
    t = str(text).strip().replace("Z", "+00:00")
    t = _TZ_SUFFIX.sub(r"\1:\2", t)
    for parse in (lambda: datetime.fromisoformat(t),
                  lambda: datetime.strptime(t, "%Y-%m-%d")):
        try:
            d = parse()
        except ValueError:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)
    return None


def _sprint_ids(raw):
    """A Sprint changelog value is a comma-separated list of sprint ids."""
    return {int(x) for x in re.findall(r"\d+", raw or "")}


def _membership_at(changes, moment, current):
    """
    Which sprints an issue belonged to at `moment`.

    `changes` are its Sprint-field changelog entries, oldest first. The state
    before the first change is that change's "from"; with no changes at all the
    issue has always been where it is now.
    """
    if not changes:
        return set(current)
    state = changes[0][1]
    for ts, before, after in changes:
        if ts > moment:
            break
        state = after
    return state


def _sprint_changes(issue):
    out = []
    for h in ((issue.get("changelog") or {}).get("histories") or []):
        ts = _iso_ms(h.get("created"))
        if ts is None:
            continue
        for it in (h.get("items") or []):
            if (it.get("field") or "").lower() != "sprint":
                continue
            out.append((ts, _sprint_ids(it.get("from")), _sprint_ids(it.get("to"))))
    out.sort(key=lambda r: r[0])
    return out


def _scope_from_changelog(site, project, sprint, email, token, timeout):
    """
    Rebuild a sprint's day-one scope from the issues' own history.

    The sprint report endpoint is the easy route but it is greenhopper's, and
    team-managed projects often do not serve it. Every Jira does record Sprint
    field changes in the changelog, so the scope can be reconstructed: an issue
    was planned if it was already in the sprint when the sprint started.

    Issues pulled out later no longer match `sprint = X`, so they are picked up
    by scanning what changed since the sprint began — otherwise dropping a
    ticket would quietly shrink the plan after the fact.
    """
    sid = sprint["id"]
    started = _iso_ms(sprint.get("startDate"))
    if started is None:
        return None, f"sprint {sid} has no start date"

    since = datetime.fromtimestamp(started / 1000, tz=timezone.utc) - timedelta(days=1)
    seen, err = {}, None
    queries = [
        f"sprint = {sid}",                                   # in it now
        f'project = "{project}" AND updated >= "{since.strftime("%Y/%m/%d %H:%M")}"',
    ]
    for jql in queries:
        token_page, pages = None, 0
        while pages < 40:
            params = {"jql": jql, "maxResults": 100,
                      "fields": "summary", "expand": "changelog"}
            if token_page:
                params["nextPageToken"] = token_page
            st, b = api(site, "/rest/api/3/search/jql?" + urllib.parse.urlencode(params),
                        email, token, timeout)
            if not (200 <= st < 300 and isinstance(b, dict)):
                err = err or _detail(st, b)
                break
            for i in (b.get("issues") or []):
                seen.setdefault(i.get("key"), i)
            token_page = b.get("nextPageToken")
            pages += 1
            if not token_page:
                break

    if not seen:
        return None, err or f"no issues found for sprint {sid}"

    planned, added = set(), set()
    for key, issue in seen.items():
        changes = _sprint_changes(issue)
        ever = set(_sprint_ids(",".join(str(x) for _, b, a in changes for x in (b | a))))
        now = {sp.get("id") for sp in ((issue.get("fields") or {}).get("sprint") or [])
               if isinstance(sp, dict)}
        if sid not in ever and sid not in now:
            continue                                  # never had anything to do with it
        if sid in _membership_at(changes, started, now):
            planned.add(key)
        else:
            added.add(key)

    return {"planned": len(planned), "added": len(added),
            "source": "changelog"}, None


def _week_of(weeks, ms):
    for w in weeks:
        if w["startMs"] <= ms < w["endMs"]:
            return w["key"]
    return None


CP_BASE = "https://api.atlassian.com"


def _ari(person):
    """Accept an account id or a full ARI."""
    p = str(person).strip()
    return p if p.startswith("ari:") else f"ari:cloud:identity::user/{p}"


def _cp(cloud_id, path):
    return f"/ex/capacity-planning/{cloud_id}/v1{path}"


def _cloud_id(site, jcfg, email, token, timeout):
    cap = jcfg.get("capacity") or {}
    if cap.get("cloudId"):
        return cap["cloudId"], None
    st, b = api(site, "/_edge/tenant_info", email, token, timeout)
    if 200 <= st < 300 and isinstance(b, dict) and b.get("cloudId"):
        return b["cloudId"], None
    return None, f"could not read the site's cloudId: {_detail(st, b)}"


def _work_for(cloud_id, ari, email, token, timeout):
    """Every work item a person is associated with, following the cursor."""
    out, after = [], None
    for _ in range(20):
        q = {"contributorDataId": ari, "first": 50}
        if after:
            q["after"] = after
        st, b = api(CP_BASE, _cp(cloud_id, "/contributor-work-associations/by-contributor?")
                    + urllib.parse.urlencode(q), email, token, timeout)
        if not (200 <= st < 300 and isinstance(b, dict)):
            return out, _detail(st, b)
        out += [a.get("workDataId") for a in (b.get("associations") or [])
                if a.get("workDataId")]
        page = b.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
        if not after:
            break
    return out, None


def _as_share(value, value_type, hours_per_week):
    """
    One week of one person, as a fraction of that person's week.

    The API reports either a percentage or hours; hours only mean something
    once divided by the working week, which is why hoursPerWeek is config and
    not a constant.
    """
    if value is None:
        return 0.0
    t = (value_type or "").upper()
    if t == "PERCENTAGE":
        return float(value) / 100.0
    if t == "HOURS":
        return float(value) / float(hours_per_week or 40)
    # Unlabelled: percentages are the only values that exceed 1 in practice.
    v = float(value)
    return v / 100.0 if v > 1.5 else v


def _work_name(site, cloud_id, work_ari, email, token, timeout):
    """
    What a work item is called.

    An ARI's resource type says which product owns it, but the spellings differ
    between products and versions, so the type is used as a hint and both routes
    are tried rather than trusted: non-project items carry their name in Capacity
    Planning, Jira issues carry theirs in Jira. Guessing from a substring was
    wrong often enough to make the whole metric return nothing.
    """
    if "/" not in work_ari:
        return ""
    head, tail = work_ari.rsplit("/", 1)
    rtype = head.rsplit(":", 1)[-1].lower()
    jira_first = "issue" in rtype

    def from_cp():
        st, b = api(CP_BASE, _cp(cloud_id, f"/non-project-work-items/{tail}"),
                    email, token, timeout)
        return (b.get("name") or "") if (200 <= st < 300 and isinstance(b, dict)) else ""

    def from_jira():
        st, b = api(site, f"/rest/api/3/issue/{urllib.parse.quote(tail)}?fields=summary",
                    email, token, timeout)
        return (((b.get("fields") or {}).get("summary")) or "") \
            if (200 <= st < 300 and isinstance(b, dict)) else ""

    for fn in ((from_jira, from_cp) if jira_first else (from_cp, from_jira)):
        name = fn()
        if name:
            return name
    return ""


def _npwi_by_name(cloud_id, wanted, email, token, timeout, seen=None):
    """
    Non-project work items whose name is one of `wanted`, as {ari: name}.

    `seen` collects every name encountered, so a miss can report what the plan
    does contain instead of only what it does not.
    """
    out, cursor = {}, None
    for _ in range(10):
        q = {"cursor": cursor} if cursor else {}
        st, b = api(CP_BASE, _cp(cloud_id, "/non-project-work-items")
                    + ("?" + urllib.parse.urlencode(q) if q else ""),
                    email, token, timeout)
        if not (200 <= st < 300 and isinstance(b, dict)):
            return out, _detail(st, b)
        for it in (b.get("results") or b.get("values") or []):
            name = (it.get("name") or "").strip()
            if seen is not None and name:
                seen.append(name)
            if name.lower() in wanted and it.get("ari"):
                out[it["ari"]] = name
        if not b.get("hasMore"):
            break
        cursor = b.get("next")
        if not cursor:
            break
    return out, None


def _contributors_of(cloud_id, work_ari, email, token, timeout):
    """Everyone associated with a work item, as account ids."""
    out, after = [], None
    for _ in range(20):
        q = {"workDataId": work_ari, "first": 50}
        if after:
            q["after"] = after
        st, b = api(CP_BASE, _cp(cloud_id, "/contributor-work-associations/by-work?")
                    + urllib.parse.urlencode(q), email, token, timeout)
        if not (200 <= st < 300 and isinstance(b, dict)):
            return out, _detail(st, b)
        for a in (b.get("associations") or []):
            cid = (a.get("contributorDataId") or "").rsplit("/", 1)[-1]
            if cid and cid not in out:
                out.append(cid)
        page = b.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
        if not after:
            break
    return out, None


def capacity_by_week(site, jcfg, email, token, timeout, weeks):
    """
    Available capacity per week, in person-weeks.

    The conversion is the whole metric, so it is stated once: a person free for
    a whole week counts as 1, 40% counts as 0.4, and the week's figure is the
    sum across the team. At five working days, 0.4 is two days.

    Jira's Capacity Planning API reports *allocations*, not availability — the
    "Available" row in the Plans grid is the remainder the UI draws. So
    availability is computed the same way: a person starts at 1 and every
    contribution is subtracted, vacation included, because vacation is itself a
    contribution to a non-project work item. That is why the screenshot's 40%
    available and 60% vacation add to 100.

    Contributions are keyed by contributor *and* work item, so there is no way
    to ask "everything for this person" in one call: the work items come from
    the association endpoint first, then one contributions call each.
    """
    cap = jcfg.get("capacity") or {}
    people = cap.get("people") or []
    # Two readings of the same grid, and they are opposites, so the choice is
    # explicit rather than inferred:
    #
    #   allocation — "Available" is a row people are allocated to, so the
    #                figure is the sum of contributions to it
    #   remainder  — "Available" is what the UI draws after everything else,
    #                so the figure is a full week less every contribution
    #
    # With a Vacation row present the remainder reading is the honest one. With
    # only an Available row, summing it is — and getting this backwards turns
    # 40% available into 60%.
    mode = (cap.get("mode") or "allocation").lower()
    counts = {str(x).strip().lower() for x in (cap.get("counts") or ["available"])}
    info = {"unit": "person-weeks", "mode": mode}

    cloud_id, why = _cloud_id(site, jcfg, email, token, timeout)
    if not cloud_id:
        info["error"] = why
        return {}, why, info
    info["cloudId"] = cloud_id

    # Who is on the team is discoverable in allocation mode: whoever is
    # associated with the Available row is, by definition, someone whose
    # availability is being tracked. Listing account ids by hand would only be
    # a second place for the team roster to go stale.
    # The plan-wide listing is an optimisation, not a requirement: it can come
    # back empty when the token cannot see the plan's items, while the same
    # items are still reachable one at a time through people's associations. So
    # a miss here is not fatal — names get resolved per item below.
    named_work, present = {}, []
    if mode == "allocation":
        named_work, err = _npwi_by_name(cloud_id, counts, email, token, timeout, present)
        info["workItemsListed"] = sorted(set(present))
        if named_work:
            info["countsFound"] = sorted(named_work.values())

    if not people and mode == "allocation" and named_work:
        discovered = []
        for ari in named_work:
            found, err = _contributors_of(cloud_id, ari, email, token, timeout)
            if err:
                info["error"] = err
                return {}, f"capacity: {err}", info
            discovered += [p for p in found if p not in discovered]
        if discovered:
            people = discovered
            info["discovered"] = "the Available row"

    if not people:
        # No Available row to read the roster off, so take the people who can be
        # assigned work in the project. It is the same team, and it stays
        # correct when someone joins or leaves.
        project = (jcfg.get("projectKey") or "")
        st, b = api(site, "/rest/api/3/user/assignable/search?"
                    + urllib.parse.urlencode({"project": project, "maxResults": 50}),
                    email, token, timeout)
        if 200 <= st < 300 and isinstance(b, list):
            people = [u.get("accountId") for u in b
                      if u.get("accountId") and u.get("accountType") == "atlassian"
                      and u.get("active", True)]
            info["discovered"] = "project assignees"
            info["discoveredFrom"] = project
        if not people:
            return {}, ("capacity.people is empty and nobody assignable was found in "
                        f"{project or 'the project'} — list the team's account ids in "
                        "jira.capacity.people"), info

    if not people:
        discovered = []
        for ari in named_work:
            found, err = _contributors_of(cloud_id, ari, email, token, timeout)
            if err:
                info["error"] = err
                return {}, f"capacity: {err}", info
            discovered += [p for p in found if p not in discovered]
        if not discovered:
            return {}, ("nobody is associated with the Available row, so there is "
                        "no capacity to report"), info
        people = discovered
        info["discovered"] = "the Available row"

    hours = cap.get("hoursPerWeek") or 40
    baselines = {str(k): float(v) for k, v in (cap.get("baseline") or {}).items()}
    first = min(w["startMs"] for w in weeks)
    last = max(w["endMs"] for w in weeks)
    span = {
        "startDate": datetime.fromtimestamp(first / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        "endDate": datetime.fromtimestamp(last / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
    }

    # by_week[week][person] — summed before anything else, so a person on two
    # work items in one week is not counted twice over.
    allocated, calls, note = {}, 0, None
    filled = set()                 # weeks the capacity plan actually covers
    seen_work, seen_aris = 0, set()
    name_cache = {}                # work ARI -> its name, resolved at most once
    for person in people:
        ari = _ari(person)
        work, err = _work_for(cloud_id, ari, email, token, timeout)
        calls += 1
        if err:
            note = note or f"{person}: {err}"
            continue
        for work_ari in work:
            seen_work += 1
            seen_aris.add(work_ari)
            if mode == "allocation":
                name = named_work.get(work_ari)
                if name is None:
                    if work_ari not in name_cache:
                        name_cache[work_ari] = _work_name(site, cloud_id, work_ari,
                                                          email, token, timeout)
                        calls += 1
                    name = name_cache[work_ari]
                    present.append(name or f"<unnamed> {work_ari}")
                if (name or "").strip().lower() not in counts:
                    continue                   # only the rows that count as capacity
            q = dict(span, contributorDataId=ari, workDataId=work_ari)
            st, b = api(CP_BASE, _cp(cloud_id, "/contributions?") + urllib.parse.urlencode(q),
                        email, token, timeout)
            calls += 1
            if not (200 <= st < 300 and isinstance(b, dict)):
                note = note or f"contributions for {person}: {_detail(st, b)}"
                continue
            for c in (b.get("contributions") or []):
                a = _iso_ms(c.get("startDate"))
                z = _iso_ms(c.get("endDate"))
                if a is None:
                    continue
                mid = a if z is None else (a + z) // 2     # a cell, placed by its middle
                key = _week_of(weeks, mid)
                if key is None:
                    continue
                share = _as_share(c.get("value"), c.get("valueType"), hours)
                allocated.setdefault(key, {}).setdefault(str(person), 0.0)
                allocated[key][str(person)] += share
                if share:
                    # The API returns a cell for every week in the window,
                    # zero-valued where nothing has been entered. A zero cell is
                    # the absence of a plan, not a plan of zero, so only
                    # non-zero cells count as evidence that a week is covered.
                    filled.add(key)

    if not filled:
        info["calls"] = calls
        info["workItemsSeen"] = sorted(set(present))
        if mode == "allocation" and present:
            return {}, (f"nothing is allocated to {' or '.join(sorted(counts))}. "
                        f"The work items these people are on are: "
                        f"{', '.join(sorted(set(present))[:12])}"), info
        if seen_work:
            return {}, (f"{seen_work} work association(s) found, but no contributions "
                        f"came back for them. First few: "
                        f"{', '.join(sorted(seen_aris)[:4])}"), info
        return {}, (note or "these people have no work associations at all"), info

    # Report only the stretch the plan actually covers: from the earliest week
    # with real data to the latest. Weeks outside it are blank rather than a
    # number nobody entered. A zero week *inside* that stretch is kept, because
    # there it does mean "no capacity" rather than "not filled in yet".
    covered = [i for i, w in enumerate(weeks) if w["key"] in filled]
    out = {}
    if covered:
        first, last = min(covered), max(covered)
        for i, w in enumerate(weeks):
            if not (first <= i <= last):
                continue
            per_person = allocated.get(w["key"]) or {}
            total = 0.0
            for person in people:
                base = baselines.get(str(person), 1.0)
                v = per_person.get(str(person), 0.0)
                total += min(base, v) if mode == "allocation" else max(0.0, base - v)
            out[w["key"]] = round(total, 2)
    info["weeksPlanned"] = len(filled)

    info["calls"] = calls
    info["people"] = len(people)
    info["weeksCovered"] = len(out)
    if note:
        info["partial"] = note
    return out, None, info


def _in_progress_at(issue, start_names, start_ids, which="last"):
    """
    When the clock starts: a move into one of the statuses that count as in
    progress.

    Which move is a real choice about what is being measured. The *last* one
    answers "once the team settled on this, how long did it take" — an issue
    started, parked, and started again is timed from the restart. The *first*
    answers "how long from first touch to done", which includes the parked
    stretch. Set jira.cycleTime.start to "first" to measure that instead.
    """
    stamps = []
    for h in ((issue.get("changelog") or {}).get("histories") or []):
        ts = _iso_ms(h.get("created"))
        if ts is None:
            continue
        for it in (h.get("items") or []):
            if (it.get("field") or "").lower() != "status":
                continue
            to_name = (it.get("toString") or "").strip().lower()
            to_id = str(it.get("to") or "")
            if to_name in start_names or (start_ids and to_id in start_ids):
                stamps.append(ts)
    if not stamps:
        return None
    return max(stamps) if which == "last" else min(stamps)


def cycle_times(site, project, jcfg, email, token, timeout, frm, to):
    """
    How long the work resolved this week took, in days, as a median.

    Measured from the last move into "In Progress" to the resolution, which is
    the span the team actually controls — time spent sitting in the backlog
    before anyone picked it up says more about prioritisation than about flow,
    and an issue parked and restarted is timed from the restart.

    The median rather than the mean: cycle times are badly skewed, and one
    ticket left open over Christmas would drag an average somewhere nobody
    recognises.

    Issues that were resolved without ever being marked in progress are left
    out. They have no measurable duration, and counting them as zero would
    flatter the figure.
    """
    cfg = jcfg.get("cycleTime") or {}
    start_names = {str(x).strip().lower() for x in
                   (cfg.get("startStatuses") or ["In Progress"])}
    start_ids = {str(x) for x in (cfg.get("startStatusIds") or [])}
    which = (cfg.get("start") or "last").lower()

    jql = (f'project = "{project}" AND resolved >= "{frm}" AND resolved < "{to}"')
    issues, page_token, pages = [], None, 0
    while pages < 40:
        params = {"jql": jql, "maxResults": 100,
                  "fields": "resolutiondate,status", "expand": "changelog"}
        if page_token:
            params["nextPageToken"] = page_token
        st, b = api(site, "/rest/api/3/search/jql?" + urllib.parse.urlencode(params),
                    email, token, timeout)
        if not (200 <= st < 300 and isinstance(b, dict)):
            return None, 0, _detail(st, b), jql
        issues += (b.get("issues") or [])
        page_token = b.get("nextPageToken")
        pages += 1
        if not page_token:
            break

    spans = []
    for i in issues:
        done = _iso_ms(((i.get("fields") or {}).get("resolutiondate")))
        started = _in_progress_at(i, start_names, start_ids, which)
        if done is None or started is None or done < started:
            continue
        spans.append((done - started) / 86400000.0)

    if not spans:
        return None, len(issues), None, jql
    spans.sort()
    mid = len(spans) // 2
    median = spans[mid] if len(spans) % 2 else (spans[mid - 1] + spans[mid]) / 2
    return round(median, 1), len(spans), None, jql


def sprint_scope_by_week(site, board, email, token, timeout, weeks, project):
    """
    What the team had planned when each sprint started, per week.

    JQL cannot answer this on its own: `sprint in openSprints()` returns
    membership *now*, so scope added on day four would be counted as part of the
    plan. Two routes are tried, because Jira exposes this differently depending
    on how the project is run:

      1. the board's sprint report, which separates the issues added after the
         sprint began — the same figures Jira's own Sprint Report screen shows
      2. the issues' changelogs, reconstructing who was in the sprint at the
         moment it started

    The board's reported type is deliberately not used as a gate. Team-managed
    projects describe their board as 'simple' while running sprints perfectly
    happily, so the only honest test is whether the board actually returns
    sprints.

    Sprints do not line up with weeks. A sprint's figures are written to every
    week it covers, so a two-week sprint shows the same number twice — the plan
    really was set once, and pretending it changed mid-sprint would be worse.
    """
    out, note = {}, None
    info = {"id": board}

    st, b = api(site, f"/rest/agile/1.0/board/{board}", email, token, timeout)
    if 200 <= st < 300:
        info["name"] = b.get("name")
        info["type"] = (b.get("type") or "").lower()
        key = (b.get("location") or {}).get("projectKey")
        base = site.rstrip("/")
        info["url"] = (f"{base}/jira/software/projects/{key}/boards/{board}" if key
                       else f"{base}/secure/RapidBoard.jspa?rapidView={board}")
    else:
        info["error"] = _detail(st, b)        # not fatal: ask for sprints anyway

    sprints, start_at = [], 0
    while start_at < 500:
        st, b = api(site, f"/rest/agile/1.0/board/{board}/sprint"
                          f"?state=active,closed&startAt={start_at}&maxResults=50",
                    email, token, timeout)
        if not 200 <= st < 300:
            info["sprintsError"] = _detail(st, b)
            info["supportsSprints"] = False
            return {}, (f"board {board} would not list sprints: {_detail(st, b)}"), info
        sprints += b.get("values", [])
        if b.get("isLast", True):
            break
        start_at += 50

    info["supportsSprints"] = True
    info["sprints"] = len(sprints)
    active = [sp for sp in sprints if (sp.get("state") or "").lower() == "active"]
    info["activeSprints"] = [{"id": sp.get("id"), "name": sp.get("name"),
                              "startDate": sp.get("startDate"),
                              "endDate": sp.get("endDate")} for sp in active]
    if not sprints:
        info["supportsSprints"] = False
        return {}, f"board {board} has no sprints yet", info

    window_from = min(w["startMs"] for w in weeks)
    window_to = max(w["endMs"] for w in weeks)
    live = []
    for sp in sprints:
        a = _iso_ms(sp.get("startDate"))
        z = _iso_ms(sp.get("completeDate") or sp.get("endDate"))
        if a is None or z is None or z < window_from or a > window_to:
            continue
        live.append((a, z, sp))
    info["sprintsInWindow"] = len(live)
    if not live:
        return {}, "no sprints fall inside the weeks being reported", info

    sources = set()
    for a, z, sp in live:
        figures = None
        st, rep = api(site, f"/rest/greenhopper/1.0/rapid/charts/sprintreport"
                            f"?rapidViewId={board}&sprintId={sp['id']}",
                      email, token, timeout)
        if 200 <= st < 300 and isinstance(rep, dict) and rep.get("contents"):
            c = rep["contents"]
            added = set(c.get("issueKeysAddedDuringSprint") or {})
            everything = {i.get("key") for key in ("completedIssues",
                                                   "issuesNotCompletedInCurrentSprint",
                                                   "puntedIssues",
                                                   "issuesCompletedInAnotherSprint")
                          for i in (c.get(key) or [])}
            everything.discard(None)
            figures = {"planned": len(everything - added),
                       "added": len(added & everything) or len(added),
                       "source": "sprint report"}
        else:
            figures, why = _scope_from_changelog(site, project, sp, email, token, timeout)
            if figures is None:
                note = note or f"sprint {sp.get('name')}: {why}"
                continue

        sources.add(figures.pop("source"))
        figures["sprintId"] = sp["id"]
        figures["sprintName"] = sp.get("name") or f"sprint {sp['id']}"
        # Written to every week the sprint touches; the sprint with the greatest
        # overlap wins a week shared by two.
        for w in weeks:
            overlap = min(z, w["endMs"]) - max(a, w["startMs"])
            if overlap <= 0:
                continue
            if w["key"] not in out or overlap > out[w["key"]]["_overlap"]:
                out[w["key"]] = dict(figures, _overlap=overlap)

    info["weeksCovered"] = len(out)
    info["source"] = ", ".join(sorted(sources)) if sources else None
    return out, note, info


def load_base(src, timeout):
    """
    A previous snapshot, from a path or a URL.

    The published dashboard is the natural place to read it from in CI: last
    night's run is already serving its delivery.json, so the pipeline can top it
    up instead of re-querying a year of Jira every night. A missing or
    unreadable base is not an error — it just means a full fetch.
    """
    if not src:
        return {}, None
    try:
        if src.startswith("http://") or src.startswith("https://"):
            req = urllib.request.Request(src, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read() or b"null")
        else:
            if not os.path.exists(src):
                return {}, f"{src} does not exist"
            with open(src, encoding="utf-8") as fh:
                data = json.load(fh)
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    weeks = (data or {}).get("weeks") or []
    return {w.get("key"): w for w in weeks if w.get("key")}, None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "delivery.json"))
    ap.add_argument("--weeks", type=int, default=None)
    ap.add_argument("--base", default=None,
                    help="an existing delivery.json — a path or a URL — whose weeks "
                         "are kept for everything this run does not re-query. With "
                         "--weeks 2 that turns a full rebuild into a two-week top-up.")
    ap.add_argument("--keep", type=int, default=None,
                    help="how many weeks the merged file should hold "
                         "(default: config weeks)")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    email, token = credentials()
    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)

    jcfg = cfg.get("jira") or {}
    site = jcfg.get("site")
    project = jcfg.get("projectKey")
    metrics = jcfg.get("metrics") or []
    if not (site and project and metrics):
        sys.exit("config.json needs jira.site, jira.projectKey and jira.metrics.")

    tz = ZoneInfo(cfg.get("timezone") or "Europe/Berlin")
    keep = max(1, min(208, args.keep or int(cfg.get("weeks") or 12)))
    count = max(1, min(104, args.weeks or int(cfg.get("weeks") or 12)))

    base_weeks, base_why = load_base(args.base, args.timeout)
    if args.base:
        if base_why:
            # Without the base there is nothing to carry forward, so a two-week
            # top-up would quietly replace a year of history with two weeks —
            # and that short file becomes tomorrow's base. Widening the fetch
            # keeps the published file whole, at the cost of one slow run.
            print(f"  base unavailable ({base_why}) — fetching {keep} weeks "
                  f"instead of {count} so no history is lost")
            count = max(count, keep)
        else:
            print(f"  base: {len(base_weeks)} week(s) from {args.base}")

    status, me = api(site, "/rest/api/3/myself", email, token, args.timeout)
    if not 200 <= status < 300:
        detail = me if isinstance(me, str) else json.dumps(me)
        if "IP address" in detail:
            sys.exit("Atlassian refused this machine's IP address. Run this where the "
                     "org's allowlist permits, or have an admin allow the runner.")
        sys.exit(f"Could not authenticate: HTTP {status} {detail[:200]}")

    acct_tz = ZoneInfo(me.get("timeZone") or "UTC")
    print(f"{me.get('displayName')} · {project} · {count} weeks")
    print(f"weeks are {tz}; the account reads JQL dates as {acct_tz}\n")

    # The tech-debt filter is written once in config and substituted into every
    # metric that needs it, so a field renamed in Jira is a one-line fix rather
    # than five. A custom field whose name is ambiguous can be given as
    # {"field": "cf[10234]"} instead, which JQL also accepts.
    td = jcfg.get("techDebt") or {}
    tech_debt = ""
    if td.get("field") and td.get("value"):
        field = td["field"]
        if not (field.startswith("cf[") or " " not in field):
            field = f'"{field}"'
        tech_debt = f'{field} = "{td["value"]}"'

    weeks = build_weeks(count, tz)
    failures = []

    # Sprint-scoped metrics need the board, not a JQL, so they are resolved once
    # up front rather than per week.
    capacity_by_wk, capacity_info = {}, None
    if any((m.get("mode") or "weekly") == "capacity" for m in metrics):
        capacity_by_wk, why, capacity_info = capacity_by_week(
            site, jcfg, email, token, args.timeout, weeks)
        if why:
            failures.append(f"capacity: {why}")
            print(f"  capacity unavailable — {why}\n")
        else:
            print(f"  capacity resolved for {len(capacity_by_wk)} week(s), "
                  f"{capacity_info.get('people')} people\n")

    sprint_by_week, board_info = {}, None
    if any((m.get("mode") or "weekly") == "sprint" for m in metrics):
        board = jcfg.get("boardId")
        if not board:
            failures.append("sprint metrics need jira.boardId in config.json")
        else:
            sprint_by_week, why, board_info = sprint_scope_by_week(
                site, board, email, token, args.timeout, weeks, project)
            if board_info:
                # What the board actually is, recorded rather than inferred from
                # an empty column later.
                board_info["checkedAt"] = datetime.now(tz).isoformat(timespec="seconds")
                if why:
                    board_info["why"] = why
                print(f"  board {board_info.get('id')} "
                      f"'{board_info.get('name')}' — type {board_info.get('type')}, "
                      f"{board_info.get('sprints', 0)} sprint(s)")
            if why:
                failures.append(f"sprint scope: {why}")
                print(f"  sprint scope unavailable — {why}\n")
            else:
                print(f"  sprint scope resolved for {len(sprint_by_week)} week(s)\n")

    # Point-in-time metrics describe the backlog right now; earlier weeks can
    # only come from what earlier runs recorded, so carry those forward rather
    # than discarding the history each time.
    prior = {k: (w.get("metrics") or {}) for k, w in base_weeks.items()}
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as fh:
                for w in (json.load(fh).get("weeks") or []):
                    prior.setdefault(w.get("key"), w.get("metrics") or {})
        except Exception:
            pass

    for i, w in enumerate(weeks, 1):
        # Berlin boundaries expressed in the account's own timezone
        frm = w.pop("_start").astimezone(acct_tz).strftime("%Y/%m/%d %H:%M")
        to = w.pop("_end").astimezone(acct_tz).strftime("%Y/%m/%d %H:%M")
        cells = []
        for m in metrics:
            mode = m.get("mode") or "weekly"

            if mode == "cycleTime":
                v, n, err, jql = cycle_times(site, project, jcfg, email, token,
                                             args.timeout, frm, to)
                w["metrics"][m["key"]] = v
                w["jql"][m["key"]] = jql
                if err:
                    failures.append(f"{w['key']} {m['key']}: {err}")
                elif v is not None:
                    w.setdefault("notes", {})[m["key"]] = (
                        f"Median of {n} issue(s) resolved this week, measured from the "
                        f"{(jcfg.get('cycleTime') or {}).get('start') or 'last'} move into "
                        f"In Progress. Issues never marked in progress are excluded.")
                cells.append(f"{m['key']}={'?' if v is None else v}d/{n}")
                continue

            if mode == "capacity":
                w["metrics"][m["key"]] = capacity_by_wk.get(w["key"])
                if w["key"] in capacity_by_wk:
                    # There is no JQL behind this one, so the info icon points at
                    # the capacity grid the figure is derived from.
                    cap_url = ((jcfg.get("capacity") or {}).get("url")
                               or (board_info or {}).get("url"))
                    if cap_url:
                        w.setdefault("links", {})[m["key"]] = cap_url
                    w.setdefault("notes", {})[m["key"]] = (
                        "Sum of each person's available share of the week, where "
                        "100% counts as 1. Computed from the Jira capacity plan: "
                        "a full week less every contribution, vacation included.")
                    cells.append(f"{m['key']}={capacity_by_wk[w['key']]}")
                continue

            if mode == "sprint":
                sp = sprint_by_week.get(w["key"])
                n = sp.get(m.get("field") or "planned") if sp else None
                w["metrics"][m["key"]] = n
                if sp:
                    w.setdefault("links", {})[m["key"]] = (
                        site.rstrip("/") + "/secure/RapidBoard.jspa?rapidView="
                        + str(jcfg.get("boardId")) + "&view=reporting"
                        + "&chart=sprintRetrospectiveChart&sprint=" + str(sp["sprintId"]))
                    w.setdefault("notes", {})[m["key"]] = (
                        f"From the sprint report for {sp['sprintName']}. "
                        f"Sprints do not align with weeks, so a sprint's figure "
                        f"appears in every week it covers.")
                    cells.append(f"{m['key']}={n}@{sp['sprintName'][:18]}")
                continue

            jql = (m["jql"].replace("{{PROJECT}}", project)
                           .replace("{{TECHDEBT}}", tech_debt)
                           .replace("{{FROM}}", frm).replace("{{TO}}", to))
            w["jql"][m["key"]] = jql          # so each cell can link to Jira

            if mode == "pointInTime":
                if w.get("current"):
                    n, err = count_jql(site, jql, email, token, args.timeout)
                    if err:
                        failures.append(f"{w['key']} {m['key']}: {err}")
                    cells.append(f"{m['key']}={'?' if n is None else n}*")
                else:
                    n = prior.get(w["key"], {}).get(m["key"])   # whatever we captured before
                    if n is not None:
                        cells.append(f"{m['key']}={n}~")
                w["metrics"][m["key"]] = n
                continue

            n, err = count_jql(site, jql, email, token, args.timeout)
            w["metrics"][m["key"]] = n
            if err:
                failures.append(f"{w['key']} {m['key']}: {err}")
            cells.append(f"{m['key']}={'?' if n is None else n}")
        print(f"  [{i}/{count}] {w['key']}  {frm} -> {to}  " + "  ".join(cells))

    # Weeks this run did not query keep whatever the base had, so a nightly
    # two-week top-up still serves a year of history. A fetched week always
    # wins: late resolutions and reopened tickets change the past, which is
    # exactly why the recent weeks are re-queried rather than only the current
    # one.
    merged = {w["key"]: w for w in weeks}
    carried = 0
    for key, w in base_weeks.items():
        if key not in merged:
            merged[key] = w
            carried += 1
    weeks = sorted(merged.values(), key=lambda w: w.get("key") or "", reverse=True)[:keep]
    # Only the newest week is in progress, whatever an older base file claimed.
    for i, w in enumerate(weeks):
        w["current"] = w["partial"] = (i == 0)
    if carried:
        print(f"\n  carried {carried} week(s) forward from the base; "
              f"{len(weeks)} in the file")

    got = [w for w in weeks if any(v is not None for v in w["metrics"].values())]
    if not got:
        sys.exit("Every week failed, refusing to write an empty delivery.json. "
                 f"First error: {failures[0] if failures else 'unknown'}")

    out = {
        "generatedAt": datetime.now(tz).isoformat(timespec="seconds"),
        "site": site,
        "project": project,
        "boardId": jcfg.get("boardId"),
        "board": board_info,
        "capacity": capacity_info,
        "accountTimeZone": str(acct_tz),
        "metrics": [{"key": m["key"], "label": m["label"],
                     "short": m.get("short") or m["label"],
                     "unit": m.get("unit") or "",
                     "section": m.get("section") or "Bug fixes (metrics)",
                     "mode": m.get("mode") or "weekly"}
                    for m in metrics],
        "weeks": weeks,
        "failures": failures,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))

    pit = [m["key"] for m in metrics if (m.get("mode") or "weekly") == "pointInTime"]
    print(f"\nWrote {args.out} — {len(got)}/{count} weeks, "
          f"{os.path.getsize(args.out):,} bytes")
    if pit:
        print(f"point-in-time metrics ({', '.join(pit)}): measured for the current week only"
              f" — marked * above; ~ marks values carried over from earlier runs.")
    if failures:
        print(f"{len(failures)} cell(s) failed; written anyway.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
