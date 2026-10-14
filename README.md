# Weekly Ops Dashboard

Week-by-week New Relic numbers for the Foundation Enablement Team's applications — uptime,
response time, errors, error rate and alerts, per application and per provider.

Weeks run **Monday 00:00:00 to Sunday 23:59:59.999 Europe/Berlin**, so CET in winter and CEST in
summer. DST is handled: the last week of March is 167 hours, the last week of October 169, and the
buckets still start and end on the real Monday and Sunday.

## Run it

1. Your New Relic **User key** goes in `api_key.txt` next to `server.py`. It never goes into the
   web page.
2. macOS: double-click **start.command**. Any OS: `python3 server.py` in this folder.
3. Open <http://localhost:8787>.

New Relic's NerdGraph endpoint sends no CORS headers, so a browser page cannot call it directly.
`server.py` is a small standard-library proxy — nothing to install — that serves the page and
forwards GraphQL with the key attached.

## Applications

The dashboard belongs to a team, named in `config.json` under `team.name`, which becomes the page
heading and the browser title. Below it sit the team's applications, each with its own entity
GUIDs, facet attribute, NRQL and thresholds:

```json
"team": { "name": "Foundation Enablement Team" },
"defaultApplication": "all",
"applications": [
  { "id": "...", "name": "backend application name", "kind": "apm",
    "hostAttribute": "entity.name", "entityGuids": [ ... ], "thresholds": { ... }, "nrql": { ... } },
  { "id": "...",   "name": "frontend application name",   "kind": "synthetic",
    "hostAttribute": "monitorName", "entityGuids": [ ... ], "thresholds": { ... }, "nrql": { ... } }
]
```

`kind` is what makes two very different applications share one table:

| | backend application (`apm`) | frontend application (`synthetic`) |
|---|---|---|
| Source | `Metric` — real request traffic | `SyntheticCheck` — ping monitors |
| Availability | 100 − error rate | % of checks returning SUCCESS |
| Response | `apm.service.transaction.duration` × 1000 | `average(duration)`, successes only |
| Errors | error-group occurrences | failed checks |
| Breakdown | `FACET entity.name` | `FACET monitorName` |

Two details that would corrupt the numbers if copied carelessly between them: `SyntheticCheck.duration`
is already in milliseconds, so it must **not** be multiplied by 1000 the way the APM query is; and
the average is taken over successful checks only, or a timed-out check inflates it.

Because the measurements differ, so do the thresholds. frontend application name's error band counts *failed
checks* — single digits — where the API's counts error occurrences in the hundreds. Sharing one
band would leave one of them permanently green.

### What Frontend application's numbers do and do not mean

The monitors are `SIMPLE` ping checks: one HTTP request, no browser, no JavaScript, no assets. So
frontend application name here means **the URL answers, and how quickly** — not that the interface works.
There is no LCP, CLS or JavaScript-error data, and no real user traffic. The subtitle says so on
screen for the same reason. Real frontend health would need the Browser agent.

Availability is also quantised by check frequency: at one check every 5 minutes there are ~2,016
checks per monitor per week, so a single failure costs 0.05 percentage points. At 15-minute
frequency it costs 0.149pp, which makes a 99.9% target unreachable in practice — the only
achievable values are 100% or ≤99.85%. Worth checking the monitors' frequency before trusting that
band.

### The combined view

With more than one application the picker offers **Both applications**, the default. There, each
weekly figure is the **worse of the two**, graded by each application's own bands — availability of
an API and of a ping monitor cannot be averaged, and summing them would be meaningless, but "our
worst this week" is both true and useful. Expanding a week shows the two applications; the
tooltip on each figure names which one set it. Per-provider detail appears when a single
application is selected, which is why the provider filter hides itself in the combined view.

Provider names differ between the two sources — the API reports `AWS` and `AWS (west-2)`, the
monitors `Ping: frontend application name - AWS EU Prod` and so on. Each application's `hostLabels` maps them
onto one canonical set (`AWS EU`, `AWS US`, `OVH`, `Swisscom`, `T-Systems`) so the two line up.

**Settings → Discover APM apps** lists every APM application in the account with its GUID, so you
can confirm which is which and tick the right ones for the service you are viewing. If the
entity names are long, the **host label map** renames them for display, e.g.
`{"application name": "host name"}`.

**Test connection** pings the account and, if the region is wrong, tries the other one and switches.

## The queries

Each tile is editable NRQL under **Settings → Advanced**. Placeholders: `{{RANGE}}` (keep it —
becomes `SINCE <monday-epoch-ms> UNTIL <next-monday-epoch-ms>`), `{{GUIDS}}`, `{{HOST_ATTR}}`,
`{{MONITORS}}`. Single-value tiles must alias one column `value`. Do **not** add `TIMESERIES` —
each week is fetched as one bucket.

| Tile | Default source |
|---|---|
| Errors | `sum(newrelic.error.group.occurrences)` from `Metric`, with your `error.group.issueType` filter — your query, aggregated instead of faceted by error group |
| Avg. web response time | `average(apm.service.transaction.duration) * 1000`, `transactionType = 'Web'` |
| Error rate | `count(apm.service.error.count) / count(apm.service.transaction.duration) * 100` |
| Uptime | `100 − error rate`. **Check this one** — if your tile comes from a Synthetics monitor, put the monitor name in Settings and it switches to `percentage(count(*), WHERE result = 'SUCCESS')` from `SyntheticCheck`, or paste your own NRQL. |
| Alerts | `count(*)` from `NrAiIncident` where `event = 'open' AND priority = 'critical'`, with the `error.group.issueType` filter, scoped to the five GUIDs — plus a faceted variant for the per-application column |
| Per host | Response time faceted on `entity.name`, merged with faceted error and alert queries so the per-application figures match the totals. Throughput is still fetched — it is the denominator for per-application error rate and uptime — but is no longer shown as a column. |

