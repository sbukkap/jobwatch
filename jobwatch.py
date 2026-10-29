#!/usr/bin/env python3
"""jobwatch: poll Greenhouse / Lever / Ashby / Workday job boards and notify on new matching roles.

Usage:
  python jobwatch.py                    # normal run: fetch, filter, notify, update seen.json
  python jobwatch.py --check            # verify every company in config.yaml resolves
  python jobwatch.py --list             # show all open matching jobs now, newest first
  python jobwatch.py --list --hours 8   # only ones posted in the last 8 hours
  python jobwatch.py --list --only nvidia,stripe   # limit to some companies
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import requests
import yaml

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
SEEN_PATH = ROOT / "seen.json"
TIMEOUT = 20
HEADERS = {"User-Agent": "jobwatch/1.0 (personal job alert script)"}


@dataclass
class Job:
    key: str            # globally unique: "<ats>:<company>:<id>"
    company: str
    title: str
    location: str
    url: str
    description: str    # plain text, used for keyword checks
    posted: str         # ISO datetime, or ISO date if posted_is_date; "" if unknown
    posted_is_date: bool = False
    loader: Optional[Callable[["Job"], None]] = field(default=None, repr=False)

    def load_details(self) -> bool:
        """Fetch full details for boards whose listing API is thin (Workday). Returns False on failure."""
        if not self.loader:
            return True
        fn, self.loader = self.loader, None
        try:
            fn(self)
            return True
        except Exception as e:
            print(f"warning: could not load details for {self.url}: {e}", file=sys.stderr)
            return False


# ---------------------------------------------------------------- fetchers

def _strip_html(s: str) -> str:
    s = html.unescape(s or "")          # Greenhouse double-escapes its HTML
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def fetch_greenhouse(slug: str, _arg, _cfg) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append(Job(
            key=f"gh:{slug}:{j['id']}",
            company=slug,
            title=j.get("title", ""),
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""),
            description=_strip_html(j.get("content", "")),
            # first_published is the real post date; updated_at changes on every edit
            posted=j.get("first_published") or "",
        ))
    return jobs


def fetch_lever(slug: str, _arg, _cfg) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    jobs = []
    for j in r.json():
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        desc_parts = [j.get("descriptionPlain", ""), j.get("additionalPlain", "")]
        desc_parts += [_strip_html(l.get("content", "")) for l in j.get("lists", [])]
        created = j.get("createdAt")
        jobs.append(Job(
            key=f"lever:{slug}:{j['id']}",
            company=slug,
            title=j.get("text", ""),
            location=" / ".join(filter(None, locs)),
            url=j.get("hostedUrl", ""),
            description=" ".join(desc_parts),
            posted=datetime.fromtimestamp(created / 1000, timezone.utc).isoformat() if created else "",
        ))
    return jobs


def fetch_ashby(slug: str, _arg, _cfg) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"
    r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")]
        locs += [(s.get("location") or "") for s in j.get("secondaryLocations") or []]
        if j.get("isRemote"):
            locs.append("Remote")
        jobs.append(Job(
            key=f"ashby:{slug}:{j['id']}",
            company=slug,
            title=j.get("title", ""),
            location=" / ".join(dict.fromkeys(filter(None, locs))),
            url=j.get("jobUrl", ""),
            description=j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", "")),
            posted=j.get("publishedAt", ""),
        ))
    return jobs


# Workday has no official public API, but every Workday career site is backed by
# a JSON endpoint ("cxs"). The listing gives title, location and a coarse
# "Posted N Days Ago"; the description and exact post date need one more request
# per job, which we only make for jobs whose title already matches.

WORKDAY_PAGE = 20          # Workday rejects larger page sizes on many sites
WORKDAY_MAX_PER_TERM = 1000


def _parse_workday_board(url: str) -> tuple[str, str, str]:
    """'nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite' -> (host, tenant, site)."""
    url = re.sub(r"^https?://", "", url.strip()).strip("/")
    host, _, path = url.partition("/")
    parts = [p for p in path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    if not host.endswith("myworkdayjobs.com") or not parts:
        raise ValueError(f"not a Workday board URL: {url}")
    return host, host.split(".")[0], parts[0]


def _workday_posted(text: str) -> str:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' -> ISO date."""
    t = (text or "").lower()
    today = datetime.now(timezone.utc).date()
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    return ""


