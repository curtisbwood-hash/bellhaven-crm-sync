#!/usr/bin/env python3
"""
Bellhaven Senior Living — website scraper (Step 1)

Pulls EVERY Bellhaven location from the website and writes:
  data/bellhaven_locations.csv
  data/bellhaven_locations.json

Fields per location: name, address, city, state, zip, care_offerings
(plus slug + source_url as evidence for the matching step).

How discovery works — this matters:
  * The /communities directory is paginated (Page 1 of 3, etc.).
    We follow every page and collect every /communities/<slug> link.
  * We ALSO scan the homepage. The homepage "New this year" notice
    links to a community (Bellhaven Meadows of Findlay) that does
    NOT appear in the directory listing. A directory-only scrape
    finds 34 locations; the site itself says there are 35.
    Scanning every page we visit for community links catches it.
  * Slugs are de-duplicated, then each detail page is fetched.
    The detail page is authoritative — e.g. Findlay lists TWO care
    offerings on its detail page but would show differently in a
    card. We parse care offerings from the detail page only.

Stdlib only (urllib + re) — no pip installs, no API keys.
Run:  python3 scraper.py
"""

import csv
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = "https://analyst-assessment-production.up.railway.app"
OUT_DIR = Path(__file__).resolve().parent / "data"

HEADERS = {"User-Agent": "bellhaven-sync/1.0 (assessment scraper)"}


def fetch(path: str, attempts: int = 4) -> str:
    """Fetch with retries — the host occasionally returns a transient 502."""
    url = path if path.startswith("http") else BASE + path
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as err:  # HTTPError, URLError, timeout — retry all
            last_err = err
            if attempt < attempts:
                wait = 2 ** attempt
                print(f"  fetch {path} failed ({err}); retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url} after {attempts} attempts: {last_err}")


def clean(text: str) -> str:
    """Strip any tags, unescape entities, collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def community_slugs_from(page_html: str) -> list[str]:
    return re.findall(r'href="/communities/([a-z0-9\-]+)"', page_html)


def discover_slugs() -> list[str]:
    """Collect every community slug linked anywhere we crawl."""
    slugs: list[str] = []
    seen: set[str] = set()

    def add(found: list[str]) -> None:
        for s in found:
            if s not in seen:
                seen.add(s)
                slugs.append(s)

    # 1) Homepage — catches the featured community missing from the directory
    add(community_slugs_from(fetch("/")))

    # 2) Directory, following pagination until there is no Next page
    page = 1
    while True:
        page_html = fetch(f"/communities?page={page}")
        add(community_slugs_from(page_html))
        if f"/communities?page={page + 1}" in page_html:
            page += 1
        else:
            break

    return slugs


def parse_detail(slug: str) -> dict:
    url = f"{BASE}/communities/{slug}"
    page_html = fetch(f"/communities/{slug}")

    name_m = re.search(r"<h1>(.*?)</h1>", page_html, re.S)
    name = clean(name_m.group(1)) if name_m else ""

    addr_m = re.search(r"<dt>Address</dt>\s*<dd>(.*?)</dd>", page_html, re.S)
    address = city = state = zip_code = ""
    if addr_m:
        parts = re.split(r"<br\s*/?>", addr_m.group(1))
        if len(parts) >= 2:
            address = clean(parts[0])
            loc = clean(parts[1])
            loc_m = re.match(r"^(.+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", loc)
            if loc_m:
                city, state, zip_code = loc_m.group(1), loc_m.group(2), loc_m.group(3)
            else:
                city = loc  # leave raw so the failure is visible, not silent

    care_m = re.search(r"<dt>Care Offerings</dt>\s*<dd>(.*?)</dd>", page_html, re.S)
    care_offerings: list[str] = []
    if care_m:
        care_offerings = [
            clean(b) for b in re.findall(r'<span class="badge">(.*?)</span>', care_m.group(1), re.S)
        ]

    record = {
        "name": name,
        "address": address,
        "city": city,
        "state": state,
        "zip": zip_code,
        "care_offerings": care_offerings,
        "slug": slug,
        "source_url": url,
    }

    missing = [k for k in ("name", "address", "city", "state", "zip") if not record[k]]
    if missing or not care_offerings:
        print(f"  WARNING {slug}: missing {missing or []} care={care_offerings}", file=sys.stderr)
    return record


def main() -> int:
    slugs = discover_slugs()
    print(f"Discovered {len(slugs)} community slugs")

    records = []
    for i, slug in enumerate(slugs, 1):
        rec = parse_detail(slug)
        records.append(rec)
        print(f"  [{i}/{len(slugs)}] {rec['name']} — {rec['city']}, {rec['state']} {rec['zip']}")

    records.sort(key=lambda r: (r["state"], r["city"], r["name"]))

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    csv_path = OUT_DIR / "bellhaven_locations.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "address", "city", "state", "zip",
                         "care_offerings", "slug", "source_url"])
        for r in records:
            writer.writerow([r["name"], r["address"], r["city"], r["state"], r["zip"],
                             "; ".join(r["care_offerings"]), r["slug"], r["source_url"]])

    json_path = OUT_DIR / "bellhaven_locations.json"
    json_path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")

    print(f"\nWrote {len(records)} locations")
    print(f"  {csv_path}")
    print(f"  {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