To copy the real NRQL out of New Relic: open the dashboard, hover a tile → **⋯** → *View query*.

## Why a number is red

A red **error count** or **error rate** can explain itself — in the **Overview matrix** and in
the **History table** alike. Those two are counted from error groups, so the groups explain them.
Uptime is derived from transaction counts and is a different question, so it offers nothing even
when the same week is bad; the set lives in `WHY_METRICS`.

There is no icon or marker of any kind: the number being red is the signal, and the cursor changes
over it. Nothing is added to the cell, so a red figure sits exactly where an amber or green one
would. Hovering shows the error groups behind it, biggest first; clicking
pins the card so a link can be followed.

```
T-Systems · W41 2026
  812   Noticed exception 'RuntimeException' with message 'Company with id [...] not found. Will retry.'
        RuntimeException
  Largest of 4 error groups this week. New Relic has no permalink to a single
  group, so the link opens the application for these dates.
```

**One error, not a list.** The biggest group answers "what went wrong here" at a glance; six turned
a hover into reading. The footer says how many others there were, and the link goes to New Relic
for the rest. The snapshot still stores the top six, so showing more is a rendering change rather
than a refetch.

**Only red.** Amber and green need no excuse, and a card on every cell would make the table twitch
under the cursor.

**Dismissing it.** The card fades in, and fades out when the pointer leaves it, when you press
anywhere else, or on Escape. Leaving the *number* does not close it while the pointer is inside the
card, so a link stays clickable; leaving the card does. Pressing inside it — selecting the message,
following the link — leaves it alone. Opening another card while one is fading removes the old one
at once rather than cross-fading, and the fading card releases its id immediately so its own
removal timer can never take the new one with it. `prefers-reduced-motion` drops the animation.

**One tooltip, not two.** The cell carries no `title` attribute — the browser's own tooltip would
have appeared next to the card, grey and late, saying the same thing. The text lives in an
`aria-label` instead, which screen readers announce and browsers do not draw. The marker is
focusable, so Tab and Enter open the card without a pointer.

**A provider row shows that provider's errors**, the week row shows all of them. The matrix is
provider-by-provider throughout, so every cell there is scoped to one. Each error is
recorded with the entity it came from — the query facets on `entity.guid` — so the link goes to the
right application. Matching providers to GUIDs by position would have been a guess: the facet order
is New Relic's, not `config.json`'s.

**The link opens the application for that week**, not the error group. New Relic has no documented
permalink to a single group — the Errors Inbox keeps the selection behind an opaque state token —
so the card says where the link actually goes rather than implying a deep link it cannot make.
`/redirect/entity/<guid>` is the documented, stable entry point, with the week as `begin`/`end`.

`fetch_snapshot.py` takes its New Relic key from `NEW_RELIC_API_KEY` or, failing that, from
`api_key.txt` beside the script — the same two places, in the same order, as `server.py`. So if the
dashboard already runs locally, the snapshot builds locally too, with nothing to set up.

The data comes from one extra NRQL per week in `fetch_snapshot.py`:

```sql
SELECT sum(newrelic.error.group.occurrences) AS count FROM Metric
WHERE metricName = 'newrelic.error.group.occurrences'
  AND (error.group.issueType = 'Error' OR error.group.issueType IS NULL)
  AND entity.guid IN (...)
FACET `error.group.message`, `error.group.name`, `entity.name`, `entity.guid`
SINCE ... UNTIL ... LIMIT 12
```

The top six are published per week. Messages are truncated at 400 characters, and `entityGuids` now
travel in the snapshot — an entity GUID appears in every New Relic URL, so it is not a secret, and
without it the card could name an error but not take you to it.

Both paths run it — the published snapshot and the live page served by `server.py` — so the cards
behave the same whether you are reading last night's build or refreshing against New Relic. It is
one extra NRQL per week, alongside the eight already sent.

## When numbers look wrong

**Diagnostics** (top right) shows, for the most recent week, the exact NRQL sent for every tile,
the row count, and the raw JSON New Relic returned. **Copy all** gives you the lot as text, and each
query can be pasted straight into New Relic's query builder to compare side by side. If a query
returns zero rows, the metric name or the entity filter is the thing to change.

## Trends tab

A line chart per metric — uptime, response time, errors, error rate and alerts — with
one coloured line per APM application plus a dashed line for the combined total. Hover anywhere on
a chart for a crosshair and a tooltip listing every application's value for that week. Click a
legend chip to drop that application from every chart — and from the History table too, since the
selection is shared. That is the quick way to answer "is this one provider or all of them".

Applications are chosen once in the header, so each tab's own bar carries only what is specific to
it: **Weeks** on both, plus **Show combined total** and **Include the in-progress week** on Trends
(the latter off by default, since a partial week drags every line down). **Reset filters** sits on
the right of both bars and resets that tab's options — the application selection has its own
*Select all* and *Clear* in the header.

A dashed guide line marks the SLA target where one is set.