def fetch_workday(name: str, board_url: str, cfg: dict) -> list[Job]:
    host, tenant, site = _parse_workday_board(board_url)
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    hdrs = {**HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
    terms = (cfg.get("filters") or {}).get("workday_search") or [""]
    session = requests.Session()

    found: dict[str, dict] = {}
    for term in terms:
        offset, total = 0, None
        while offset < WORKDAY_MAX_PER_TERM:
            body = {"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": offset, "searchText": term}
            r = session.post(f"{api}/jobs", json=body, headers=hdrs, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if total is None:
                total = data.get("total") or 0   # only reliable on the first page
            page = data.get("jobPostings") or []
            for p in page:
                if p.get("externalPath"):
                    found.setdefault(p["externalPath"], p)
            offset += WORKDAY_PAGE
            if len(page) < WORKDAY_PAGE or offset >= total:
                break

    def make_loader(path: str):
        def load(job: Job) -> None:
            r = session.get(f"{api}{path}", headers=hdrs, timeout=TIMEOUT)
            r.raise_for_status()
            info = r.json().get("jobPostingInfo") or {}
            job.description = _strip_html(info.get("jobDescription", ""))
            locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
            country = (info.get("country") or {}).get("descriptor", "")
            job.location = " / ".join(dict.fromkeys(filter(None, locs + [country]))) or job.location
            if info.get("startDate"):
                job.posted = info["startDate"][:10]
            if info.get("externalUrl"):
                job.url = info["externalUrl"]
        return load

    jobs = []
    for path, p in found.items():
        jobs.append(Job(
            key=f"wd:{name}:{path}",
            company=name,
            title=p.get("title", ""),
            location=p.get("locationsText", ""),     # may be "3 Locations"; details fill it in
            url=f"https://{host}/en-US/{site}{path}",
            description="",
            posted=_workday_posted(p.get("postedOn", "")),
            posted_is_date=True,
            loader=make_loader(path),
        ))
    return jobs


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "ashby": fetch_ashby, "workday": fetch_workday}


# ---------------------------------------------------------------- filtering

def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def title_ok(job: Job, f: dict) -> bool:
    return _any(f["title_include"], job.title) and not _any(f.get("title_exclude", []), job.title)


def classify(job: Job, f: dict) -> tuple[bool, list[str]]:
    """Return (keep, warnings). May fetch job details (Workday) once the title matches."""
    if not title_ok(job, f):
        return False, []
    warnings = []
    if not job.load_details():
        warnings.append("couldn't load details, check location/requirements yourself")
    if not re.fullmatch(r"\d+ Locations?", job.location.strip(), re.IGNORECASE):
        loc_inc = f.get("location_include", [])
        if loc_inc and job.location and not _any(loc_inc, job.location):
            return False, []
        loc_exc = f.get("location_exclude", [])
        if loc_exc and job.location and _any(loc_exc, job.location) and not _any(
                f.get("location_us_override", []), job.location):
            return False, []
    if _any(f.get("description_exclude", []), job.description):
        return False, []
    warnings += [label for label, pats in (f.get("description_warn") or {}).items()
                 if _any(pats, job.description)]
    return True, warnings


# ---------------------------------------------------------------- notifying

def _fmt(job: Job, warnings: list[str]) -> str:
    line = f"**{job.company}** — {job.title}\n📍 {job.location or 'n/a'}\n{job.url}"
    if warnings:
        line += "\n⚠️ " + ", ".join(warnings)
    return line


def notify(items: list[tuple[Job, list[str]]]) -> None:
    if not items:
        return
    blocks = [_fmt(j, w) for j, w in items]
    sent = False

    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if webhook:
        sent = True
        for chunk in _chunks(blocks, 1900):
            requests.post(webhook, json={"content": chunk}, timeout=TIMEOUT).raise_for_status()
            time.sleep(1)  # stay under Discord's rate limit

    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        sent = True
        for chunk in _chunks(blocks, 3900):
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": chunk.replace("**", ""),
                      "disable_web_page_preview": True},
                timeout=TIMEOUT,
            ).raise_for_status()

    if not sent:
        print("No DISCORD_WEBHOOK_URL or TELEGRAM_* secrets set; printing instead:\n")
        print("\n\n".join(blocks))


def _chunks(blocks: list[str], limit: int):
    buf = ""
    for b in blocks:
        if buf and len(buf) + len(b) + 2 > limit:
            yield buf
            buf = ""
        buf = f"{buf}\n\n{b}" if buf else b
    if buf:
        yield buf


# ---------------------------------------------------------------- fetching all boards

def board_tasks(companies: dict, only: set[str] | None = None) -> list[tuple[str, str, str]]:
    """(ats, name, arg) for every board in config. Workday entries are name: url."""
    tasks = []
    for ats, entries in (companies or {}).items():
        if ats not in FETCHERS:
            print(f"warning: unknown board type '{ats}' in config.yaml", file=sys.stderr)
            continue
        if isinstance(entries, dict):
            pairs = list(entries.items())
        else:
            pairs = [(e, e) for e in entries or []]
        for name, arg in pairs:
            if only and name.lower() not in only:
                continue
            tasks.append((ats, str(name), str(arg)))
    return tasks


def fetch_all(cfg: dict, only: set[str] | None = None):
    tasks = board_tasks(cfg["companies"], only)
    results, errors = {}, {}

    def run(task):
        ats, name, arg = task
        try:
            return task, FETCHERS[ats](name, arg, cfg), None
        except Exception as e:  # one bad board shouldn't kill the run
            return task, None, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        for (ats, name, _), jobs, err in pool.map(run, tasks):
            board = f"{ats}:{name}"
            if err:
                errors[board] = err
            else:
                results[board] = jobs
    return results, errors


# ---------------------------------------------------------------- dates

