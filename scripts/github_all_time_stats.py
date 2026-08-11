#!/usr/bin/env python3
"""
Persistent GitHub repository statistics.

What "all-time" means here
--------------------------
* Traffic views/clones:
  GitHub exposes only a rolling 14-day daily window. On first run this program
  imports every recoverable traffic day, then permanently stores those daily
  values in the repository. Every later run reconciles the overlapping window.
  Therefore cumulative traffic is exact from `traffic_tracking_started` onward.

* Commits:
  Reconstructed from the complete git history on every run.

* Stars:
  Current star history is reconstructed with `starred_at` timestamps.

* Forks:
  Current forks are reconstructed with their creation timestamps.

* Release downloads:
  GitHub exposes cumulative download_count per current release asset. This
  program maintains an asset ledger and keeps the maximum ever seen for each
  asset, so an asset that is later deleted does not make tracked downloads fall.

No traffic value is interpolated. Missing API runs are recovered from later
overlapping traffic windows whenever still available.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

API = "https://api.github.com"
API_VERSION = "2022-11-28"

README = Path("README.md")
STATE = Path("stats/state.json")
DAILY = Path("stats/daily.csv")
POLL_LOG = Path("stats/polls.jsonl")
ASSET_LEDGER = Path("stats/release-assets.json")
CHARTS = Path("stats/charts")

README_START = "<!-- ALL_TIME_REPO_STATS_START -->"
README_END = "<!-- ALL_TIME_REPO_STATS_END -->"
BOT_COMMIT_PREFIX = "chore(stats):"

FIELDS = [
    "date",
    "views",
    "unique_visitors_daily",
    "views_status",
    "clones",
    "unique_cloners_daily",
    "clones_status",
    "commits",
    "stars_added",
    "stars_current_cumulative",
    "forks_added",
    "forks_current_cumulative",
    "release_downloads_tracked_cumulative",
]


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def zulu(v: dt.datetime) -> str:
    return v.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def api(path: str, *, accept: str = "application/vnd.github+json", retries: int = 4) -> Any:
    req = urllib.request.Request(
        API + path,
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "persistent-github-repository-statistics",
        },
    )
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403, 404):
                break
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"{path}: {last}")


def api_all(path: str, *, accept: str = "application/vnd.github+json") -> list[Any]:
    out: list[Any] = []
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        batch = api(f"{path}{sep}per_page=100&page={page}", accept=accept)
        if not isinstance(batch, list):
            raise RuntimeError(f"Expected list from {path}")
        out.extend(batch)
        if len(batch) < 100:
            return out
        page += 1


def try_api(path: str, *, accept: str = "application/vnd.github+json") -> tuple[Any, str | None]:
    try:
        return api(path, accept=accept), None
    except Exception as exc:
        return None, str(exc)


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def dates(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def empty_row(day: str) -> dict[str, str]:
    return {k: "" for k in FIELDS} | {
        "date": day,
        "views_status": "not_tracked",
        "clones_status": "not_tracked",
    }


def load_daily() -> dict[str, dict[str, str]]:
    if not DAILY.exists():
        return {}
    with DAILY.open(newline="", encoding="utf-8") as f:
        return {r["date"]: r for r in csv.DictReader(f)}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def as_int(v: str | int | None) -> int | None:
    if v is None or v == "":
        return None
    return int(v)


def max_assign(row: dict[str, str], key: str, value: int) -> None:
    old = as_int(row.get(key))
    row[key] = str(value if old is None else max(old, value))


def merge_traffic(
    rows: dict[str, dict[str, str]],
    payload: dict[str, Any] | None,
    *,
    array_key: str,
    count_key: str,
    unique_key: str,
    status_key: str,
    today: dt.date,
) -> list[dt.date]:
    seen: list[dt.date] = []
    if not payload:
        return seen

    for item in payload.get(array_key, []):
        day = parse_date(str(item["timestamp"]))
        seen.append(day)
        row = rows.setdefault(day.isoformat(), empty_row(day.isoformat()))

        # Same UTC day can be observed several times while still accumulating.
        # max() also prevents a transient lower/partial response from destroying
        # a previously stored exact count.
        max_assign(row, count_key, int(item.get("count", 0)))
        max_assign(row, unique_key, int(item.get("uniques", 0)))
        row[status_key] = "provisional" if day == today else "final"

    return seen


def traffic_start_from_state_or_payload(
    state: dict[str, Any],
    seen_views: list[dt.date],
    seen_clones: list[dt.date],
    today: dt.date,
) -> dt.date:
    if state.get("traffic_tracking_started"):
        return parse_date(state["traffic_tracking_started"])

    seen = seen_views + seen_clones
    # First run normally receives the oldest available daily bucket.
    # If GitHub temporarily returns no buckets, reserve the currently recoverable
    # interval so a later successful run can still fill it.
    return min(seen) if seen else today - dt.timedelta(days=13)


def mark_traffic_coverage(
    rows: dict[str, dict[str, str]],
    *,
    start: dt.date,
    today: dt.date,
) -> None:
    # The API's recoverable daily window is 14 days including today.
    oldest_currently_recoverable = today - dt.timedelta(days=13)

    for d in dates(start, today):
        row = rows.setdefault(d.isoformat(), empty_row(d.isoformat()))
        for value_key, status_key in (
            ("views", "views_status"),
            ("clones", "clones_status"),
        ):
            if row.get(status_key) == "not_tracked":
                row[status_key] = "pending"

            # Never guess a zero. Once a blank date falls outside the rolling API
            # window it is explicitly marked unrecoverable.
            if d < oldest_currently_recoverable and not row.get(value_key):
                row[status_key] = "unrecoverable"


def reconstruct_commits(rows: dict[str, dict[str, str]]) -> int:
    counts: dict[str, int] = {}
    log = git("log", "--date=format:%Y-%m-%d", "--pretty=format:%ad%x09%s")
    total = 0
    for line in log.splitlines() if log else []:
        if "\t" not in line:
            continue
        day, subject = line.split("\t", 1)
        if subject.startswith(BOT_COMMIT_PREFIX):
            continue
        counts[day] = counts.get(day, 0) + 1
        total += 1

    # Recomputed from scratch, so remove stale daily commit values first.
    for row in rows.values():
        row["commits"] = ""

    for day, count in counts.items():
        rows.setdefault(day, empty_row(day))["commits"] = str(count)
    return total


def reconstruct_star_history(rows: dict[str, dict[str, str]], base: str) -> tuple[int | None, str | None]:
    try:
        items = api_all(
            f"{base}/stargazers",
            accept="application/vnd.github.star+json",
        )
    except Exception as exc:
        return None, str(exc)

    for row in rows.values():
        row["stars_added"] = ""
        row["stars_current_cumulative"] = ""

    daily: dict[str, int] = {}
    for item in items:
        when = item.get("starred_at")
        if when:
            day = when[:10]
            daily[day] = daily.get(day, 0) + 1

    cumulative = 0
    if daily:
        start, end = parse_date(min(daily)), parse_date(max(daily))
        for d in dates(start, end):
            day = d.isoformat()
            add = daily.get(day, 0)
            cumulative += add
            row = rows.setdefault(day, empty_row(day))
            row["stars_added"] = str(add)
            row["stars_current_cumulative"] = str(cumulative)

    return len(items), None


def reconstruct_fork_history(rows: dict[str, dict[str, str]], base: str) -> tuple[int | None, str | None]:
    try:
        items = api_all(f"{base}/forks?sort=oldest")
    except Exception as exc:
        return None, str(exc)

    for row in rows.values():
        row["forks_added"] = ""
        row["forks_current_cumulative"] = ""

    daily: dict[str, int] = {}
    for item in items:
        when = item.get("created_at")
        if when:
            day = when[:10]
            daily[day] = daily.get(day, 0) + 1

    cumulative = 0
    if daily:
        start, end = parse_date(min(daily)), parse_date(max(daily))
        for d in dates(start, end):
            day = d.isoformat()
            add = daily.get(day, 0)
            cumulative += add
            row = rows.setdefault(day, empty_row(day))
            row["forks_added"] = str(add)
            row["forks_current_cumulative"] = str(cumulative)

    return len(items), None


def update_release_asset_ledger(base: str) -> tuple[dict[str, Any], int | None, int | None, str | None]:
    ledger = load_json(ASSET_LEDGER, {"assets": {}})
    try:
        releases = api_all(f"{base}/releases")
    except Exception as exc:
        old_total = sum(int(a["max_download_count"]) for a in ledger.get("assets", {}).values())
        return ledger, old_total if ledger.get("assets") else None, None, str(exc)

    now = zulu(now_utc())
    live_ids: set[str] = set()

    for release in releases:
        for asset in release.get("assets", []):
            asset_id = str(asset["id"])
            live_ids.add(asset_id)
            previous = ledger.setdefault("assets", {}).get(asset_id, {})
            current_count = int(asset.get("download_count", 0))
            max_count = max(int(previous.get("max_download_count", 0)), current_count)
            ledger["assets"][asset_id] = {
                "id": int(asset["id"]),
                "name": asset.get("name"),
                "release_id": release.get("id"),
                "release_tag": release.get("tag_name"),
                "created_at": asset.get("created_at"),
                "last_seen_at": now,
                "current_download_count": current_count,
                "max_download_count": max_count,
                "currently_exists": True,
            }

    for asset_id, record in ledger.get("assets", {}).items():
        if asset_id not in live_ids:
            record["currently_exists"] = False

    save_json(ASSET_LEDGER, ledger)

    tracked_total = sum(int(a["max_download_count"]) for a in ledger["assets"].values())
    current_total = sum(
        int(a["current_download_count"])
        for a in ledger["assets"].values()
        if a.get("currently_exists")
    )
    return ledger, tracked_total, current_total, None


def set_release_download_point(rows: dict[str, dict[str, str]], day: str, value: int | None) -> None:
    if value is not None:
        rows.setdefault(day, empty_row(day))["release_downloads_tracked_cumulative"] = str(value)


def write_daily(rows: dict[str, dict[str, str]]) -> None:
    DAILY.parent.mkdir(parents=True, exist_ok=True)
    with DAILY.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for day in sorted(rows):
            w.writerow({k: rows[day].get(k, "") for k in FIELDS})


def known_sum(rows: dict[str, dict[str, str]], key: str, start: dt.date | None = None) -> int:
    total = 0
    for day, row in rows.items():
        if start and parse_date(day) < start:
            continue
        v = as_int(row.get(key))
        if v is not None:
            total += v
    return total


def missing_count(rows: dict[str, dict[str, str]], status_key: str, start: dt.date) -> int:
    return sum(
        1
        for day, row in rows.items()
        if parse_date(day) >= start and row.get(status_key) == "unrecoverable"
    )


def svg_chart(
    path: Path,
    *,
    title: str,
    subtitle: str,
    rows: list[dict[str, str]],
    series: list[tuple[str, str]],
    cumulative_missing_as_hold: bool = False,
    width: int = 1100,
    height: int = 330,
) -> None:
    left, right, top, bottom = 72, 25, 58, 54
    pw, ph = width-left-right, height-top-bottom
    palette = ["#2f81f7", "#3fb950", "#d29922", "#f85149"]

    data: list[list[float | None]] = []
    all_vals: list[float] = []

    for key, _ in series:
        vals: list[float | None] = []
        last: float | None = None
        for row in rows:
            raw = row.get(key, "")
            if raw != "":
                v = float(raw)
                last = v
            elif cumulative_missing_as_hold:
                v = last
            else:
                v = None
            vals.append(v)
            if v is not None:
                all_vals.append(v)
        data.append(vals)

    ymax = max(all_vals) if all_vals else 1
    ymax = max(1, ymax * 1.08)
    n = max(1, len(rows))

    def X(i: int) -> float:
        return left if n == 1 else left + i * pw / (n-1)

    def Y(v: float) -> float:
        return top + ph - (v/ymax)*ph

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
        '<rect width="100%" height="100%" fill="#0d1117" rx="10"/>',
        f'<text x="{left}" y="27" fill="#f0f6fc" font-family="system-ui,sans-serif" font-size="18" font-weight="600">{html.escape(title)}</text>',
        f'<text x="{left}" y="47" fill="#8b949e" font-family="system-ui,sans-serif" font-size="12">{html.escape(subtitle)}</text>',
    ]

    for t in range(5):
        value = ymax*t/4
        yy = Y(value)
        out.append(f'<line x1="{left}" x2="{width-right}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="#21262d"/>')
        out.append(f'<text x="{left-8}" y="{yy+4:.1f}" text-anchor="end" fill="#8b949e" font-family="system-ui,sans-serif" font-size="11">{int(round(value)):,}</text>')

    if rows:
        k = min(8, len(rows))
        idxs = sorted(set(round(i*(len(rows)-1)/max(1,k-1)) for i in range(k)))
        for i in idxs:
            label = rows[i]["date"]
            out.append(f'<text x="{X(i):.1f}" y="{height-24}" text-anchor="middle" fill="#8b949e" font-family="system-ui,sans-serif" font-size="10">{label}</text>')

    for si, ((key, label), vals) in enumerate(zip(series, data)):
        colour = palette[si % len(palette)]
        segment: list[tuple[int,float]] = []

        def flush() -> None:
            nonlocal segment
            if not segment:
                return
            if len(segment) == 1:
                i,v = segment[0]
                out.append(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="2.5" fill="{colour}"/>')
            else:
                pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i,v in segment)
                out.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>')
            segment = []

        for i,v in enumerate(vals):
            if v is None:
                flush()
            else:
                segment.append((i,v))
        flush()

        lx = left + si*205
        out.append(f'<line x1="{lx}" x2="{lx+24}" y1="{height-8}" y2="{height-8}" stroke="{colour}" stroke-width="3"/>')
        out.append(f'<text x="{lx+30}" y="{height-4}" fill="#c9d1d9" font-family="system-ui,sans-serif" font-size="11">{html.escape(label)}</text>')

    out.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out)+"\n", encoding="utf-8")


def cumulative_traffic_rows(rows: dict[str, dict[str, str]], start: dt.date) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    cv = cc = 0
    views_valid = clones_valid = True

    if not rows:
        return out
    end = max(parse_date(d) for d in rows)

    for d in dates(start, end):
        source = rows.get(d.isoformat(), empty_row(d.isoformat()))
        vr = as_int(source.get("views"))
        cr = as_int(source.get("clones"))

        # Cumulative totals only advance on known values. They remain numerically
        # defined even if a historical gap exists, but the README explicitly
        # reports data-quality gaps.
        if vr is not None:
            cv += vr
        if cr is not None:
            cc += cr

        out.append({
            "date": d.isoformat(),
            "views_cumulative": str(cv),
            "clones_cumulative": str(cc),
        })
    return out


def render_charts(rows: dict[str, dict[str, str]], traffic_start: dt.date) -> None:
    ordered = [rows[d] for d in sorted(rows)]

    traffic_daily = [r for r in ordered if parse_date(r["date"]) >= traffic_start]
    svg_chart(
        CHARTS/"traffic-daily.svg",
        title="Daily repository traffic",
        subtitle=f"Exact stored GitHub daily buckets from {traffic_start.isoformat()} onward; gaps are not interpolated",
        rows=traffic_daily,
        series=[("views","Views"),("clones","Clones")],
    )

    cumulative = cumulative_traffic_rows(rows, traffic_start)
    svg_chart(
        CHARTS/"traffic-all-time.svg",
        title="All-time tracked traffic",
        subtitle=f"Cumulative stored views and clones since {traffic_start.isoformat()}",
        rows=cumulative,
        series=[("views_cumulative","Views"),("clones_cumulative","Clones")],
        cumulative_missing_as_hold=True,
    )

    svg_chart(
        CHARTS/"stars-all-time.svg",
        title="Current stars by star date",
        subtitle="Reconstructed from GitHub stargazer timestamps",
        rows=ordered,
        series=[("stars_current_cumulative","Stars")],
        cumulative_missing_as_hold=True,
    )

    svg_chart(
        CHARTS/"forks-all-time.svg",
        title="Current forks by fork creation date",
        subtitle="Reconstructed from GitHub fork creation timestamps",
        rows=ordered,
        series=[("forks_current_cumulative","Forks")],
        cumulative_missing_as_hold=True,
    )

    svg_chart(
        CHARTS/"commits-daily.svg",
        title="Project commits",
        subtitle="Full git history; automatic statistics commits excluded",
        rows=ordered,
        series=[("commits","Commits")],
    )


def fmt(v: int | None) -> str:
    return "n/a" if v is None else f"{v:,}"


def update_readme(
    *,
    now: dt.datetime,
    repo: dict[str, Any] | None,
    traffic_start: dt.date,
    rows: dict[str, dict[str, str]],
    commits: int,
    stars: int | None,
    forks: int | None,
    tracked_downloads: int | None,
    current_asset_downloads: int | None,
) -> None:
    views_total = known_sum(rows, "views", traffic_start)
    clones_total = known_sum(rows, "clones", traffic_start)
    view_gaps = missing_count(rows, "views_status", traffic_start)
    clone_gaps = missing_count(rows, "clones_status", traffic_start)

    repo_created = repo.get("created_at","")[:10] if repo else "unknown"
    watchers = int(repo.get("subscribers_count", 0)) if repo else None
    issues = int(repo.get("open_issues_count", 0)) if repo else None

    quality = "complete"
    if view_gaps or clone_gaps:
        quality = f"{view_gaps} view-day gap(s), {clone_gaps} clone-day gap(s)"

    block = f"""\
{README_START}
<table>
<tr>
<td><b>📈 All-time tracked views</b><br>{fmt(views_total)}</td>
<td><b>📥 All-time tracked clones</b><br>{fmt(clones_total)}</td>
<td><b>⭐ Current stars</b><br>{fmt(stars)}</td>
<td><b>🍴 Current forks</b><br>{fmt(forks)}</td>
</tr>
<tr>
<td><b>🧬 All-time commits</b><br>{fmt(commits)}</td>
<td><b>⬇️ Tracked release downloads</b><br>{fmt(tracked_downloads)}</td>
<td><b>👀 Watchers</b><br>{fmt(watchers)}</td>
<td><b>🩺 Traffic history</b><br>{html.escape(quality)}</td>
</tr>
</table>