Charts are laid out **exactly two per row**, folding to one column below 760px. They are drawn as inline SVG, so there is no charting library and nothing to
download.

## Layout

Three top-level sections:

- **Overview** — the weekly KPI matrix, the landing view
- **Operations** — backend application, with **History** and **Trends** as sub-tabs
- **Delivery** — the Jira numbers

Operations carries its own bar directly under the section links: **History / Trends** on the left
and, when more than one application is enabled, the application picker on the right. The week's KPI
cards sit beneath it.

The **Providers** picker lives in the filter bar beside **Weeks**, since it filters rather than
scopes. History and Trends each have their own filter bar, so rather than duplicate the control —
two elements sharing one set of ids — the single picker is re-parented into whichever bar is on
screen. One control, one piece of state, always next to the week filter.

All of this is scoped to New Relic applications, so it lives inside the section and disappears when
you leave it. The header keeps only the team name, what is being measured, and the page-wide
actions.

### Weekly KPI matrix

The spreadsheet layout: KPIs down the left, calendar weeks across the top as `26-CW40`, oldest
column on the left. Row labels and the header row stay put while you scroll, so a value in week 41
is still identifiable as T-Systems' error rate.

Operations rows are coloured by the same bands as everywhere else, one row per provider under each
metric. Jira rows are **not** coloured, for the reason given below. A blank cell means no data for
that week — the ops snapshot and the Jira snapshot can cover different spans, and the matrix shows
whichever each one has rather than padding with zeros.

The matrix keeps its own week range, defaulting to 26, because 52 columns is unreadable as a
landing view. The other tabs share one range between them.

## Delivery tab (Jira)

Eleven KPIs from Jira project `FET`, on the same Monday-to-Sunday spine as the ops numbers, written
to `delivery.json` by `fetch_jira.py`.

### The metrics

| Metric | Column | Section | Mode |
|---|---|---|---|
| New bugs | New bugs | Bug fixes | weekly |
| Resolved bugs | Resolved bugs | Bug fixes | weekly |
| Open bug tickets | Open | Bug fixes | pointInTime |
| High priority bugs older than 90 days | High, >90d | Bug fixes | pointInTime |
| Medium priority bugs older than 90 days | Medium, >90d | Bug fixes | pointInTime |
| Tickets in Jira (all types, unresolved) | Open tickets | Delivery & Cycle time | pointInTime |
| Planned at sprint start | Planned | Delivery & Cycle time | sprint |
| Added after sprint start | Added | Delivery & Cycle time | sprint |
| Tickets resolved | Resolved | Delivery & Cycle time | weekly |
| Capacity available (person-weeks) | Capacity | Delivery & Cycle time | capacity |
| Cycle time, median (days) | Cycle time | Delivery & Cycle time | cycleTime |

Each metric names its own `section`, which drives both the topic tabs on the Delivery tab and the
grouping of Jira rows in the Overview matrix. Adding a metric to a section is a `config.json`
change, not a code change; a metric in a *new* section grows the tab bar by itself.

The tab splits by **topic** — *Bug fixes (metrics)* and *Delivery & Cycle time* — on a segmented
control above the History/Trends pills, both flush left. One topic at a time, so cards, table and
charts narrow together rather than showing ten columns at once.

### Tech Debt

A third topic, after *Delivery & Cycle time*, counting the same shapes of thing but only for
tickets whose **Engineering Capacity Category** is *Tech Debt*.

| Metric | Column | Mode |
|---|---|---|
| New tech debt tickets | New tickets | weekly |
| Resolved tech debt tickets | Resolved | weekly |
| Open tech debt tickets | Open | pointInTime |
| High priority tech debt older than 90 days | High, >90d | pointInTime |
| Medium priority tech debt older than 90 days | Medium, >90d | pointInTime |

The filter is written **once**, in `jira.techDebt`, and substituted into each metric as
`{{TECHDEBT}}`:

```json
"techDebt": { "field": "Engineering Capacity Category", "value": "Tech Debt" }
```

```jql
project = FET AND "Engineering Capacity Category" = "Tech Debt"
AND created >= "{{FROM}}" AND created < "{{TO}}"
```

A field name containing spaces is quoted automatically. **JQL accepts a custom field by name only
while that name is unique across the site** — if Jira rejects the query, put the id form in
`field` instead (`cf[10234]`), which needs no quoting and is never ambiguous.

Every metric here is scoped by the category alone — **no `issuetype` clause**. Tech debt is
whatever the team marked as tech debt, whether it was filed as a bug, a task or a story, so
counting only bugs would have under-reported it. The two aged metrics therefore read higher than
the equivalents in the Bug fixes section, which do filter to bugs.

Direction follows the obvious reading: debt arriving is bad, debt cleared is good, and the three
backlog counts are bad when they grow.

### The modes

How a metric is measured, not just what it counts. This is the part worth understanding, because
three of the five modes cannot produce real history.

| Mode | How it is measured | History |
|---|---|---|
| `weekly` | one JQL per week, counted | full — every week is recomputed each run |
| `pointInTime` | one JQL, run once, describing *now* | accumulates from the first run; earlier weeks stay blank |
| `sprint` | the board's sprint report, or issue changelogs | full, back to the first sprint on the board |
| `capacity` | the Capacity Planning API | only the weeks the plan actually covers |
| `cycleTime` | one JQL per week plus changelogs | full |

### The queries

`{{PROJECT}}`, `{{FROM}}` and `{{TO}}` are substituted per week. `FROM`/`TO` are the Monday and the
following Monday, **converted into the calling account's timezone** — see below, it is the easiest
thing here to get wrong.

