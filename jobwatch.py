#!/usr/bin/env python3
"""jobwatch: poll Greenhouse / Lever / Ashby job boards and notify on new matching roles.

Usage:
  python jobwatch.py            # normal run: fetch, filter, notify, update seen.json
  python jobwatch.py --check    # verify every company slug in config.yaml resolves
  python jobwatch.py --dry-run  # fetch + filter, print matches, don't notify or save
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
SEEN_PATH = ROOT / "seen.json"
TIMEOUT = 20
HEADERS = {"User-Agent": "jobwatch/1.0 (personal job alert script)"}


@dataclass
class Job:
    key: str          # globally unique: "<ats>:<company>:<id>"
    company: str
    title: str
    location: str
    url: str
    description: str  # plain text, used for keyword checks
    posted: str       # ISO date string if known, else ""


# ---------------------------------------------------------------- fetchers

def _strip_html(s: str) -> str:
    s = html.unescape(s or "")          # Greenhouse double-escapes its HTML
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def fetch_greenhouse(slug: str) -> list[Job]:
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
            posted=j.get("first_published") or j.get("updated_at") or "",
        ))
    return jobs


def fetch_lever(slug: str) -> list[Job]:
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


def fetch_ashby(slug: str) -> list[Job]:
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


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


# ---------------------------------------------------------------- filtering

def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def classify(job: Job, f: dict) -> tuple[bool, list[str]]:
    """Return (keep, warnings)."""
    if not _any(f["title_include"], job.title):
        return False, []
    if _any(f.get("title_exclude", []), job.title):
        return False, []
    loc_inc = f.get("location_include", [])
    if loc_inc and job.location and not _any(loc_inc, job.location):
        return False, []
    loc_exc = f.get("location_exclude", [])
    if loc_exc and job.location and _any(loc_exc, job.location) and not _any(
            f.get("location_us_override", []), job.location):
        return False, []
    if _any(f.get("description_exclude", []), job.description):
        return False, []
    warnings = [label for label, pats in (f.get("description_warn") or {}).items()
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


# ---------------------------------------------------------------- main

def load_seen() -> dict:
    if SEEN_PATH.exists():
        return json.loads(SEEN_PATH.read_text())
    return {"boards": {}, "jobs": {}}


def fetch_all(companies: dict) -> tuple[dict[str, list[Job]], dict[str, str]]:
    tasks = [(ats, slug) for ats, slugs in companies.items() for slug in (slugs or [])]
    results, errors = {}, {}

    def run(task):
        ats, slug = task
        try:
            return task, FETCHERS[ats](slug), None
        except Exception as e:  # one bad board shouldn't kill the run
            return task, None, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        for (ats, slug), jobs, err in pool.map(run, tasks):
            board = f"{ats}:{slug}"
            if err:
                errors[board] = err
            else:
                results[board] = jobs
    return results, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify company slugs and exit")
    ap.add_argument("--dry-run", action="store_true", help="don't notify or save state")
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    results, errors = fetch_all(cfg["companies"])

    if args.check:
        for board, jobs in sorted(results.items()):
            print(f"OK    {board:40s} {len(jobs)} jobs")
        for board, err in sorted(errors.items()):
            print(f"FAIL  {board:40s} {err}")
        return 1 if errors else 0

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
    SEEN_PATH.write_text(json.dumps(seen, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