<sub>
Repository created: <b>{html.escape(repo_created)}</b> ·
Traffic retained from: <b>{traffic_start.isoformat()}</b> ·
Updated: <b>{zulu(now)}</b>
</sub>

> **Traffic retention:** GitHub itself exposes only its most recent 14 days of repository views/clones. This repository permanently retains every daily bucket collected from **{traffic_start.isoformat()}** onward, so these cumulative traffic totals keep growing and never roll off.

### All-time tracked traffic

![All-time tracked traffic](stats/charts/traffic-all-time.svg)

### Daily traffic

![Daily repository traffic](stats/charts/traffic-daily.svg)

### Repository history

![Stars](stats/charts/stars-all-time.svg)

![Forks](stats/charts/forks-all-time.svg)

![Commits](stats/charts/commits-daily.svg)

<sub>
Data: <code>stats/daily.csv</code> ·
Release-asset ledger: <code>stats/release-assets.json</code> ·
Raw polls: <code>stats/polls.jsonl</code>
</sub>
{README_END}"""

    current = README.read_text(encoding="utf-8") if README.exists() else ""
    if README_START in current and README_END in current:
        before, rest = current.split(README_START,1)
        _, after = rest.split(README_END,1)
        new = before.rstrip()+"\n\n"+block+"\n\n"+after.lstrip()
    else:
        new = block+"\n\n"+current.lstrip()
    README.write_text(new.rstrip()+"\n", encoding="utf-8")


def main() -> None:
    now = now_utc()
    today = now.date()
    owner, repo_name = os.environ["GITHUB_REPOSITORY"].split("/",1)
    base = f"/repos/{owner}/{repo_name}"

    state = load_json(STATE, {})
    rows = load_daily()

    repo, repo_err = try_api(base)
    views, views_err = try_api(f"{base}/traffic/views?per=day")
    clones, clones_err = try_api(f"{base}/traffic/clones?per=day")

    seen_views = merge_traffic(
        rows, views,
        array_key="views",
        count_key="views",
        unique_key="unique_visitors_daily",
        status_key="views_status",
        today=today,
    )
    seen_clones = merge_traffic(
        rows, clones,
        array_key="clones",
        count_key="clones",
        unique_key="unique_cloners_daily",
        status_key="clones_status",
        today=today,
    )

    traffic_start = traffic_start_from_state_or_payload(state, seen_views, seen_clones, today)
    state["traffic_tracking_started"] = traffic_start.isoformat()
    state["last_updated_utc"] = zulu(now)

    mark_traffic_coverage(rows, start=traffic_start, today=today)

    commits = reconstruct_commits(rows)
    stars, stars_err = reconstruct_star_history(rows, base)
    forks, forks_err = reconstruct_fork_history(rows, base)

    ledger, tracked_downloads, current_asset_downloads, downloads_err = update_release_asset_ledger(base)
    set_release_download_point(rows, today.isoformat(), tracked_downloads)

    write_daily(rows)
    render_charts(rows, traffic_start)
    save_json(STATE, state)

    poll = {
        "timestamp_utc": zulu(now),
        "traffic_tracking_started": traffic_start.isoformat(),
        "errors": {
            "repository": repo_err,
            "views": views_err,
            "clones": clones_err,
            "stars": stars_err,
            "forks": forks_err,
            "release_downloads": downloads_err,
        },
        "current": {
            "stars": stars,
            "forks": forks,
            "commits": commits,
            "tracked_release_downloads": tracked_downloads,
            "current_existing_asset_downloads": current_asset_downloads,
            "all_time_tracked_views": known_sum(rows, "views", traffic_start),
            "all_time_tracked_clones": known_sum(rows, "clones", traffic_start),
        },
        "raw_traffic": {
            "views": views,
            "clones": clones,
        },
    }
    POLL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with POLL_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(poll, separators=(",",":"), sort_keys=True)+"\n")

    update_readme(
        now=now,
        repo=repo if isinstance(repo, dict) else None,
        traffic_start=traffic_start,
        rows=rows,
        commits=commits,
        stars=stars,
        forks=forks,
        tracked_downloads=tracked_downloads,
        current_asset_downloads=current_asset_downloads,
    )

    print(json.dumps(poll, indent=2))


if __name__ == "__main__":
    main()