```jql
-- New bugs
project = {{PROJECT}} AND issuetype = Bug
AND created >= "{{FROM}}" AND created < "{{TO}}"

-- Resolved bugs
project = {{PROJECT}} AND issuetype = Bug
AND resolved >= "{{FROM}}" AND resolved < "{{TO}}"

-- Tickets resolved (all issue types)
project = {{PROJECT}} AND resolved >= "{{FROM}}" AND resolved < "{{TO}}"

-- High / Medium priority bugs older than 90 days
project = {{PROJECT}} AND issuetype = Bug AND statusCategory != Done
AND priority = High   AND created <= -90d
project = {{PROJECT}} AND issuetype = Bug AND statusCategory != Done
AND priority = Medium AND created <= -90d

-- Open bug tickets
project = {{PROJECT}} AND issuetype = Bug AND statusCategory != Done

-- Tickets in Jira (all types, unresolved)
project = {{PROJECT}} AND statusCategory != Done

-- Cycle time: the issues whose durations are then read from their changelogs
project = {{PROJECT}} AND resolved >= "{{FROM}}" AND resolved < "{{TO}}"
```

`statusCategory != Done` rather than `resolution = EMPTY`: the two usually agree, but an issue
sitting in a Done column with no resolution set counts as open under the first and closed under the
second. The status category is what the board shows, and it keeps the aged-bug and open-ticket
numbers consistent with each other.

**Counting is not `total`.** Jira Cloud's current search endpoint returns no `total` and rejects
`maxResults=0`, so `count_jql` tries three routes, best first: `/search/approximate-count` (one
cheap POST, exact at these volumes), then `/search/jql` paged, then the pre-2025 `/search` endpoint.
A count is only trusted when a route finishes.

### Point-in-time metrics

The three aged-bug and open-ticket metrics describe the backlog **as it stands when the fetch
runs**. Jira cannot say what it was on a past Monday without replaying every changelog, so the
fetcher measures them for the current week only and carries forward whatever earlier runs recorded.
History accumulates from the first run rather than appearing complete and being wrong; earlier
weeks stay blank. The run log marks these `*` when freshly measured and `~` when carried over.

This is also why they show no week-on-week delta on the KPI cards until a second week exists.

### Topping up instead of rebuilding

A full run re-queries every week, which is a few hundred Jira requests. In the pipeline that is
wasteful: weeks that are over do not change much, and the numbers for them are already published.

```
python3 fetch_jira.py --base <path or url> --weeks 2 --keep 52
```

`--base` is an existing `delivery.json`. Weeks this run does not query are carried over from it;
weeks it does query overwrite what the base had. `--keep` caps the merged file.

The pipeline uses the **published dashboard itself** as the base:

```
python3 fetch_jira.py --base "$CI_PAGES_URL/delivery.json" --weeks 2 --out public/delivery.json
```

Last night's run is still being served by Pages, so its `delivery.json` is right there — no token,
no cache, no artifact plumbing. The file accumulates history run after run, and can hold more weeks
than any single fetch could reasonably retrieve.

**Two weeks, not one.** Issues get resolved late and reopened, so the week just gone is still
moving; re-asking is cheap and keeps it honest.

**A missing base widens the fetch.** If Pages cannot be read, a two-week run would publish a
two-week file — and that short file would become the next night's base, destroying the history for
good. So when `--base` is given and cannot be read, the script fetches `--keep` weeks instead and
says why. One slow run beats a silently truncated dashboard.

Point-in-time metrics keep working across top-ups: a week that is no longer current carries its
recorded value forward from the base, which is how their history accumulates in the first place.

### What a fetch costs

Roughly, for 52 weeks:

| Metric | Requests |
|---|---|
| each `weekly` metric | 1 per week (52) |
| each `pointInTime` metric | 1 in total |
| sprint scope | 1 per sprint, plus 2 to list |
| capacity | 1 per person, plus 1 per person-work pair |
| cycle time | 1 per week, each expanding changelogs — **the slowest** |

A full run is a few hundred requests and takes minutes, not seconds. `--weeks 12` while testing.

### Sprint scope

**Planned at sprint start** and **Added after sprint start** are `"mode": "sprint"`. They are the
only metrics that are not a JQL, because JQL cannot express them: `sprint in openSprints()` returns
membership *now*, so scope added on day four would be counted as part of the plan, and the number
would drift upward through the sprint — the opposite of what it is meant to measure.

Two routes are tried per sprint, and `delivery.json` records which one answered in `board.source`:

1. **the board's sprint report** — the greenhopper endpoint behind Jira's own Sprint Report screen,
   which reports issues added after the sprint began separately. Commitment is everything in the
   report minus those additions.
2. **the issues' changelogs** — every Jira records Sprint field changes with timestamps, so
   membership at the moment the sprint started can be reconstructed. Team-managed projects often do
   not serve the first endpoint, so this is not a rare path.

The changelog route also scans what changed since the sprint began, not just current members. An
issue planned on day one and pulled out on day five no longer matches `sprint = X`, so counting
current membership would quietly shrink the plan after the fact.

**The board's reported type is not used as a gate.** Team-managed projects describe their board as
`simple` while running sprints perfectly happily — an earlier version of this script treated that
as "Kanban, no sprints" and returned empty columns for a board that had a sprint running. The only
honest test is whether the board returns sprints, and that is now the only test.