def _parse_time(job: Job) -> datetime | None:
    s = job.posted
    if not s:
        return None
    try:
        if job.posted_is_date:
            return datetime.combine(date.fromisoformat(s[:10]), datetime.min.time(), timezone.utc)
        s = s.replace("Z", "+00:00")
        s = re.sub(r"\.\d+", "", s)  # drop fractional seconds for older Pythons
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _ago(job: Job, dt: datetime | None) -> str:
    if not dt:
        return "date unknown"
    if job.posted_is_date:
        days = (datetime.now(timezone.utc).date() - dt.date()).days
        return "today" if days <= 0 else "yesterday" if days == 1 else f"{days}d ago"
    h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if h < 1:
        return f"{max(1, int(h * 60))}m ago"
    if h < 48:
        return f"{int(h)}h ago"
    return f"{int(h // 24)}d ago"


def _within(job: Job, dt: datetime | None, hours: float) -> bool:
    if dt is None:
        return False
    now = datetime.now(timezone.utc)
    if job.posted_is_date:   # day precision: keep if posted on or after the cutoff's date
        return dt.date() >= (now - timedelta(hours=hours)).date()
    return (now - dt).total_seconds() <= hours * 3600


# ---------------------------------------------------------------- commands

def list_jobs(results: dict, errors: dict, filters: dict, hours: float | None) -> int:
    candidates = []
    for jobs in results.values():
        for job in jobs:
            if not title_ok(job, filters):
                continue
            if hours is not None and not _within(job, _parse_time(job), hours):
                continue
            candidates.append(job)

    # Workday jobs need a detail request each; do those in parallel.
    with ThreadPoolExecutor(max_workers=8) as pool:
        classified = list(pool.map(lambda j: (j, classify(j, filters)), candidates))

    rows = []
    for job, (keep, warnings) in classified:
        if not keep:
            continue
        dt = _parse_time(job)
        if hours is not None and not _within(job, dt, hours):   # re-check with exact date
            continue
        rows.append((dt, job, warnings))

    rows.sort(key=lambda r: r[0] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for dt, job, warnings in rows:
        print(f"[{_ago(job, dt):>12}]  {job.company} | {job.title}")
        print(f"                {job.location or 'n/a'}")
        print(f"                {job.url}")
        if warnings:
            print(f"                ⚠️ {', '.join(warnings)}")
        print()
    window = f" posted in the last {hours:g}h" if hours is not None else ""
    print(f"{len(rows)} matching jobs{window} across {len(results)} boards"
          + (f" ({len(errors)} boards failed: {', '.join(sorted(errors))})" if errors else ""))
    print("(Workday sites only report the posting day, so their jobs show 'today' / 'Nd ago'.)")
    return 0


def load_seen() -> dict:
    if SEEN_PATH.exists():
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    return {"boards": {}, "jobs": {}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify company boards and exit")
    ap.add_argument("--dry-run", action="store_true", help="don't notify or save state")
    ap.add_argument("--list", action="store_true",
                    help="print all currently open matching jobs, newest first")
    ap.add_argument("--hours", type=float, default=None,
                    help="with --list: only jobs posted within this many hours")
    ap.add_argument("--only", default=None,
                    help="comma-separated company names to limit --list/--check to")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # emoji-safe on Windows
    except Exception:
        pass

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    only = {s.strip().lower() for s in args.only.split(",")} if args.only else None
    if only and not (args.list or args.check):
        ap.error("--only works with --list or --check")
    results, errors = fetch_all(cfg, only)

    if args.check:
        for board, jobs in sorted(results.items()):
            print(f"OK    {board:40s} {len(jobs)} jobs")
        for board, err in sorted(errors.items()):
            print(f"FAIL  {board:40s} {err}")
        return 1 if errors else 0

    if args.list:
        return list_jobs(results, errors, cfg["filters"], args.hours)

    seen = load_seen()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_items = []

    for board, jobs in results.items():
        first_time = board not in seen["boards"]   # newly added company: seed silently
        seen["boards"][board] = now
        for job in jobs:
            if job.key in seen["jobs"]:
                continue
            seen["jobs"][job.key] = now
            if first_time:
                continue
            keep, warnings = classify(job, cfg["filters"])
            if keep:
                new_items.append((job, warnings))

    for board, err in errors.items():
        print(f"warning: {board} failed: {err}", file=sys.stderr)

    print(f"{len(results)} boards ok, {len(errors)} failed, {len(new_items)} new matches")

    if args.dry_run:
        for j, w in new_items:
            print("-", _fmt(j, w).replace("\n", " | "))
        return 0

    notify(new_items)

    # Prune job keys not seen for 60 days so the file doesn't grow forever.
    live = {j.key for jobs in results.values() for j in jobs}
    cutoff = time.time() - 60 * 86400
    seen["jobs"] = {
        k: ts for k, ts in seen["jobs"].items()
        if k in live or datetime.fromisoformat(ts).timestamp() > cutoff
    }
    SEEN_PATH.write_text(json.dumps(seen, indent=1, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