Sprints do not line up with weeks. A sprint's figures are written to **every week it covers**, so a
two-week sprint shows the same number twice; the plan was set once, and pretending it changed
mid-sprint would be worse. For the same reason these two cards do not carry the "so far" marker —
they are not accumulating through the week. **Planned** is left uncoloured: a bigger sprint is not
a better one. **Added** is coloured, with more being worse.

What the board turned out to be is recorded under `board` in `delivery.json` — id, name, type, the
active sprints with their dates, how many sprints fell in the window, and `why` when the figures
came back empty. Two blank columns should never again be a mystery to debug.

### Capacity available (person-weeks)

From Jira's **Capacity Planning** API (`api.atlassian.com/ex/capacity-planning/{cloudId}/v1`),
which is where the Plans "Capacity by person" grid keeps its numbers — not the ordinary Jira REST
API, and not the same host.

A person free for a whole week counts as **1**, 40% counts as **0.4**, and the week's figure is the
sum across the team. At five working days, 0.4 is two days.

The API reports **allocations**, so how the Available row is read decides everything, and the two
readings are opposites. `jira.capacity.mode` makes the choice explicit:

| mode | meaning | when |
|---|---|---|
| `allocation` (default) | sum the rows named in `counts` | "Available" is a row people are allocated to |
| `remainder` | a full week less every contribution | "Available" is drawn by the UI from what is left |

Only the rows named in `counts` are summed in `allocation` mode — work, on-call and everything
else is ignored, which is the point: availability is the Available row and nothing else.

**Weeks nobody has filled in are blank, not full.** The API returns a cell for every week in the
window, zero-valued where nothing has been entered — so reading those literally made the whole team
look fully available for every past week. Only non-zero cells count as evidence that a week is
covered, and the metric is reported from the earliest covered week to the latest. A zero week
*inside* that range is kept, because there it really does mean no capacity rather than nothing
entered yet. `capacity.weeksPlanned` in `delivery.json` says how many weeks carry real data.

Finding that row does not depend on the plan-wide listing. `/non-project-work-items` can return
nothing while the same items are perfectly reachable one at a time through people's associations,
so each work item's name is resolved individually (and cached) when the listing comes up empty. If
no row called Available is found anywhere, the error names the ones that *were* seen rather than
only saying what is missing.

On identical data these give **2.6** and **0.6**. Getting it backwards turns 40% available into 60%,
so it is worth one check against the grid; `jira_probe.py --capacity` prints the raw cells.

**Nobody needs listing by hand.** The team is discovered: in `allocation` mode from whoever is on
the Available row, in `remainder` mode from who can be assigned work in the project. The second is
broader — anyone assignable counts, which may include people outside the team — so
`capacity.people` is there to pin an exact roster when that matters. `capacity.baseline` still
overrides anyone who is not full time.

Contributions are keyed by contributor *and* work item, so "everything for this person" is not one
call: the work items come from `/contributor-work-associations/by-contributor` first, then one
`/contributions` call each. `capacity.calls` in `delivery.json` records how many that took.

Configuration lives under `jira.capacity`: `people` (account ids — `jira_probe.py --capacity` lists
them), `hoursPerWeek` (only used when the API reports HOURS rather than PERCENTAGE), and
`baseline`, a per-person override for anyone who is not full time. `cloudId` is discovered from
`/_edge/tenant_info` unless pinned.

The API is a **preview** feature at the time of writing, so the contract may move under us. A 404
on the work-items endpoint means it is not enabled for the site.

### Cycle time, median (days)

For the issues **resolved** in a week, the time from the **last** move into In Progress to the
resolution, taken as a median.

- *Last* transition (`jira.cycleTime.start`, set to `"first"` to change it). The two answer
  different questions: the last move gives "once the team settled on this, how long did it take",
  timing a parked-and-restarted issue from the restart; the first gives "how long from first touch
  to done", including the parked stretch. On the same four issues these give **3.5** and **6.0**
  days, so it is worth knowing which one the number is.
- *Median*, not mean. Cycle times are badly skewed and one ticket left open over a holiday would
  drag an average somewhere nobody recognises.
- Issues resolved without ever being marked in progress are **excluded**. They have no measurable
  duration, and counting them as zero would flatter the figure. The hover note gives the sample
  size, so a median over two issues is visibly that.

Which statuses start the clock is `jira.cycleTime.startStatuses` (default `["In Progress"]`), with
`startStatusIds` if names are ambiguous. This metric reads changelogs, so it costs one search per
week — the slowest thing in the fetch.

Story points and escalation hours are deliberately **absent** — each
needs either a definition, a second API call, or a field ID. Adding them half-defined would produce
numbers nobody could defend.

**The counts carry no red/amber banding.** Bug arrivals and completions have no service target,
and colouring them invites gaming the number rather than improving the work. The column headers
carry a sparkline, with its direction coloured by whether more is better — fewer new bugs good,
more resolved good.

**Headings are short on purpose.** Six full metric names across is wider than a laptop, and a table
you scroll sideways is one you cannot read a row of. Each metric carries a `short` name for the
column heading and a `unit` shown under it; the full wording is the heading's tooltip and is what
the KPI cards and the Overview matrix use, where there is room. Worst case the delivery table is
about 900px wide, so it fits without a horizontal scrollbar on any normal screen.

**Every figure links to Jira**, opening that week's exact issue list, so a number you doubt is one
click from the tickets behind it. Each metric title also carries an **info icon**: hovering shows
the exact JQL behind that column, and clicking opens it in Jira. The sprint metrics have no JQL to
show, so theirs link to the sprint report and explain where the figure came from. The same icons appear on the Jira
rows of the Overview matrix. They use the newest week on screen, since the query is identical apart
from its date bounds.

### The timezone conversion

JQL reads bare date literals in the *calling account's* timezone, which for this account is GMT —
not Europe/Berlin. So `fetch_jira.py` asks Jira which timezone the account uses and converts each
Berlin boundary into it: a Berlin Monday 00:00 becomes `22:00` the previous day. Writing
`"2026/09/28 00:00"` instead would shift every week by two hours and silently misfile Monday-morning
tickets. The conversion is read from the account each run, so it follows if anyone changes it.

### Running it

```
python3 fetch_jira.py                 # writes delivery.json beside index.html
python3 fetch_jira.py --weeks 12 --out public/delivery.json
```

**`server.py` does this for you on startup.** It refreshes in a background thread, so the server
answers straight away rather than waiting on a few hundred Jira requests, and the page watches for
the file and loads it when it lands — no manual reload. If the fetch fails, it says so and the rest
of the dashboard carries on.

| Flag | Effect |
|---|---|
| *(default)* | refresh if `delivery.json` is missing or older than 12 hours |
| `--refresh-jira` | refresh regardless of age |
| `--no-jira` | never refresh on startup |
| `--jira-max-age N` | change the staleness threshold, in hours |

It is skipped entirely when the credentials are absent, with the reason printed.

**When something goes wrong you see it in the dashboard**, not only in the terminal. The server
records the outcome of each refresh at `/api/jira-status`, and the page shows it: the actual error
message with the last lines of output, a **Try again** button that re-runs the fetch without
restarting the server, or a note that Jira is not configured. A missing `delivery.json` used to
produce only a generic "run the fetcher" message, which is useless when the fetcher is what
failed.

Locally, credentials come from `jira_token.txt` — email on the first line, token on the second.

In CI they are **two separate variables**, not the file's combined contents:

| Variable | Value | Flags |
|---|---|---|
| `JIRA_EMAIL` | your Atlassian login email | Protected |
| `JIRA_API_TOKEN` | the API token **only**, without the email | Masked, Protected |

Pasting `email:token` into `JIRA_API_TOKEN` fails authentication: the script sends
`email:token` as HTTP Basic, so a combined value becomes `email:email:token`. Only the token
belongs there. The email is not secret, so it needs no masking — and masked values must be a
single line with no whitespace, which an email satisfies but gains nothing from.

The job runs the fetch only when **both** are set, naming whichever is missing in the log, and
carries on publishing the ops snapshot if the fetch fails. With no `delivery.json` the tab says
what to run. It must run where the Atlassian IP allowlist permits.

Delivery opens with the same current-week cards as Operations: the **current, in-progress week**
with the change against last week, on both of its views. Because the current week is a part-week,
counts compared against a complete one are not like-for-like, so the cards carry a greyed **so far**
next to the delta; it disappears when the week closes. Sprint and capacity figures do not carry it —
they are set once, not accumulating through the week.

The change is coloured by direction: fewer bugs arriving is green, more work completed is green.
The number itself is left uncoloured, because Operations colours its numbers by SLA band and these
counts have no SLA — a week with more resolved bugs is not "green" in any absolute sense. Two
metrics are left uncoloured entirely: **Planned at sprint start**, because a bigger sprint is not a
better one, and **Capacity available**, because more capacity is not better, only more.

Delivery has **History** and **Trends** views: the week table on one, a line chart per metric on the
other, each with its own week range and an option to include the in-progress week. The charts carry
no target line and no banding — these are counts, not service levels — and each title keeps its info
icon. Metrics with a JQL link to that week's exact issue list; the sprint and capacity metrics have
no JQL, so theirs link to the sprint report and the capacity grid instead, with the explanation on
hover.

### When a Jira column is empty

Blank is used deliberately, and `delivery.json` records the reason rather than leaving you to guess.

| What you see | What it means |
|---|---|
| aged-bug or open-ticket history blank | point-in-time; it only accumulates from the first run |
| sprint columns blank, `board.why` set | the board returned no sprints, or the report was unreadable |
| capacity blank before a certain week | the plan does not cover those weeks — `capacity.weeksPlanned` says how many it does |
| capacity blank entirely | `capacity.error`, or `workItemsSeen` listing what was found instead of "Available" |
| cycle time blank for a week | nothing was resolved, or nothing resolved was ever marked in progress |
| `n cell(s) failed` | per-week query failures; `failures` carries the first message |

A zero is not a blank. Capacity shows `0.0` for a week *inside* the plan with no availability, and
cycle time shows `0.0` when work was started and resolved the same day — worth a look, since that
often means tickets closed in a batch rather than genuinely fast flow.

The application and provider pickers hide themselves on this tab: Jira work is not split by
application, so they would be dead controls.

## Reading the table

One row per week, newest first; the current week is highlighted and marked *in progress*. Hover a
week number for its exact Monday-to-Sunday range with times and timezone. Column headers carry a
sparkline of the filtered period, right-aligned with the figures below them.

The sun/moon button at the top right switches between light and dark; until you press it the page
follows your operating system.

The filter bar above the table holds the week range; applications come from the header dropdown,
whose button shows what is active, e.g. `Applications: 4 of 5`. Unticking everything shows an empty
state rather than silently reverting to all.

Deselecting an application does more than hide its row — the weekly totals are **recalculated**
from the applications still selected, since a total covering services you have filtered out would
be misleading. Rows showing a recalculated total are tagged `subset`. Response time is re-averaged
weighted by throughput, and error rate re-derived from the remaining counts. With every
application selected, the figures queried from New Relic are used unchanged. **Export CSV** writes
whatever the filters are currently showing, marking recalculated rows `subtotal`.

Click anywhere on a week row (or its ▸ chevron) to expand the applications underneath. They
are rendered as ordinary rows of the same table, so each value sits directly under its own column
heading and lines up with the weekly total above it. The chevron is greyed out for weeks with no
per-application data. **Export CSV** writes total and per-application rows together.

Default thresholds (Settings → Advanced to change):

| Metric | Green | Amber | Red |
|---|---|---|---|
| Uptime | ≥ 99.9% | ≥ 99.5% | below |
| Avg response | 0–249 ms | 250–299 ms | 300 ms and above |
| Error rate | 0–0.001% | 0.001–0.01% | above 0.01% |
| Errors | 0–49 | 50–999 | 1000 and above |
| Alerts | 0 | 1–3 | more than 3 |

### Threshold shapes

A metric can be written either way.

**Shorthand** — two cut-offs, three colours:

```json
"uptime": { "good": 99.9, "warn": 99.5, "higherIsBetter": true }
```

**Bands** — explicit ranges, read in order like conditional-formatting rules, first match wins.
`from` and `to` are inclusive, and either can be left out for an open end, so a final band with
neither is the catch-all:

```json
"responseMs": {
  "higherIsBetter": false,
  "bands": [
    { "from": 0,   "to": 249, "state": "good", "label": "0-249 ms" },
    { "from": 250, "to": 299, "state": "warn", "label": "250-299 ms" },
    { "from": 300,            "state": "bad",  "label": "300 ms and above" }
  ]
}
```

`state` is `good`, `warn` or `bad` and picks the theme's green, amber or red. Add
`"color": "#7a4fd1"` to a band for a colour of your own, and `"label"` for the tooltip shown when
hovering that cell. Any number of bands is allowed, so a fourth "severe" tier is just another entry.

### How the total row is coloured

The weekly total cells, and the KPI cards above, are **not** coloured by grading the total figure.
They take the worst band any single application fell into that week, so a colour only appears if at
least one application actually earned it. Five applications at 40 errors each total 200, but every
one is inside the 0–49 green band, so the total stays green; one application at 1200 turns the
total red on its own. Hovering a total cell names the application that set the colour.

Weeks with no per-application breakdown fall back to grading the total figure directly.

`higherIsBetter` still applies with bands: it decides the direction of the week-over-week arrows
and which edge of the `good` band the dashed target line on the chart is drawn at.

## OfficeVibe Engagement

`fetch_officevibe.py` writes `engagement.json` on the same Monday-to-Sunday spine as the other two
snapshots.

| Metric | Column | Source |
|---|---|---|
| Survey participants | Participants | **derived** — see below |
| Participation rate | Participation % | `participationRate` |
| eNPS | eNPS | `nps` |
| Engagement score | Engagement | `engagementScore` |

## OfficeVibe Engagement

A fourth topic on the Delivery tab, and the **first block of the Overview matrix**, from the
Officevibe engagement scores another team already publishes as weekly JSON.

| Metric | Column | Source field |
|---|---|---|
| # of participants | Participants | `headcount` |
| Participation rate | Participation % | `participation_pct` |
| eNPS score | eNPS | `enps` |
| Engagement score | Engagement | `engagement_score` |

The names follow the team's spreadsheet. **Participants is the team's headcount** — everyone the
survey goes to — not the export's `est_participants`, which is participation x headcount and so
only restates the rate as a count. The spreadsheet's row being constant week to week is the
giveaway: it is the denominator, not the response. A suppressed week still reports its headcount,
since team size is not one of the figures Officevibe withholds.

```
python3 fetch_officevibe.py                     # url from config.json
python3 fetch_officevibe.py --file copy.json    # or a copy on disk
```

**The weeks already line up.** The export is keyed by ISO week, Monday to Sunday, the same spine
this dashboard uses, so no date juggling is needed. The fetcher still recomputes the key from each
row's `start` date rather than trusting the `period` string, so a file generated with a different
week convention is caught instead of misfiling a year of numbers by one week.

**Suppressed weeks are not missing weeks.** Officevibe withholds a week's scores when too few
people answered to keep them anonymous; those arrive as nulls with `"suppressed": true` and are
published as nulls. The run says how many, and `weeksSuppressed` records it, so a run of blanks
reads as anonymity rather than an outage.

**Participants is the export's own estimate** (`participation x headcount`), not a counted figure,
which is why it is shown without a good/bad direction. Participation, eNPS and engagement are all
better when higher.

The metrics are folded into the same week objects as the Jira ones, so the topic tabs, table,
charts and Overview matrix need no special case — the `section` on each metric is what puts it on
its own tab. If `engagement.json` is absent the tab and the matrix block simply do not appear.

**The matrix has its own section order** (`MX_SECTION_ORDER`), matching the spreadsheet: engagement,
then bugs, then delivery, then tech debt. The Delivery tab's topic tabs keep their natural order,
because the two answer different questions and bugs are the usual starting point there. A section
missing from the list keeps its natural position after the listed ones, so adding one needs no
code change. Matrix rows also carry their unit — `Participation rate (%)` — the way the ops rows
have always read `Uptime (%)`.

### Targets

Three of the four are graded against the team's targets:

| Metric | Green | Amber | Red |
|---|---|---|---|
| Participation rate | above 66% | 50-66% | under 50% |
| eNPS score | above 10 | 0-10 | negative |
| Engagement score | above 7.0 | 6.0-7.0 | below 6.0 |

Strictly above, so 66%, 10 and 7.0 are **not** green.

The red lines are not round numbers picked for neatness. **Half the team** is where a participation
figure stops describing the team and starts describing a minority of it. **Zero eNPS** is where
detractors begin to outnumber promoters, which is the one point on that scale that means something
on its own. **6.0 engagement** is the level this team only ever sat below during its worst
stretches, so it marks a real floor rather than an arbitrary one. The bands are ordered and the first match
wins with `to` inclusive, which is how the boundary lands on the right side:

```json
"ovParticipationRate": { "bands": [
  { "to": 66,   "state": "bad",  "label": "66% or less" },
  { "from": 66, "state": "good", "label": "above 66%" } ] }
```

They live in `officevibe.thresholds` and travel in `engagement.json`, so the published page grades
exactly as the local one does — the same arrangement the New Relic snapshot uses.

**Only these three are coloured.** `# of participants` is a headcount, not a performance, and every
Jira count on the other tabs has no service target, so they stay plain. The target shows on hover.

Each band carries a label that says why, shown on hover: *"negative — more detractors than
promoters"*, *"under half the team answered"*.

### Cards show the newest week that has a figure

Not every metric reports every week: a survey may still be open, a week's scores may be withheld,
a point-in-time metric only has the current week. The KPI cards therefore show, per metric, the
most recent week that actually has a number, labelled `W41 2026 · latest available` when that is
not the current week. Pinning every card to week zero showed a column of dashes while the numbers
sat one row below in the table.

## Publishing to GitLab Pages

Pages serves static files only, so the browser cannot query New Relic there — NerdGraph sends no
CORS headers, and a key shipped to Pages would be readable by anyone who can open the site.
Instead a **scheduled pipeline** runs the queries on the runner and publishes the numbers as
`data.json`. The page loads that automatically when no local server is present, hiding Refresh,
Settings and Diagnostics and showing when the snapshot was taken.

Set-up, once:

1. **Settings → CI/CD → Variables**: add `NEW_RELIC_API_KEY` with your User key, ticking **Masked**
   and **Protected**. The key stays in GitLab; only computed numbers reach the site.
2. **Settings → General → Visibility**: set Pages access to **Only project members**. This page
   exposes internal uptime, error and incident figures, and on a self-managed instance a Pages
   site can otherwise be reached without signing in.
3. **Build → Pipeline schedules**: add a daily schedule on `main`. Any time works — completed
   weeks never change, and only the in-progress week moves. Tick **Protected** if the variable is
   protected, so the job can read it.
4. Push. The first pipeline publishes to the project's Pages URL.

`Run pipeline` gives an on-demand refresh. The job also runs on `main` whenever `index.html`,
`config.json`, `fetch_snapshot.py` or `.gitlab-ci.yml` changes.

### Which runner

The build/test runners are the ones to use. The tag comes from the `RUNNER_TAG` variable at the
top of `.gitlab-ci.yml`, default `build` — the tag every other Pages job in this group uses. To
try the test runner instead, change that one value, or override `RUNNER_TAG` in Run pipeline.

Do **not** write `tags: [build, test]` expecting either runner to pick it up. GitLab treats a tag
list as AND: it requires a single runner carrying *both* tags, so if build and test are separate
runners the job matches none of them and stays pending indefinitely.

The snapshot contains only the weekly figures plus thresholds and host labels for rendering — no
API key, no NRQL, and no entity GUIDs. If some weeks fail, the rest are still published and the
page says so rather than showing a blank dashboard; only a total failure aborts the deploy, so a
transient New Relic problem cannot replace a good site with an empty one.

To generate a snapshot locally:

```
NEW_RELIC_API_KEY=$(cat api_key.txt) python3 fetch_snapshot.py --weeks 12 --out public/data.json
```

Nothing about local use changes: `server.py` still queries live, and Settings and Diagnostics stay
available there.

## Files

| File | What it is |
|---|---|
| `index.html` | The dashboard (single file, no dependencies) |
| `server.py` | Local web server + NerdGraph proxy |
| `config.json` | Saved settings — safe to edit by hand |
| `api_key.txt` | Your User key, in plain text. Git-ignored — never commit it. |
| `api_key.txt.example` | Placeholder so the repo documents the file without carrying a key |
| `start.command` | macOS double-click launcher |
| `fetch_snapshot.py` | Builds `data.json` for the static Pages deploy |
| `fetch_jira.py` | Builds `delivery.json` — the weekly Jira counts |
| `jira_probe.py` | Checks what the Atlassian API allows from a given machine |
| `jira_token.txt` | Atlassian email + API token. Git-ignored. |
| `.gitlab-ci.yml` | Daily scheduled pipeline that publishes to Pages |
| `.gitignore` | Keeps `api_key.txt` and the generated `public/` out of commits |

Settings are also cached in the browser, and that cache wins over `config.json`. If you edit
`config.json` by hand and the page ignores it, use **Settings → Advanced → Reset all settings**.
